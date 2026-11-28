import unittest
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_mjh.candidate_demo import evaluate_candidates
from test_mjh.candidates import catalog_payload, enumerate_sequences, evaluate_sequences, rejected_sequences


class CandidateTests(unittest.TestCase):
    def test_sleep_is_requested_for_sleep_exams(self):
        catalog = catalog_payload()
        self.assertEqual(len(catalog["conditions"]), 9)
        links = {(entry["exam_id"], entry["condition_id"]) for entry in catalog["required_conditions"]}
        self.assertIn(("EX33", "S05"), links)
        self.assertIn(("EX37", "S05"), links)
        self.assertNotIn(("EX01", "S01"), links)
        orders = ["EX33", "EX37"]
        self.assertEqual(evaluate_sequences(orders, {}, set())["sequences"], [])
        self.assertEqual(len(evaluate_sequences(orders, {"S05": True}, set())["sequences"]), 2)

    def test_screening_reports_eligible_blocked_and_unknown(self):
        orders = ["EX01", "EX05", "EX18", "EX38"]
        results = evaluate_candidates(orders, {"S01": False}, set())

        self.assertEqual(
            [result["status"] for result in results],
            ["当前可选", "暂不可选", "待确认", "暂不可选"],
        )
        self.assertIn("D07", results[3]["reasons"][0])

    def test_verified_state_and_prerequisite_allow_candidate(self):
        results = evaluate_candidates(["EX18", "EX38"], {"S02": True}, {"D07"})

        self.assertEqual(
            [result["status"] for result in results],
            ["当前可选", "当前可选"],
        )

    def test_unknown_exam_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知医嘱编码"):
            evaluate_candidates(["EX99"], {}, set())

    def test_sequences_include_every_order_once(self):
        orders = ["EX01", "EX06", "EX05"]
        with patch("test_mjh.candidates.load_config_precedence", return_value=[]):
            data = evaluate_sequences(orders, {"S01": True}, set())
            blocked = evaluate_sequences(orders, {"S01": False}, set())
        self.assertEqual(len(data["sequences"]), 6)
        self.assertTrue(all(sorted(sequence) == sorted(orders) for sequence in data["sequences"]))
        self.assertEqual(blocked["sequences"], [["EX01", "EX06"], ["EX06", "EX01"]])
        self.assertEqual(blocked["deferred_count"], 1)
        self.assertTrue(blocked["results"][2]["suggestions"])
        self.assertEqual(len(blocked["rejected"]), 6)
        self.assertTrue(all("空腹" in entry["reasons"][0] for entry in blocked["rejected"]))
        self.assertEqual(data["rejected"], [])

    def test_sequences_exclude_reversed_dependency_and_detect_cycle(self):
        edge = {"before_exam_id": "A", "after_exam_id": "B"}
        self.assertEqual(enumerate_sequences(["A", "B", "C"], [edge], 10), [
            ["A", "B", "C"], ["A", "C", "B"], ["C", "A", "B"],
        ])
        reverse = {"before_exam_id": "B", "after_exam_id": "A"}
        self.assertEqual(enumerate_sequences(["A", "B", "C"], [edge, reverse], 10), [])

    def test_rejected_sequences_explain_each_reversed_rule(self):
        edges = [
            {"rule_id": "TEST-1", "before_exam_id": "A", "after_exam_id": "B"},
            {"rule_id": "TEST-2", "before_exam_id": "B", "after_exam_id": "C"},
        ]
        rejected = rejected_sequences(["A", "B", "C"], edges, [], 10)
        by_order = {tuple(entry["order"]): entry["reasons"] for entry in rejected}
        self.assertEqual(len(rejected), 5)
        self.assertEqual(len(by_order), 5)
        self.assertNotIn(("A", "B", "C"), by_order)
        self.assertEqual(len(by_order[("C", "B", "A")]), 2)

    def test_deferred_prerequisite_also_defers_dependent(self):
        edges = [
            {"rule_id": "TEST-1", "before_exam_id": "EX05", "after_exam_id": "EX01"},
            {"rule_id": "TEST-2", "before_exam_id": "EX01", "after_exam_id": "EX06"},
        ]
        catalog = {"prerequisites": edges, "exam_precedence": [], "exams": []}
        with patch("test_mjh.candidates.catalog_payload", return_value=catalog), patch(
            "test_mjh.candidates.load_config_precedence", return_value=[]
        ):
            data = evaluate_sequences(["EX05", "EX01", "EX06", "EX18"], {"S01": False, "S02": True}, set())
        self.assertEqual(data["sequences"], [["EX18"]])
        self.assertEqual(data["deferred_count"], 3)
        self.assertIn("TEST-2", data["results"][2]["reasons"][0])

    def test_project_precedence_from_sqlite_filters_orders_and_exposes_gap(self):
        sql = Path("backend/config/medical_rules.sql").read_text(encoding="utf-8")
        with TemporaryDirectory() as folder:
            database = Path(folder) / "rules.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript(sql)
                db.execute(
                    "INSERT INTO exam_precedence_rules VALUES (?, ?, ?, ?, ?)",
                    ("EP-TEST", "EX01", "EX06", 10, 1),
                )
            with patch("test_mjh.candidates.DATABASE", database):
                data = evaluate_sequences(["EX06", "EX01"], {}, set())
                missing = evaluate_sequences(["EX06"], {}, set())
                confirmed = evaluate_sequences(["EX06"], {}, {"EP-TEST"})

        self.assertEqual(data["sequences"], [["EX01", "EX06"]])
        self.assertEqual(data["edges"][0]["recommended_gap_minutes"], 10)
        self.assertIn("EP-TEST", data["rejected"][0]["reasons"][0])
        self.assertEqual(missing["sequences"], [])
        self.assertEqual(confirmed["sequences"], [["EX06"]])

    def test_json_precedence_defines_a_chain(self):
        orders = ["EX05", "EX06", "EX01"]
        data = evaluate_sequences(orders, {"S01": True}, set())
        blocked = evaluate_sequences(orders, {"S01": False}, set())

        self.assertEqual(data["sequences"], [["EX01", "EX06", "EX05"]])
        self.assertEqual(blocked["sequences"], [["EX01", "EX06"]])
        self.assertEqual(len(data["rejected"]), 5)
        self.assertEqual([edge["recommended_gap_minutes"] for edge in data["edges"]], [10, 5])
        self.assertTrue(all(edge["note"] for edge in data["edges"]))
