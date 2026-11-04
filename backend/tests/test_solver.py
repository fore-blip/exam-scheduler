"""求解器正确性与约束测试。

运行：python -m unittest test_solver -v
"""

from __future__ import annotations

import itertools
import random
import unittest

import os
import sys

# 允许直接 `python backend/tests/xxx.py` 运行：把项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.app import rules as rules_mod
from backend.app.estimator import QueueEstimator
from backend.app.models import Dependency, DepartmentState, Exam, TimeWindow
from backend.app.solver import ExamScheduler


def make_scheduler(exams, deps, windows, dept_waits, start=0.0, drain=1.0, completed=None):
    states = [
        DepartmentState(department=dept, queue_length=10, wait_minutes=w)
        for dept, w in dept_waits.items()
    ]
    est = QueueEstimator(states, drain_rate=drain)
    return ExamScheduler(
        exams, deps, windows, est,
        start_time=start, completed=completed or {},
    )


def brute_force(scheduler, codes):
    """穷举所有排列，返回最小完成时间（不可行返回 None）。"""
    best = None
    for perm in itertools.permutations(codes):
        plan = scheduler.simulate(perm)
        if plan is None:
            continue
        if best is None or plan.total_minutes < best:
            best = plan.total_minutes
    return best


class TestAgainstBruteForce(unittest.TestCase):
    """分支限界的结果必须与穷举一致，否则说明剪枝不成立。"""

    def test_random_instances(self):
        rng = random.Random(20261031)
        compared = 0
        for _ in range(120):
            n = rng.randint(3, 5)
            codes = [f"E{i}" for i in range(n)]
            depts = ["甲", "乙", "丙"]
            waits = {d: rng.choice([0, 5, 15, 30, 50]) for d in depts}

            exams = [
                Exam(code=c, name=c, department=rng.choice(depts),
                     service_minutes=rng.choice([5, 10, 20]))
                for c in codes
            ]

            deps = []
            for i in range(n):
                for j in range(i + 1, n):
                    if rng.random() < 0.18:
                        deps.append(Dependency("R", codes[i], codes[j], rng.choice([0, 5, 15])))

            windows = []
            if rng.random() < 0.4:
                exams_pick = rng.choice(codes)
                windows.append(TimeWindow("W", exams_pick, 0, rng.choice([30, 60, 120])))

            sched = make_scheduler(exams, deps, windows, waits)
            plan = sched.solve()

            expected = brute_force(sched, codes)
            if expected is None:
                self.assertFalse(plan.feasible, "穷举无解但求解器给出了方案")
            else:
                self.assertTrue(plan.feasible, "穷举有解但求解器判定不可行")
                self.assertAlmostEqual(
                    plan.total_minutes, expected, places=6,
                    msg=f"求解器 {plan.total_minutes} vs 穷举 {expected}",
                )
                # 结果必须自洽：按该顺序重放，时间应完全一致
                replay = sched.simulate(plan.order())
                self.assertIsNotNone(replay)
                self.assertAlmostEqual(replay.total_minutes, plan.total_minutes, places=6)
            compared += 1

        self.assertEqual(compared, 120)


class TestConstraints(unittest.TestCase):
    def test_dependency_with_lag(self):
        exams = [
            Exam("ECG", "心电图", "心电科", 15),
            Exam("ECHO", "心脏彩超", "超声科", 25),
        ]
        deps = [Dependency("R1", "ECG", "ECHO", 10)]
        sched = make_scheduler(exams, deps, [], {"心电科": 0, "超声科": 0})
        plan = sched.solve()
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.order(), ["ECG", "ECHO"])
        self.assertGreaterEqual(plan.steps[1].start_at, plan.steps[0].finish_at + 10 - 1e-9)

    def test_time_window_forces_priority(self):
        """B 队列更短，但 A 的时间窗即将关闭 —— 必须先做 A。"""
        exams = [
            Exam("A", "A项", "甲", 5),
            Exam("B", "B项", "乙", 20),
        ]
        waits = {"甲": 10, "乙": 5}
        windows = [TimeWindow("W1", "A", 0, 20)]
        sched = make_scheduler(exams, [], windows, waits)
        plan = sched.solve()
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.order(), ["A", "B"], "时间窗约束未被遵守")

    def test_window_infeasible_reported(self):
        exams = [Exam("A", "A项", "甲", 30)]
        windows = [TimeWindow("W1", "A", 0, 10)]
        sched = make_scheduler(exams, [], windows, {"甲": 20})
        plan = sched.solve()
        self.assertFalse(plan.feasible)

    def test_missing_prerequisite_reported(self):
        exams = [Exam("ECHO", "心脏彩超", "超声科", 25)]
        deps = [Dependency("R1", "ECG", "ECHO", 0)]
        sched = make_scheduler(exams, deps, [], {"超声科": 0})
        plan = sched.solve()
        self.assertFalse(plan.feasible)
        self.assertTrue(any("ECG" in c for c in plan.conflicts))

    def test_completed_prerequisite_satisfied(self):
        exams = [Exam("ECHO", "心脏彩超", "超声科", 25)]
        deps = [Dependency("R1", "ECG", "ECHO", 10)]
        sched = make_scheduler(exams, deps, [], {"超声科": 0}, completed={"ECG": -5})
        plan = sched.solve()
        self.assertTrue(plan.feasible)
        self.assertGreaterEqual(plan.steps[0].start_at, 5 - 1e-9)


class TestTicketBlock(unittest.TestCase):
    """已挂号块语义：整块排在最前，块内最优；未挂号块在其后单独最优。"""

    @staticmethod
    def _sched(exams, deps, waits, tickets, block):
        states = [
            DepartmentState(department=d, queue_length=10, wait_minutes=w,
                            serving_no=100, service_minutes=5)
            for d, w in waits.items()
        ]
        return ExamScheduler(
            exams, deps, [], QueueEstimator(states),
            tickets=tickets, ticket_block=block,
        )

    @staticmethod
    def _brute_force_blocked(sched, codes, block):
        """只允许「块内项目全部在前」的排列，穷举最小完成时间。"""
        best = None
        for perm in itertools.permutations(codes):
            seen_rest = False
            valid = True
            for code in perm:
                if code in block:
                    if seen_rest:
                        valid = False
                        break
                else:
                    seen_rest = True
            if not valid:
                continue
            plan = sched.simulate(perm)
            if plan is not None and (best is None or plan.total_minutes < best):
                best = plan.total_minutes
        return best

    def test_single_ticket_goes_first(self):
        exams = [Exam("A", "A项", "甲", 10), Exam("B", "B项", "乙", 5), Exam("C", "C项", "丙", 20)]
        sched = self._sched(exams, [], {"甲": 30, "乙": 0, "丙": 10},
                            tickets={"C": 120}, block=["C"])
        plan = sched.solve()
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.order()[0], "C")

    def test_multiple_tickets_form_one_prefix(self):
        exams = [Exam(c, c, d, 5) for c, d in
                 [("A", "甲"), ("B", "乙"), ("C", "丙"), ("D", "丁"), ("E", "甲")]]
        waits = {"甲": 20, "乙": 5, "丙": 30, "丁": 10}
        tickets = {"C": 130, "D": 140}
        sched = self._sched(exams, [], waits, tickets, ["C", "D"])
        plan = sched.solve()

        self.assertTrue(plan.feasible)
        self.assertEqual(set(plan.order()[:2]), {"C", "D"}, "已挂号项没有全部排在前面")
        # 块内顺序 + 剩余顺序必须与「限定块在前」的穷举一致
        self.assertAlmostEqual(
            plan.total_minutes,
            self._brute_force_blocked(sched, [e.code for e in exams], {"C", "D"}),
            places=6,
        )

    def test_demoted_ticket_stays_out_of_block(self):
        """号还在，但被 business 层剔出块：仍按号算，只是不占块内位置。"""
        exams = [Exam("A", "A项", "甲", 10), Exam("B", "B项", "乙", 5)]
        sched = self._sched(exams, [], {"甲": 10, "乙": 0},
                            tickets={"A": 110, "B": 130}, block=["B"])
        plan = sched.solve()
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.order()[0], "B")

    def test_split_block_demotes_dependent(self):
        from backend.app.service import split_ticket_block

        exams = [
            Exam("ECG", "心电图", "心电科", 15),
            Exam("ECHO", "心脏彩超", "超声科", 25),
            Exam("CBC", "血常规", "检验科", 5),
        ]
        deps = [Dependency("R1", "ECG", "ECHO", 10)]

        block, demoted = split_ticket_block(exams, deps, {"ECHO": 30, "CBC": 40}, {})
        self.assertEqual(block, ["CBC"])
        self.assertEqual(demoted, [("ECHO", ["ECG"])])

        block, demoted = split_ticket_block(exams, deps, {"ECHO": 30, "ECG": 40}, {})
        self.assertEqual(block, ["ECG", "ECHO"])
        self.assertEqual(demoted, [])

        # 前置已完成时不算缺
        block, _ = split_ticket_block(exams, deps, {"ECHO": 30}, {"ECG": -5})
        self.assertEqual(block, ["ECHO"])


class TestGreedyCanFail(unittest.TestCase):
    def test_greedy_hits_window_optimal_does_not(self):
        """构造反例：最短等待优先会踩时间窗，最优解不会。"""
        exams = [
            Exam("A", "A项", "甲", 5),
            Exam("B", "B项", "乙", 20),
        ]
        waits = {"甲": 10, "乙": 5}
        windows = [TimeWindow("W1", "A", 0, 20)]
        sched = make_scheduler(exams, [], windows, waits)

        greedy = sched.greedy_shortest_wait()
        optimal = sched.solve()

        self.assertIsNone(greedy, "该算例下贪心本应因时间窗失效")
        self.assertTrue(optimal.feasible)
        self.assertEqual(optimal.order(), ["A", "B"])


class TestQueueModel(unittest.TestCase):
    def test_wait_decays_linearly(self):
        est = QueueEstimator([DepartmentState("甲", 10, 40)], drain_rate=1.0)
        self.assertAlmostEqual(est.wait_at("甲", 0), 40)
        self.assertAlmostEqual(est.wait_at("甲", 15), 25)
        self.assertAlmostEqual(est.wait_at("甲", 100), 0)

    def test_conservative_mode_never_decays(self):
        est = QueueEstimator([DepartmentState("甲", 10, 40)], drain_rate=0.0)
        self.assertAlmostEqual(est.wait_at("甲", 0), 40)
        self.assertAlmostEqual(est.wait_at("甲", 60), 40)

    def test_missing_department_raises(self):
        est = QueueEstimator([DepartmentState("甲", 10, 40)])
        with self.assertRaises(KeyError):
            est.wait_at("乙", 0)


class TestRulesLibrary(unittest.TestCase):
    def test_bundled_rules_are_valid(self):
        self.assertEqual(rules_mod.validate(), [])

    def test_cycle_detected(self):
        edges = {"A": ["B"], "B": ["C"], "C": ["A"]}
        cycle = rules_mod.find_cycle(edges)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1])

    def test_catalog_and_windows_build(self):
        catalog = rules_mod.build_catalog()
        self.assertIn("CBC", catalog)
        windows = rules_mod.build_windows()
        for w in windows:
            self.assertLess(w.open_minute, w.close_minute)


if __name__ == "__main__":
    unittest.main(verbosity=2)
