"""推荐服务：把「患者申请 + 队列快照 + 规则库」组装成一次求解。

这是未来 FastAPI 接口 `POST /recommend` 的实现体，接口层只需做参数校验与鉴权。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from . import rules as rules_mod
from .estimator import QueueEstimator
from .models import DepartmentState, Exam, Plan, TicketReminder, TimeWindow
from .solver import ExamScheduler


class RequestError(ValueError):
    """请求本身不合法（项目不存在、科室缺快照等）。"""


def _shift_windows(windows: Sequence[TimeWindow], now_minute: int) -> List[TimeWindow]:
    """把「距 00:00 的绝对分钟」换算成「距快照时刻的相对分钟」。"""
    return [
        replace(w, open_minute=w.open_minute - now_minute, close_minute=w.close_minute - now_minute)
        for w in windows
    ]


def _dedup(seq: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _prepare(
    exam_codes: Sequence[str],
    *,
    now: datetime,
    states: Dict[str, DepartmentState],
    completed: Optional[Dict[str, float]] = None,
    fasting: bool = True,
    drain_rate: float = rules_mod.DEFAULT_DRAIN_RATE,
    start_offset: float = 0.0,
    tickets: Optional[Dict[str, int]] = None,
) -> Tuple[Optional[ExamScheduler], Optional[Plan]]:
    """校验请求并构造求解器。返回 (scheduler, blocking_plan)，二者恰有一个为 None。"""
    rules_mod.assert_valid()
    catalog = rules_mod.build_catalog()

    codes = _dedup(exam_codes)
    unknown = [c for c in codes if c not in catalog]
    if unknown:
        raise RequestError(f"以下检查编码不在规则库中：{', '.join(unknown)}")

    exams: List[Exam] = [catalog[c] for c in codes]

    # 取号：只保留属于本次检查的号
    clean_tickets: Dict[str, int] = {}
    for code, value in (tickets or {}).items():
        if code not in codes or value in (None, ""):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise RequestError(f"「{catalog[code].name}」的取号不是整数：{value!r}")
        if number <= 0:
            raise RequestError(f"「{catalog[code].name}」的取号必须大于 0")
        clean_tickets[code] = number

    # 过号不再直接判"不可行"：真实医院的处理是让你去分诊台重取，
    # 代价是多等一会儿。具体由 solver 的 missed 分支按现场排队重算，
    # 提示信息由 _build_notes 统一给出。

    # 准备条件：未空腹时需空腹的项目无法执行——报冲突而非自动跳过（方案 §2.2）
    fasting_blocked = [e for e in exams if e.requires_fasting and not fasting]
    if fasting_blocked:
        return None, Plan(
            feasible=False,
            status="INFEASIBLE",
            conflicts=[
                "患者当前非空腹，以下检查需空腹才能进行："
                + "、".join(f"{e.name}（{e.department}）" for e in fasting_blocked)
                + "。请确认是否改为另约时段。"
            ],
        )

    # 项目互斥：命中即交临床确认，不擅自删医嘱
    present = set(codes)
    mutex_hits = [
        f"「{catalog[a].name}」与「{catalog[b].name}」互斥：{reason}"
        for a, b, reason in rules_mod.mutex_pairs()
        if a in present and b in present
    ]
    if mutex_hits:
        return None, Plan(feasible=False, status="INFEASIBLE", conflicts=mutex_hits)

    missing_dept = sorted({e.department for e in exams if e.department not in states})
    if missing_dept:
        raise RequestError(f"队列快照缺少科室：{', '.join(missing_dept)}")

    estimator = QueueEstimator(states.values(), drain_rate=drain_rate,
                               buffer=rules_mod.ESTIMATE_BUFFER)
    windows = _shift_windows(rules_mod.build_windows(), now.hour * 60 + now.minute)

    scheduler = ExamScheduler(
        exams,
        rules_mod.build_dependencies(),
        windows,
        estimator,
        start_time=start_offset,
        completed=completed or {},
        tickets=clean_tickets,
        travel_minutes=rules_mod.TRAVEL_MINUTES,
    )
    return scheduler, None


def build_reminders(
    plan: Plan,
    states: Dict[str, DepartmentState],
    tickets: Dict[str, int],
) -> List[TicketReminder]:
    """在等待窗口里寻找可以顺路取号的空档。

    判据（源自「取号与检查解耦」的设计讨论）：

        本步占用时间（排队 + 检查） + 往返耗时 ≤ 目标科室当前排队 × 保守系数

    左边是「取完号回来、再把当前这项做完」需要的时间，右边是「取到号后到被叫到」
    的等待。左边不超过右边，就说明取完号回来号还没被叫过，不会过号。
    """
    if not plan.feasible or not plan.steps:
        return []

    travel = rules_mod.TRAVEL_MINUTES
    round_trip = travel * 2.0
    pending = [s for s in plan.steps if s.needs_ticket]
    if not pending:
        return []

    out: List[TicketReminder] = []
    used_dept: set = set()

    for step in plan.steps:
        if step.wait_minutes < rules_mod.REMIND_MIN_WAIT:
            continue
        busy = step.wait_minutes + step.exam.service_minutes
        for cand in pending:
            code = cand.exam.code
            dept = cand.exam.department
            if code in tickets or dept in used_dept:
                continue
            if dept == step.exam.department:
                continue                     # 同科室做完顺手取，不必提醒
            st = states.get(dept)
            if st is None or st.wait_minutes <= 0:
                continue
            b_wait = st.wait_minutes * rules_mod.REMIND_BUFFER
            if busy + round_trip > b_wait:
                continue                     # 取完号回来会过号，不提醒

            out.append(TicketReminder(
                at=step.arrive_at,
                during_exam=step.exam.name,
                exam_code=code,
                exam_name=cand.exam.name,
                department=dept,
                travel_minutes=travel,
                expect_call_at=step.arrive_at + travel + b_wait,
                reason=(
                    f"等{step.exam.name}的 {step.wait_minutes:.0f} 分钟里，"
                    f"可顺路去{dept}取号（往返 {round_trip:.0f} 分钟）；"
                    f"{dept}当前排队约 {st.wait_minutes:.0f} 分钟，回来仍赶得上"
                ),
            ))
            used_dept.add(dept)
            break                            # 一步只提醒一项，避免刷屏
    return out


def recommend(
    exam_codes: Sequence[str],
    *,
    now: datetime,
    states: Dict[str, DepartmentState],
    completed: Optional[Dict[str, float]] = None,
    fasting: bool = True,
    reference_order: Optional[Sequence[str]] = None,
    drain_rate: float = rules_mod.DEFAULT_DRAIN_RATE,
    start_offset: float = 0.0,
    time_limit: float = rules_mod.SOLVER_TIME_LIMIT,
    tickets: Optional[Dict[str, int]] = None,
) -> Plan:
    """给出最优检查顺序。

    参数
    ----
    exam_codes       : 患者待完成的检查编码列表（顺序即医嘱开立顺序）
    now              : 快照时刻
    states           : 科室队列快照
    completed        : 已完成项目 code -> 完成时刻（距快照的分钟数，过去为负）
    fasting          : 患者当前是否空腹
    reference_order  : 上一次推荐顺序，用于抑制推荐抖动
    drain_rate       : 队列消退速率，1.0 默认，0.0 为保守情景
    start_offset     : 距快照已过去的分钟数（重规划时使用）
    tickets          : 患者已取的号 code -> 号码。填了的项按叫号时刻固定开始时间；
                       没填的项按"到达即排队"估算。
    """
    scheduler, blocked = _prepare(
        exam_codes,
        now=now,
        states=states,
        completed=completed,
        fasting=fasting,
        drain_rate=drain_rate,
        start_offset=start_offset,
        tickets=tickets,
    )
    if blocked is not None:
        return blocked

    assert scheduler is not None
    plan = scheduler.solve(reference_order=reference_order, time_limit=time_limit)

    if not plan.feasible:
        detail = _format_ticket_conflicts(scheduler, now)
        if detail:
            plan.conflicts = detail      # 有具体冲突时，替掉"找不到可行顺序"这种笼统提示
    elif scheduler.tickets:
        plan.notes.append(
            "已按取号计算：各步开始时间由该号被叫到的时刻决定，"
            "按时到场即可，早到只会站着多等"
        )

    plan.notes.extend(_build_notes(plan, scheduler.exams, scheduler.estimator, scheduler.windows))

    plan.reminders = build_reminders(plan, states, tickets or {})
    if plan.reminders:
        plan.notes.append(
            "部分项目还没取号，方案在等待空档里给出了取号建议；"
            "按建议取号可避免做完再跑一趟"
        )
    plan.notes.extend(scheduler.estimator.consistency_warnings())
    return plan


def _clock(now: datetime, offset: float) -> str:
    return (now + timedelta(minutes=float(offset))).strftime("%H:%M")


def _format_ticket_conflicts(scheduler: ExamScheduler, now: datetime) -> List[str]:
    """把取号冲突翻译成人话：哪两个号撞了、大概几点、后果是什么。"""
    out: List[str] = []
    for item in scheduler.ticket_conflicts():
        a = item["a"]
        b = item["b"]
        out.append(
            f"「{a['name']}」（{a['ticket']} 号，约 {_clock(now, a['call'])} 叫到）"
            f"与「{b['name']}」（{b['ticket']} 号，约 {_clock(now, b['call'])} 叫到）"
            f"时间重叠：两处都要求本人在场，必有一个会过号作废，建议重取其中一个的号"
        )
    return out


def compare_baselines(
    exam_codes: Sequence[str],
    *,
    medical_order: Optional[Sequence[str]] = None,
    **kwargs,
) -> Dict[str, Optional[Plan]]:
    """与基线算法对比（方案 §6、§8.3：同一组任务、同一套约束、同一队列）。"""
    scheduler, blocked = _prepare(exam_codes, **kwargs)
    if blocked is not None:
        return {"本方案": blocked}

    assert scheduler is not None
    result: Dict[str, Optional[Plan]] = {"本方案": scheduler.solve()}
    if medical_order:
        result["按医嘱顺序"] = scheduler.medical_order(medical_order)
    result["最短等待优先"] = scheduler.greedy_shortest_wait()
    return result


def explain_steps(plan: Plan, states: Dict[str, DepartmentState]) -> List[str]:
    """为方案中的每一步生成一句可解释的理由（方案 §5.5）。"""
    if not plan.feasible or not plan.steps:
        return []

    catalog = rules_mod.build_catalog()
    deps = rules_mod.build_dependencies()
    chosen = set(plan.order())
    out: List[str] = []

    for step in plan.steps:
        code = step.exam.code
        parts: List[str] = []

        if step.missed:
            parts.append(f"{step.ticket} 号已过号，改按现场重新取号排队")
        elif step.shared_with:
            partner = catalog[step.shared_with].name if step.shared_with in catalog else step.shared_with
            parts.append(f"与{partner}一次到场，顺带完成，不重复排队")

        dependents = [d.after for d in deps if d.before == code and d.after in chosen]
        if dependents:
            names = "、".join(catalog[c].name for c in dependents)
            parts.append(f"其下游的{names}需要先拿到本项结果，先做可减少后续空转")

        initial = states[step.exam.department].wait_minutes
        if step.wait_minutes <= 0.5:
            parts.append("到达即可开始，不额外占用等待")
        elif step.wait_minutes < initial - 1:
            parts.append(
                f"此刻预计等待 {step.wait_minutes:.0f} 分钟，较初始 {initial:.0f} 分钟已明显下降"
            )
        elif step.wait_minutes <= 15:
            parts.append(f"预计等待仅 {step.wait_minutes:.0f} 分钟，穿插代价低")
        else:
            parts.append(f"此刻到达仍需等待 {step.wait_minutes:.0f} 分钟，属当前可接受水平")

        if step.forced_idle > 0.5:
            parts.append(f"需等待前置结果约 {step.forced_idle:.0f} 分钟")

        out.append("；".join(parts))

    return out


def _build_notes(
    plan: Plan,
    exams: Sequence[Exam],
    estimator: QueueEstimator,
    windows: Sequence[TimeWindow],
) -> List[str]:
    """生成面向使用者的人话提示（方案 §5.5 解释与异常处理）。"""
    notes: List[str] = []
    if not plan.feasible or not plan.steps:
        return notes

    if plan.total_idle > 0.5:
        notes.append(
            f"因等待前置检查结果或科室开诊，累计空转约 {plan.total_idle:.0f} 分钟"
            f"（已尽量用其他检查填补该时段）"
        )

    missed = [s for s in plan.steps if s.missed]
    if missed:
        names = "、".join(s.exam.name for s in missed)
        notes.append(
            f"{names} 的号与已有检查撞车、已经过号，方案改按现场重新取号安排；"
            f"总时长里已包含重新排队的等待"
        )

    shared = [s for s in plan.steps if s.shared_with]
    if shared:
        names = "、".join(s.exam.name for s in shared)
        notes.append(f"{names} 与同科室项目一次到场顺带完成，不重复排队")

    # 初始等待最长的科室若被排在较后位置，说明在利用队列自然消退
    long_wait = sorted(
        ((e.department, estimator.wait_now(e.department)) for e in exams),
        key=lambda x: x[1],
        reverse=True,
    )
    if long_wait and long_wait[0][1] >= 40:
        dept, w0 = long_wait[0]
        first = next((s for s in plan.steps if s.exam.department == dept), None)
        if first is not None and first.arrive_at >= 20:
            notes.append(
                f"{dept}当前排队约 {w0:.0f} 分钟，方案将其安排在 +{first.arrive_at:.0f} 分钟，"
                f"届时预计等待降至 {first.wait_minutes:.0f} 分钟"
            )

    # 时间窗余量：余量过小时提示人工复核
    shifted = {w.exam: w for w in windows}
    for step in plan.steps:
        win = shifted.get(step.exam.code)
        if win is None:
            continue
        slack = win.close_minute - step.start_at
        if 0 <= slack <= 20:
            notes.append(
                f"{step.exam.name}受时间窗约束，预计开始时间距关闭仅剩 {slack:.0f} 分钟，"
                f"建议优先安排以留出缓冲"
            )

    return notes


def recommend_from_defaults(
    *,
    now: Optional[datetime] = None,
    reference_order: Optional[Sequence[str]] = None,
    drain_rate: float = rules_mod.DEFAULT_DRAIN_RATE,
    start_offset: float = 0.0,
) -> Plan:
    """用内置数据跑一次推荐，供 main.py 与单元测试调用。"""
    import data_source

    request = data_source.get_patient_request()
    snapshot_time = data_source.SNAPSHOT_TIME if now is None else now
    states = data_source.get_snapshot(snapshot_time)

    return recommend(
        request["exam_codes"],
        now=snapshot_time,
        states=states,
        completed=request.get("completed") or {},
        fasting=bool(request.get("is_fasting", True)),
        reference_order=reference_order,
        drain_rate=drain_rate,
        start_offset=start_offset,
    )
