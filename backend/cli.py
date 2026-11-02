"""命令行入口：输入患者检查清单 + 科室队列快照，输出最优检查顺序。

用法：
    python main.py
    python main.py --exams CBC,ECG,ECHO,US_ABD
    python main.py --drain 0.0          # 保守情景：队列完全不消退
    python main.py --json               # 输出机器可读结果
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from backend.app import data_source
from backend.app import rules as rules_mod
from backend.app import service
from backend.app.models import DepartmentState, Plan


# ----------------------------------------------------------------------
# 排版工具：中文字符按两个宽度计算，保证表头对齐
# ----------------------------------------------------------------------
def _w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _w(text))
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def _clock(now: datetime, offset: float) -> str:
    return (now + timedelta(minutes=offset)).strftime("%H:%M")


def _line(char: str = "─", width: int = 72) -> str:
    return char * width


# ----------------------------------------------------------------------
# 展示
# ----------------------------------------------------------------------
def render(
    plan: Plan,
    request: dict,
    now: datetime,
    states: Dict[str, DepartmentState],
    comparison: Dict[str, Optional[Plan]],
    drain_rate: float,
) -> None:
    print("=" * 72)
    print(" 儿科门诊多检查顺序推荐 · 原型 v0.1")
    print("=" * 72)
    print(f" 快照时刻：{now:%Y-%m-%d %H:%M}    患者：{request.get('patient_id', '-')}")
    print(f" 队列假设：drain_rate={drain_rate:.2f}"
          f"（{'队列随时间线性消退' if drain_rate > 0 else '队列不消退（保守）'}）")
    print()

    # 【1】科室队列
    print("【1】科室队列快照")
    print(_line())
    print(f"  {_pad('科室', 12)} {_pad('排队人数', 10, 'right')} {_pad('预计等待', 10, 'right')}")
    for dept in sorted(states, key=lambda d: -states[d].wait_minutes):
        st = states[dept]
        print(f"  {_pad(dept, 12)} {_pad(str(st.queue_length), 10, 'right')} {_pad(f'{st.wait_minutes:.0f} 分', 10, 'right')}")
    print()

    # 【2】待检项目与耦合
    catalog = rules_mod.build_catalog()
    print("【2】待检项目与耦合关系")
    print(_line())
    for code in request["exam_codes"]:
        exam = catalog[code]
        flag = "（需空腹）" if exam.requires_fasting else ""
        print(f"  {_pad(code, 8)} {_pad(exam.name, 12)} {_pad(exam.department, 10)} "
              f"{exam.service_minutes:.0f} 分钟{flag}")
    deps = rules_mod.build_dependencies()
    shown = [d for d in deps if d.before in request["exam_codes"] and d.after in request["exam_codes"]]
    if shown:
        print()
        print("  耦合规则：")
        for d in shown:
            lag = f"，间隔 {d.lag_minutes:.0f} 分钟" if d.lag_minutes else "，完成即可"
            print(f"    {catalog[d.before].name} → {catalog[d.after].name}{lag}")
            print(f"      依据：{d.reason}")
    print()

    if not plan.feasible:
        print("【!】无法给出推荐")
        print(_line())
        for c in plan.conflicts:
            print(f"  × {c}")
        print()
        return

    # 【3】推荐方案
    print("【3】推荐顺序")
    print(_line())
    header = (f"  {_pad('#', 4)}{_pad('检查项目', 16)}{_pad('科室', 10)}"
              f"{_pad('到达', 9, 'right')}{_pad('排队', 10, 'right')}"
              f"{_pad('开始', 9, 'right')}{_pad('结束', 9, 'right')}")
    print(header)
    for k, step in enumerate(plan.steps, 1):
        print(
            f"  {_pad(str(k), 4)}"
            f"{_pad(step.exam.name, 16)}"
            f"{_pad(step.exam.department, 10)}"
            f"{_pad(_clock(now, step.arrive_at), 9, 'right')}"
            f"{_pad(f'{step.wait_minutes:.0f} 分', 10, 'right')}"
            f"{_pad(_clock(now, step.start_at), 9, 'right')}"
            f"{_pad(_clock(now, step.finish_at), 9, 'right')}"
        )
    print(_line())
    print(f"  预计全部完成：{_clock(now, plan.total_minutes)}"
          f"（自现在起 {plan.total_minutes:.0f} 分钟）")
    print(f"  其中累计排队 {plan.total_wait:.0f} 分钟"
          f"，检查占用 {plan.total_minutes - plan.total_wait - plan.total_idle:.0f} 分钟"
          f"，等结果空转 {plan.total_idle:.0f} 分钟")
    print(f"  求解状态：{plan.status}    搜索节点：{plan.nodes}"
          f"    耗时：{plan.elapsed_ms:.1f} ms")
    print()

    # 【4】逐项理由
    print("【4】为什么是这个顺序")
    print(_line())
    reasons = service.explain_steps(plan, states)
    for k, step in enumerate(plan.steps):
        print(f"  {k + 1}. {step.exam.name}——{reasons[k] if k < len(reasons) else ''}")
    print()

    # 【5】方案对比
    print("【5】与其他策略对比（同一队列、同一约束）")
    print(_line())
    base = plan.total_minutes
    for name, other in comparison.items():
        if other is None:
            print(f"  {_pad(name, 16)} 该顺序不可行（时间窗冲突）")
            continue
        if not other.feasible:
            print(f"  {_pad(name, 16)} 不可行")
            continue
        delta = other.total_minutes - base
        if name == "本方案":
            print(f"  {_pad(name, 16)} {other.total_minutes:6.0f} 分钟")
        else:
            pct = delta / other.total_minutes * 100 if other.total_minutes else 0
            tail = f"节省 {delta:.0f} 分钟（{pct:.1f}%）" if delta > 0.5 else "无差异"
            print(f"  {_pad(name, 16)} {other.total_minutes:6.0f} 分钟    {tail}")
    print()

    # 【6】提示
    if plan.notes:
        print("【6】提示与风险")
        print(_line())
        for note in plan.notes:
            print(f"  · {note}")
        print()


# ----------------------------------------------------------------------
# 重规划演示（方案 §5.4）
# ----------------------------------------------------------------------
def demo_replan(plan: Plan, request: dict, now: datetime, states: Dict[str, DepartmentState]) -> None:
    if not plan.feasible or len(plan.steps) < 2:
        return

    done = plan.steps[0]
    rest = [c for c in request["exam_codes"] if c != done.exam.code]
    completed = dict(request.get("completed") or {})
    completed[done.exam.code] = done.finish_at

    new_plan = service.recommend(
        rest,
        now=now,
        states=states,
        completed=completed,
        fasting=bool(request.get("is_fasting", True)),
        reference_order=plan.order(),
        start_offset=done.finish_at,
    )

    print("【7】滚动重规划演示（完成第一项后）")
    print(_line())
    print(f"  已完成：{done.exam.name}，于 {_clock(now, done.finish_at)} 结束")
    if new_plan.feasible:
        print(f"  剩余 {len(new_plan.steps)} 项，新推荐顺序：")
        for step in new_plan.steps:
            print(f"    · {_pad(step.exam.name, 12)} 到达 {_clock(now, step.arrive_at)}"
                  f"   排队 {step.wait_minutes:>3.0f} 分"
                  f"   结束 {_clock(now, step.finish_at)}")
        print(f"  预计全部完成：{_clock(now, new_plan.total_minutes)}")
    else:
        print("  剩余项目无可行方案：")
        for c in new_plan.conflicts:
            print(f"    × {c}")
    print()


# ----------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="儿科门诊多检查顺序推荐")
    parser.add_argument("--exams", help="覆盖患者检查清单，逗号分隔，如 CBC,ECG,ECHO")
    parser.add_argument("--drain", type=float, default=rules_mod.DEFAULT_DRAIN_RATE,
                        help="队列消退速率 0~1，默认 1.0")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    problems = rules_mod.validate()
    if problems:
        print("[规则库校验未通过]")
        for p in problems:
            print("  -", p)
        return

    request = data_source.get_patient_request()
    if args.exams:
        request["exam_codes"] = [c.strip().upper() for c in args.exams.split(",") if c.strip()]

    now = data_source.SNAPSHOT_TIME
    states = data_source.get_snapshot(now)

    try:
        plan = service.recommend(
            request["exam_codes"],
            now=now,
            states=states,
            completed=request.get("completed") or {},
            fasting=bool(request.get("is_fasting", True)),
            drain_rate=args.drain,
        )
    except service.RequestError as exc:
        print(f"[请求不合法] {exc}")
        return

    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return

    try:
        comparison = service.compare_baselines(
            request["exam_codes"],
            medical_order=request["exam_codes"],
            now=now,
            states=states,
            completed=request.get("completed") or {},
            fasting=bool(request.get("is_fasting", True)),
            drain_rate=args.drain,
        )
    except service.RequestError:
        comparison = {"本方案": plan}

    render(plan, request, now, states, comparison, args.drain)
    demo_replan(plan, request, now, states)


if __name__ == "__main__":
    main()
