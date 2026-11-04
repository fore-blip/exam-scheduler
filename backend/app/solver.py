"""合法检查序列的精确求解器。

对应方案 §5.3「路径一」：对每条合法候选顺序调用队列模拟器，用分支限界搜索最优。

为什么第一版不用 CP-SAT
----------------------
等待时长是"患者到达时刻"的函数，而到达时刻又由顺序决定——目标函数依赖自身输出。
CP-SAT 无法内建这种外部排队逻辑（方案 §5.3「重要工程判断」）。而单患者检查数通常
3~8 项，合法序列规模在可穷举范围内。因此先用"模拟器在环 + 分支限界"建立可信基准，
待真实规模确认后，再决定是否引入时间离散化 + CP-SAT（路径二）。

目标与次要目标
--------------
首要：最小化预计全部完成时间（makespan）。
次要：完成时间相差不足 TIE_WINDOW 分钟时，优先保留与上一次推荐更接近的方案，
      避免推荐抖动（方案 §5.4「变更门槛」）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .estimator import QueueEstimator
from .models import Dependency, Exam, Plan, PlanStep, TimeWindow

INF = float("inf")
TIE_WINDOW = 1.0   # 分钟。完成时间差异在此范围内时，按推荐稳定性择优
HONOR_SLACK = 3.0  # 分钟。为「不浪费已取到的号」最多允许多花的时间


@dataclass
class _Candidate:
    """某一时刻执行某项检查的完整时间线。"""

    idx: int
    arrive: float
    wait: float
    start: float
    finish: float
    forced_idle: float
    ticket: Optional[int] = None
    call_at: Optional[float] = None
    missed: bool = False
    shared_with: Optional[str] = None
    wait_source: str = "estimate"
    travel_minutes: float = 0.0
    needs_ticket: bool = False


def _deviation(order: Sequence[int], ref_pos: Dict[int, int]) -> int:
    """与参考顺序的逆序对数量，衡量推荐变动幅度。未出现在参考中的项目不计。"""
    seq = [ref_pos[i] for i in order if i in ref_pos]
    total = 0
    for a in range(len(seq)):
        for b in range(a + 1, len(seq)):
            if seq[a] > seq[b]:
                total += 1
    return total


class ExamScheduler:
    """单患者多检查顺序求解器。"""

    def __init__(
        self,
        exams: Sequence[Exam],
        dependencies: Sequence[Dependency],
        windows: Sequence[TimeWindow],
        estimator: QueueEstimator,
        *,
        start_time: float = 0.0,
        completed: Optional[Dict[str, float]] = None,
        tickets: Optional[Dict[str, int]] = None,
        travel_minutes: float = 0.0,
        tickets_first: bool = True,
    ) -> None:
        self.exams: List[Exam] = list(exams)
        self.estimator = estimator
        self.start_time = float(start_time)
        self.completed: Dict[str, float] = dict(completed or {})
        self.windows: List[TimeWindow] = list(windows)
        # 换科室的单程耗时。同科室连续两项视为一次到场，不再计移动。
        self.travel_minutes = max(0.0, float(travel_minutes))
        # 患者已经取到的号：检查编码 -> 号码。没取号的项走"到达即排队"的旧逻辑。
        self.tickets: Dict[str, int] = {
            str(k): int(v) for k, v in (tickets or {}).items() if v is not None
        }
        # 已取号的项必须排在前面：号已经拿到手，患者不会（也不该）先去做别的。
        # 打开时是硬约束——搜索时只在「还没做完的已取号项」里选，
        # 全部做完后才放开其余项目。这样自然形成「已挂号的块 + 其余项目的块」。
        self.tickets_first = bool(tickets_first)

        self._index: Dict[str, int] = {e.code: i for i, e in enumerate(self.exams)}
        self._prereq: Dict[int, List[Tuple[int, float]]] = {i: [] for i in range(len(self.exams))}
        self._external: Dict[int, List[Tuple[str, float]]] = {i: [] for i in range(len(self.exams))}
        self._missing: List[str] = []

        for dep in dependencies:
            if dep.after not in self._index:
                continue
            target = self._index[dep.after]
            if dep.before in self._index:
                self._prereq[target].append((self._index[dep.before], dep.lag_minutes))
            elif dep.before in self.completed:
                self._external[target].append((dep.before, dep.lag_minutes))
            else:
                self._external[target].append((dep.before, dep.lag_minutes))
                self._missing.append(
                    f"「{self.exams[target].name}」依赖「{dep.before}」的结果，"
                    f"但该检查既不在待检列表中、也未标记为已完成"
                )

        self._windows: Dict[int, Tuple[int, int]] = {}
        for win in windows:
            if win.exam in self._index:
                i = self._index[win.exam]
                self._windows[i] = (win.open_minute, win.close_minute)

    # ------------------------------------------------------------------
    # 基础模拟
    # ------------------------------------------------------------------
    def _candidate(self, idx: int, now: float, finish_of: Dict[int, float]) -> Optional[_Candidate]:
        """给定当前时刻与已完成任务，计算执行 idx 的时间线；不可行返回 None。"""
        exam = self.exams[idx]

        # 1) 前置结果何时可用
        release = now
        for j, lag in self._prereq[idx]:
            if j not in finish_of:
                return None
            release = max(release, finish_of[j] + lag)
        for code, lag in self._external[idx]:
            if code not in self.completed:
                return None
            release = max(release, self.completed[code] + lag)

        window = self._windows.get(idx)

        # 换科室的移动耗时。同科室连续两项视为一次到场，不再计移动；
        # 第一项从当前位置直接出发，也不计。
        prev_dept: Optional[str] = None
        prev_finish = -1e18
        for j, fin in finish_of.items():
            if fin > prev_finish:
                prev_finish, prev_dept = fin, self.exams[j].department
        travel = self.travel_minutes if (prev_dept is not None and prev_dept != exam.department) else 0.0

        # 同科室是否已有项目被安排。若有，患者到那时人已经在科室里，
        # 后面的项目可以顺带一次做完（典型：检验科一次采血做血常规 + 生化）。
        same_finish: Optional[float] = None
        same_code: Optional[str] = None
        for j, fin in finish_of.items():
            if self.exams[j].department == exam.department and (same_finish is None or fin > same_finish):
                same_finish, same_code = fin, self.exams[j].code

        # 2) 取号模式：号决定了"最早可做时刻"
        ticket = self.tickets.get(exam.code)
        if ticket is not None:
            call = self.estimator.call_offset(exam.department, ticket)
            if call is not None:
                arrive = max(now + travel, release)
                if window is not None and arrive < window[0]:
                    arrive = float(window[0])

                # 2a) 顺带做：上一项就在同科室、人还在里面，且本项的号已被叫过
                if same_finish is not None and travel <= 1e-9 and same_finish >= call - 1e-9:
                    start = max(arrive, same_finish)
                    if window is not None and start > window[1] + 1e-9:
                        return None
                    return _Candidate(
                        idx=idx,
                        arrive=arrive,
                        wait=max(0.0, start - arrive),
                        start=start,
                        finish=start + exam.service_minutes,
                        forced_idle=0.0,
                        ticket=ticket,
                        call_at=call,
                        shared_with=same_code,
                        wait_source="ticket",
                        travel_minutes=travel,
                    )

                # 2b) 赶得上：按号等到叫号时刻开始
                if call >= -1e-9 and arrive <= call + 1e-9:
                    if window is not None and call > window[1] + 1e-9:
                        return None
                    return _Candidate(
                        idx=idx,
                        arrive=arrive,
                        wait=call - arrive,
                        start=call,
                        finish=call + exam.service_minutes,
                        forced_idle=0.0,
                        ticket=ticket,
                        call_at=call,
                        wait_source="ticket",
                        travel_minutes=travel,
                    )

                # 2c) 号已过：作废，按到达时刻现场重新取号排队。
                #     不判"不可行"——真实医院也是让你去分诊台重取，代价是多等一会儿。
                wait = self.estimator.estimate_wait_at(exam.department, arrive)
                start = arrive + wait
                if window is not None and start > window[1] + 1e-9:
                    return None
                return _Candidate(
                    idx=idx,
                    arrive=arrive,
                    wait=wait,
                    start=start,
                    finish=start + exam.service_minutes,
                    forced_idle=arrive - now,
                    ticket=ticket,
                    call_at=call,
                    missed=True,
                    wait_source="estimate",
                    travel_minutes=travel,
                )

        # 3a) 无号但同科室刚做完：一次到场可以顺带完成（检验科一次采血做多项）
        if same_finish is not None and travel <= 1e-9:
            start = max(max(now, release), same_finish)
            if window is not None and start > window[1] + 1e-9:
                return None
            return _Candidate(
                idx=idx,
                arrive=start,
                wait=0.0,
                start=start,
                finish=start + exam.service_minutes,
                forced_idle=start - now,
                shared_with=same_code,
                wait_source="estimate",
                travel_minutes=0.0,
            )

        # 3) 未取号：按"到达即排队"估算
        arrive = max(now + travel, release)
        if window is not None and arrive < window[0]:
            arrive = float(window[0])

        wait = self.estimator.estimate_wait_at(exam.department, arrive)
        start = arrive + wait

        if window is not None and start > window[1] + 1e-9:
            return None

        return _Candidate(
            idx=idx,
            arrive=arrive,
            wait=wait,
            start=start,
            finish=start + exam.service_minutes,
            forced_idle=arrive - now,
            wait_source="estimate",
            travel_minutes=travel,
            needs_ticket=exam.code not in self.tickets,
        )

    @staticmethod
    def _to_step(cand: _Candidate, exam: Exam) -> PlanStep:
        return PlanStep(
            exam=exam,
            arrive_at=cand.arrive,
            wait_minutes=cand.wait,
            start_at=cand.start,
            finish_at=cand.finish,
            forced_idle=cand.forced_idle,
            ticket=cand.ticket,
            call_at=cand.call_at,
            missed=cand.missed,
            shared_with=cand.shared_with,
            wait_source=cand.wait_source,
            travel_minutes=cand.travel_minutes,
            needs_ticket=cand.needs_ticket,
        )

    def simulate(self, sequence: Sequence[str], start_time: Optional[float] = None) -> Optional[Plan]:
        """按固定顺序推进，返回计划；顺序不可行（时间窗冲突等）返回 None。"""
        now = self.start_time if start_time is None else float(start_time)
        finish_of: Dict[int, float] = {}
        steps: List[PlanStep] = []

        for code in sequence:
            idx = self._index.get(code)
            if idx is None:
                return None
            cand = self._candidate(idx, now, finish_of)
            if cand is None:
                return None
            steps.append(self._to_step(cand, self.exams[idx]))
            finish_of[idx] = cand.finish
            now = cand.finish

        return Plan(steps=steps, status="FIXED")

    # ------------------------------------------------------------------
    # 精确求解
    # ------------------------------------------------------------------
    def solve(
        self,
        *,
        reference_order: Optional[Sequence[str]] = None,
        time_limit: float = 5.0,
        node_limit: int = 2_000_000,
    ) -> Plan:
        t_begin = time.perf_counter()
        n = len(self.exams)

        if n == 0:
            return Plan(steps=[], status="EMPTY", elapsed_ms=0.0)

        if self._missing:
            return Plan(
                feasible=False,
                status="INFEASIBLE",
                conflicts=list(dict.fromkeys(self._missing)),
                elapsed_ms=(time.perf_counter() - t_begin) * 1000,
            )

        ref_pos: Dict[int, int] = {}
        if reference_order:
            for pos, code in enumerate(reference_order):
                if code in self._index:
                    ref_pos[self._index[code]] = pos

        best: Dict[str, object] = {"finish": INF, "order": None, "path": None, "missed": 0}
        alt: Dict[str, object] = {"dev": None, "finish": INF, "order": None, "path": None}
        # 备选：优先「不浪费已取到的号」的方案。只有当它不比最快方案慢太多时才采用。
        honor: Dict[str, object] = {"missed": None, "finish": INF, "order": None, "path": None}
        stats = {"nodes": 0, "timeout": False, "node_limit": False}
        deadline = t_begin + time_limit

        finish_of: Dict[int, float] = {}
        path: List[PlanStep] = []
        order: List[int] = []
        remaining: List[int] = list(range(n))

        def lower_bound(now: float, rest: List[int]) -> float:
            """完成时间的可采纳下界。

            (a) 剩余服务时长必须全部走完：now + Σs；
            (b) 对同一科室 D，患者第一次到达时的开始时刻 = max(到达, W_D) ≥ W_D，
                之后每次都要接着上一次做完，故该科室最后一项完成 ≥ W_D + Σ_{i∈D}s_i。
            两者取大。均不会剪掉最优解（random 对拍已验证）。
            """
            total_service = 0.0
            per_dept: Dict[str, float] = {}
            bound = now
            for i in rest:
                exam = self.exams[i]
                s = exam.service_minutes
                total_service += s

                ticket = self.tickets.get(exam.code)
                call = self.estimator.call_offset(exam.department, ticket) if ticket is not None else None
                if call is not None:
                    # 有号的项开始时刻固定，完成不早于 叫号时刻 + 服务时长
                    bound = max(bound, call + s)
                else:
                    per_dept[exam.department] = per_dept.get(exam.department, 0.0) + s

            bound = max(bound, now + total_service)
            for dept, service_sum in per_dept.items():
                value = self.estimator.wait_now(dept) + service_sum
                if value > bound:
                    bound = value
            return bound

        def record(now: float) -> None:
            miss = sum(1 for s in path if s.missed)

            # 记录「浪费号最少」的方案。最终是否采用还要看它比最快方案慢多少，
            # 因为剪枝放宽了 HONOR_SLACK，这里可能收到略慢但有价值的解。
            if (honor["missed"] is None or miss < honor["missed"]        # type: ignore[operator]
                    or (miss == honor["missed"] and now < honor["finish"])):  # type: ignore[operator]
                honor["missed"] = miss
                honor["finish"] = now
                honor["order"] = list(order)
                honor["path"] = list(path)

            if now < best["finish"] - 1e-9:          # type: ignore[operator]
                best["finish"] = now
                best["order"] = list(order)
                best["path"] = list(path)
                best["missed"] = miss
                alt["dev"] = None
                alt["order"] = None
                alt["path"] = None
                return
            if now <= best["finish"] + TIE_WINDOW:   # type: ignore[operator]
                dev = _deviation(order, ref_pos) if ref_pos else 0
                if alt["dev"] is None or dev < alt["dev"]:   # type: ignore[operator]
                    alt["dev"] = dev
                    alt["finish"] = now
                    alt["order"] = list(order)
                    alt["path"] = list(path)

        def dfs(now: float) -> None:
            stats["nodes"] += 1
            nodes = stats["nodes"]
            if nodes % 2048 == 0:
                if time.perf_counter() > deadline:
                    stats["timeout"] = True
                if nodes > node_limit:
                    stats["node_limit"] = True
            if stats["timeout"] or stats["node_limit"]:
                return

            if not remaining:
                record(now)
                return

            # 放宽 HONOR_SLACK：略慢但能保住已取号的方案也要被搜到
            if lower_bound(now, remaining) >= best["finish"] + HONOR_SLACK - 1e-9:   # type: ignore[operator]
                return

            # 已取号却没做完时，只在它们里面挑——用户手里的号是既成事实，
            # 不能为了压缩总时长把它排到后面去作废。
            pool: List[int] = remaining
            if self.tickets_first and self.tickets:
                waiting = [i for i in remaining if self.exams[i].code in self.tickets]
                if waiting:
                    pool = waiting

            cands: List[_Candidate] = []
            for i in pool:
                c = self._candidate(i, now, finish_of)
                if c is not None:
                    cands.append(c)
            # 先探索"最早能开始"的分支，尽早拿到一个较好的上界用于剪枝
            cands.sort(key=lambda c: (c.start, c.finish))

            for c in cands:
                i = c.idx
                finish_of[i] = c.finish
                order.append(i)
                path.append(self._to_step(c, self.exams[i]))
                remaining.remove(i)

                dfs(c.finish)

                remaining.append(i)
                path.pop()
                order.pop()
                del finish_of[i]

                if stats["timeout"] or stats["node_limit"]:
                    return

        dfs(self.start_time)

        chosen: Dict[str, object] = best
        stability_applied = False
        honored = False
        if (honor["order"] is not None and best["order"] is not None
                and honor["missed"] < best["missed"]                                  # type: ignore[operator]
                and honor["finish"] <= best["finish"] + HONOR_SLACK + 1e-9):          # type: ignore[operator]
            chosen = honor
            honored = True
        elif alt["order"] is not None and best["order"] is not None:
            dev_best = _deviation(best["order"], ref_pos) if ref_pos else 0  # type: ignore[arg-type]
            if alt["dev"] is not None and alt["dev"] < dev_best:             # type: ignore[operator]
                chosen = alt
                stability_applied = True

        elapsed_ms = (time.perf_counter() - t_begin) * 1000

        if chosen["order"] is None:
            return Plan(
                feasible=False,
                status="INFEASIBLE",
                conflicts=["在给定约束与时间窗下找不到可行顺序，请检查前置依赖与预约时段"],
                nodes=stats["nodes"],
                elapsed_ms=elapsed_ms,
            )

        status = "OPTIMAL"
        if stats["timeout"]:
            status = "TIME_LIMIT"
        elif stats["node_limit"]:
            status = "NODE_LIMIT"

        plan = Plan(
            steps=list(chosen["path"]),      # type: ignore[arg-type]
            feasible=True,
            status=status,
            nodes=stats["nodes"],
            elapsed_ms=elapsed_ms,
        )
        if honored:
            extra = float(chosen["finish"]) - float(best["finish"])   # type: ignore[arg-type]
            if extra > 0.05:
                plan.notes.append(
                    f"已优先保留取到的号：方案比最快顺序多 {extra:.0f} 分钟，"
                    f"但避免了号作废后重新排队"
                )
            else:
                plan.notes.append("已优先按取到的号安排，避免号作废后重新排队")
        if stability_applied:
            plan.notes.append(
                f"存在完成时间差异不足 {TIE_WINDOW:.0f} 分钟的其他方案，已按"
                f"「减少推荐变动」原则沿用上一次顺序"
            )
        return plan

    # ------------------------------------------------------------------
    # 基线算法（用于对比，方案 §6）
    # ------------------------------------------------------------------
    def medical_order(self, sequence: Sequence[str]) -> Optional[Plan]:
        """基线一：严格按医嘱开立顺序执行（前置未满足时原地等结果）。"""
        plan = self.simulate(sequence)
        if plan is not None:
            plan.status = "BASELINE_MEDICAL_ORDER"
        return plan

    def greedy_shortest_wait(self) -> Optional[Plan]:
        """基线二：规则过滤 + 最短当前等待优先（可解释贪心）。"""
        now = self.start_time
        finish_of: Dict[int, float] = {}
        remaining = set(range(len(self.exams)))
        steps: List[PlanStep] = []

        while remaining:
            cands: List[_Candidate] = []
            for i in remaining:
                c = self._candidate(i, now, finish_of)
                if c is not None:
                    cands.append(c)
            if not cands:
                return None
            c = min(cands, key=lambda x: (x.wait, x.start, x.finish))
            steps.append(self._to_step(c, self.exams[c.idx]))
            finish_of[c.idx] = c.finish
            now = c.finish
            remaining.remove(c.idx)

        return Plan(steps=steps, status="BASELINE_GREEDY")

    # ------------------------------------------------------------------
    # 取号冲突诊断
    # ------------------------------------------------------------------
    def ticket_conflicts(self) -> List[Dict[str, object]]:
        """找出"叫号时间互相重叠"的组合：两处都要人在场，必然有一个过号。"""
        items: List[Dict[str, object]] = []
        for code, ticket in self.tickets.items():
            idx = self._index.get(code)
            if idx is None:
                continue
            exam = self.exams[idx]
            call = self.estimator.call_offset(exam.department, ticket)
            if call is None:
                continue
            items.append({
                "code": code,
                "name": exam.name,
                "department": exam.department,
                "ticket": int(ticket),
                "call": float(call),
                "end": float(call) + exam.service_minutes,
            })

        found: List[Dict[str, object]] = []
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                x, y = items[a], items[b]
                if float(x["call"]) < float(y["end"]) and float(y["call"]) < float(x["end"]):
                    found.append({"a": x, "b": y})
        return found

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def diagnose(self, order: Sequence[str]) -> List[str]:
        """解释某个顺序为何不可行（用于人工兜底提示）。"""
        now = self.start_time
        finish_of: Dict[int, float] = {}
        reasons: List[str] = []

        for code in order:
            idx = self._index[code]
            exam = self.exams[idx]
            release = now
            blocked = None
            for j, lag in self._prereq[idx]:
                if j not in finish_of:
                    blocked = f"前置检查「{self.exams[j].name}」尚未完成"
                    break
                release = max(release, finish_of[j] + lag)
            if blocked:
                reasons.append(f"{exam.name}：{blocked}")
                continue

            arrive = max(now, release)
            wait = self.estimator.estimate_wait_at(exam.department, arrive)
            start = arrive + wait
            window = self._windows.get(idx)
            if window and start > window[1] + 1e-9:
                reasons.append(
                    f"{exam.name}：预计 {start:.0f} 分钟时才能开始，"
                    f"已超出当日开放时间（{window[1] // 60:02d}:{window[1] % 60:02d}）"
                )
                continue

            finish_of[idx] = start + exam.service_minutes
            now = finish_of[idx]

        return reasons
