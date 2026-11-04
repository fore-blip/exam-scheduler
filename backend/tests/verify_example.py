"""独立验证脚本：不复用 solver 的任何计算逻辑。

思路：用一套**重新手写**的模拟器穷举全部排列，与求解器结果逐项对照。
如果两边一致，说明"搜索没有漏解"；如果逐项时间也对得上，说明"模拟公式没有算错"。

运行：python verify_example.py
"""

from __future__ import annotations

import itertools
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import os
import sys

# 允许直接 `python backend/tests/xxx.py` 运行：把项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.app import data_source
from backend.app import rules as rules_mod
from backend.app import service
from backend.app.estimator import QueueEstimator
from backend.app.models import DepartmentState, Dependency, Exam, TimeWindow
from backend.app.solver import ExamScheduler


# ----------------------------------------------------------------------
# 排版
# ----------------------------------------------------------------------
def _w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _w(text))
    return (" " * gap + str(text)) if align == "right" else (str(text) + " " * gap)


def _clock(base: datetime, offset: float) -> str:
    return (base + timedelta(minutes=offset)).strftime("%H:%M")


# ----------------------------------------------------------------------
# 独立实现的模拟器（与 solver._candidate 无关，按公式重新写）
# ----------------------------------------------------------------------
def simulate(order, depts, svc, waits, deps, windows, drain=1.0, start=0.0,
             travel=0.0, buffer=1.0):
    now = start
    done: Dict[str, float] = {}
    rows = []
    prev_dept = None

    for code in order:
        dept = depts[code]
        # 换科室的移动耗时：同科室连续两项视为一次到场
        move = travel if (prev_dept is not None and prev_dept != dept) else 0.0

        release = now + move
        for before, lag in deps.get(code, []):
            if before not in done:
                return None
            release = max(release, done[before] + lag)

        arrive = max(now + move, release)
        if code in windows and arrive < windows[code][0]:
            arrive = windows[code][0]

        # 未取号：先按队列消退算剩余等待，再整体上浮安全系数
        wait = max(0.0, waits[dept] - drain * arrive) * buffer
        begin = arrive + wait

        if code in windows and begin > windows[code][1] + 1e-9:
            return None

        finish = begin + svc[code]
        done[code] = finish
        rows.append((code, arrive, wait, begin, finish, move))
        now = finish
        prev_dept = dept

    return rows


def brute_force(codes, depts, svc, waits, deps, windows, drain=1.0):
    best_total = None
    best_order = None
    feasible_count = 0
    for perm in itertools.permutations(codes):
        rows = simulate(perm, depts, svc, waits, deps, windows, drain,
                        travel=rules_mod.TRAVEL_MINUTES,
                        buffer=rules_mod.ESTIMATE_BUFFER)
        if rows is None:
            continue
        feasible_count += 1
        total = rows[-1][4]
        if best_total is None or total < best_total - 1e-9:
            best_total = total
            best_order = perm
    return best_total, best_order, feasible_count


# ----------------------------------------------------------------------
# 从真实配置里取出算例
# ----------------------------------------------------------------------
def load_default_instance():
    now = data_source.SNAPSHOT_TIME
    states = data_source.get_snapshot(now)
    catalog = rules_mod.build_catalog()
    codes = data_source.get_patient_request()["exam_codes"]

    depts = {c: catalog[c].department for c in codes}
    svc = {c: catalog[c].service_minutes for c in codes}
    names = {c: catalog[c].name for c in codes}
    waits = {d: states[d].wait_minutes for d in {depts[c] for c in codes}}

    deps: Dict[str, List[Tuple[str, float]]] = {}
    for d in rules_mod.build_dependencies():
        if d.before in codes and d.after in codes:
            deps.setdefault(d.after, []).append((d.before, d.lag_minutes))

    now_min = now.hour * 60 + now.minute
    windows = {
        w.exam: (w.open_minute - now_min, w.close_minute - now_min)
        for w in rules_mod.build_windows()
        if w.exam in codes
    }
    return now, states, codes, depts, svc, names, waits, deps, windows


# ----------------------------------------------------------------------
# 验证一个算例
# ----------------------------------------------------------------------
def verify(
    title: str,
    now: datetime,
    codes: Sequence[str],
    depts: Dict[str, str],
    svc: Dict[str, float],
    names: Dict[str, str],
    waits: Dict[str, float],
    deps: Dict[str, List[Tuple[str, float]]],
    windows: Dict[str, Tuple[float, float]],
    plan,
) -> bool:
    print("=" * 78)
    print(f" {title}")
    print("=" * 78)

    print("\n[1] 输入")
    print(f"  快照时刻 {now:%H:%M}   队列：" +
          "，".join(f"{d} {w:.0f}分" for d, w in sorted(waits.items(), key=lambda x: -x[1])))
    print(f"  待检 {len(codes)} 项：" +
          "，".join(f"{names[c]}({svc[c]:.0f}分)" for c in codes))
    if deps:
        print("  依赖：" + "；".join(
            f"{names[b]}→{names[c]}(间隔{lag:.0f}分)" for c, lst in deps.items() for b, lag in lst))
    if windows:
        print("  时间窗：" + "；".join(
            f"{names[c]} {_clock(now, v[0])}–{_clock(now, v[1])}"
            for c, v in windows.items()))

    # ---- 独立穷举 ----
    total_bf, order_bf, feasible = brute_force(codes, depts, svc, waits, deps, windows)
    total_solver = plan.total_minutes

    print("\n[2] 独立穷举 vs 求解器")
    print(f"  全排列 {len(list(itertools.permutations(codes)))} 种，其中可行 {feasible} 种")
    print(f"  穷举最优：{total_bf:.0f} 分钟   顺序 {' → '.join(names[c] for c in order_bf)}")
    print(f"  求解器  ：{total_solver:.0f} 分钟   顺序 {' → '.join(names[c] for c in plan.order())}")
    ok_total = abs(total_bf - total_solver) < 0.05
    same_order = list(order_bf) == list(plan.order())
    if ok_total and not same_order:
        print("  ✓ 总时长一致（顺序不同，属多解，不影响最优性）")
    else:
        print(f"  {'✓ 一致' if ok_total else '× 不一致'}")

    # ---- 逐项重算 ----
    print("\n[3] 逐项重算（用独立模拟器复现求解器的输出）")
    rows = simulate(plan.order(), depts, svc, waits, deps, windows,
                    travel=rules_mod.TRAVEL_MINUTES,
                    buffer=rules_mod.ESTIMATE_BUFFER)
    header = (f"  {'#':<4}{_pad('检查', 14)}{_pad('科室', 10)}"
              f"{_pad('到达', 8, 'right')}{_pad('等待', 8, 'right')}"
              f"{_pad('开始', 8, 'right')}{_pad('结束', 8, 'right')}   重算校验")
    print(header)
    print("  " + "-" * 74)
    all_match = True
    for k, (step, row) in enumerate(zip(plan.steps, rows), 1):
        code, arrive, wait, begin, finish, _move = row
        match = (
            abs(step.arrive_at - arrive) < 1e-6
            and abs(step.wait_minutes - wait) < 1e-6
            and abs(step.start_at - begin) < 1e-6
            and abs(step.finish_at - finish) < 1e-6
        )
        if not match:
            print(f"      求解器 arrive={step.arrive_at:.3f} wait={step.wait_minutes:.3f} "
                  f"start={step.start_at:.3f} | 重算 arrive={arrive:.3f} wait={wait:.3f} start={begin:.3f}")
        all_match = all_match and match
        print(
            f"  {k:<4}{_pad(names[code], 14)}{_pad(depts[code], 10)}"
            f"{_pad(_clock(now, arrive), 8, 'right')}"
            f"{_pad(f'{wait:.0f}', 8, 'right')}"
            f"{_pad(_clock(now, begin), 8, 'right')}"
            f"{_pad(_clock(now, finish), 8, 'right')}"
            f"   {'✓' if match else '×'}"
        )
    print("  " + "-" * 74)
    print(f"  合计 {total_solver:.0f} 分钟（{_clock(now, 0)} → {_clock(now, total_solver)}）")
    ok_rows = all_match and abs(rows[-1][4] - total_solver) < 1e-6

    # ---- 约束核验 ----
    print("\n[4] 约束核验")
    finish_of = {row[0]: row[4] for row in rows}
    start_of = {row[0]: row[3] for row in rows}
    ok_constraints = True

    for code, lst in deps.items():
        for before, lag in lst:
            need = finish_of[before] + lag
            got = start_of[code]
            good = got >= need - 1e-9
            ok_constraints = ok_constraints and good
            print(f"  {'✓' if good else '×'} {names[before]} 结束 {finish_of[before]:.0f} "
                  f"+ 间隔 {lag:.0f} = {need:.0f} ≤ {names[code]} 开始 {got:.0f}")

    for code, (op, cl) in windows.items():
        got = start_of[code]
        good = op - 1e-9 <= got <= cl + 1e-9
        ok_constraints = ok_constraints and good
        print(f"  {'✓' if good else '×'} {names[code]} 开始 {got:.0f} 落在时间窗 "
              f"[{op:.0f}, {cl:.0f}] 内")

    for k in range(1, len(rows)):
        prev_finish = rows[k - 1][4]
        arrive = rows[k][1]
        good = arrive >= prev_finish - 1e-9
        ok_constraints = ok_constraints and good
        print(f"  {'✓' if good else '×'} 串行：{names[rows[k][0]]} 到达 {arrive:.0f} ≥ "
              f"上一项结束 {prev_finish:.0f}")

    # ---- 下界证书 ----
    print("\n[5] 最优性说明")
    total_service = sum(svc.values())
    per_dept: Dict[str, float] = {}
    for c in codes:
        per_dept[depts[c]] = per_dept.get(depts[c], 0.0) + svc[c]
    lb = total_service
    detail = [f"Σ服务={total_service:.0f}"]
    for d, s in per_dept.items():
        v = waits[d] + s
        detail.append(f"{d}: {waits[d]:.0f}+{s:.0f}={v:.0f}")
        lb = max(lb, v)
    print(f"  下界 = max({', '.join(detail)}) = {lb:.0f} 分钟")
    print(f"  求解结果 {total_solver:.0f} 分钟" +
          ("，达到下界 → 已证明最优" if abs(total_solver - lb) < 1e-6
           else "，高于下界；由穷举确认最优"))

    ok = ok_total and ok_rows and ok_constraints
    print(f"\n  结论：{'✓ 全部通过' if ok else '× 存在问题'}\n")
    return ok


# ----------------------------------------------------------------------
# 场景二：时间窗咬合 + 结果等待，检验约束分支
# ----------------------------------------------------------------------
def scenario_two() -> bool:
    now = datetime(2026, 10, 31, 9, 0)
    codes = ["ECG", "ECHO", "CBC", "US_ABD"]
    depts = {"ECG": "心电科", "ECHO": "超声科", "CBC": "检验科", "US_ABD": "超声科"}
    svc = {"ECG": 10.0, "ECHO": 20.0, "CBC": 5.0, "US_ABD": 15.0}
    names = {"ECG": "心电图", "ECHO": "心脏彩超", "CBC": "血常规", "US_ABD": "腹部B超"}
    waits = {"心电科": 5.0, "超声科": 50.0, "检验科": 30.0}
    deps = {"ECHO": [("ECG", 15.0)]}
    windows = {"US_ABD": (-60.0, 60.0)}   # 10:00 关闭

    states = [DepartmentState(d, 10, w) for d, w in waits.items()]
    est = QueueEstimator(states, buffer=rules_mod.ESTIMATE_BUFFER)
    sched = ExamScheduler(
        [Exam(c, names[c], depts[c], svc[c]) for c in codes],
        [Dependency("R1", "ECG", "ECHO", 15.0)],
        [TimeWindow("W1", "US_ABD", -60, 60)],
        est,
        travel_minutes=rules_mod.TRAVEL_MINUTES,
    )
    plan = sched.solve()
    return verify("场景二：时间窗 + 结果间隔（09:00 快照）",
                  now, codes, depts, svc, names, waits, deps, windows, plan)


def main() -> None:
    now, states, codes, depts, svc, names, waits, deps, windows = load_default_instance()
    plan = service.recommend(
        codes,
        now=now,
        states=states,
        fasting=bool(data_source.get_patient_request().get("is_fasting", True)),
    )
    ok1 = verify("场景一：默认算例（6 项检查，720 种排列）",
                 now, codes, depts, svc, names, waits, deps, windows, plan)
    print()
    ok2 = scenario_two()
    print("=" * 78)
    print(f" 总体：{'✓ 两个场景全部通过' if ok1 and ok2 else '× 存在未通过项'}")
    print("=" * 78)


if __name__ == "__main__":
    main()
