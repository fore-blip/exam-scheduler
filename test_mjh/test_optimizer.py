import importlib.util
import unittest

from test_mjh.candidates import evaluate_sequences
from test_mjh.optimizer import optimize_candidates, timeline
from test_mjh.queue_simulator import QueueSimulator


class OptimizerTests(unittest.TestCase):
    def test_timeline_includes_travel_and_remaining_queue(self):
        exams = {
            "A": {"name": "A", "department": "first", "service_minutes": 5},
            "B": {"name": "B", "department": "second", "service_minutes": 10},
        }
        route = timeline(["A", "B"], exams, {"first": 0, "second": 600})
        self.assertEqual(route["steps"][1]["arrival"], 540)
        self.assertEqual(route["steps"][1]["wait"], 69)
        self.assertEqual(route["total_seconds"], 1209)

    @unittest.skipUnless(importlib.util.find_spec("ortools"), "需要 OR-Tools")
    def test_cp_sat_matches_exhaustive_routes(self):
        orders = ["EX05", "EX01", "EX06", "EX18"]
        candidates = evaluate_sequences(orders, {"S01": True, "S02": True}, set())
        snapshot = QueueSimulator().snapshot()
        data = optimize_candidates(candidates, snapshot)["optimization"]

        self.assertEqual(data["status"], "OPTIMAL")
        self.assertEqual(len(data["routes"]), 4)
        self.assertEqual(data["route"]["total_seconds"], min(route["total_seconds"] for route in data["routes"]))
        self.assertIn(data["route"]["order"], candidates["sequences"])

    @unittest.skipUnless(importlib.util.find_spec("ortools"), "需要 OR-Tools")
    def test_cp_sat_search_is_independent_of_the_displayed_orders(self):
        orders = ["EX01", "EX06", "EX05", "EX18"]
        candidates = evaluate_sequences(orders, {"S01": True, "S02": True}, set())
        snapshot = QueueSimulator().snapshot()
        full = optimize_candidates(candidates, snapshot)["optimization"]
        worse = full["routes"][-1]
        self.assertGreater(worse["total_seconds"], full["route"]["total_seconds"])
        candidates["sequences"] = [worse["order"]]
        result = optimize_candidates(candidates, snapshot)["optimization"]
        self.assertEqual(result["route"]["total_seconds"], full["route"]["total_seconds"])
