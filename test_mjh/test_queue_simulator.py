import unittest

from simulator.queue_mock import CALL_INTERVAL
from test_mjh.queue_simulator import QueueSimulator


class QueueSimulatorTests(unittest.TestCase):
    def test_manual_call_restarts_only_selected_department_clock(self):
        now = 0.0
        simulator = QueueSimulator(clock=lambda: now)

        def snapshot():
            return {row["department"]: row for row in simulator.snapshot()["departments"]}

        first, second = list(snapshot())[:2]
        now = CALL_INTERVAL / 2
        simulator.call_next(first)
        manual = simulator.call_next(first)["serving_no"]
        self.assertEqual(snapshot()[first]["next_call_in_seconds"], CALL_INTERVAL)
        self.assertEqual(snapshot()[second]["next_call_in_seconds"], CALL_INTERVAL / 2)

        now = CALL_INTERVAL
        rows = snapshot()
        self.assertEqual(rows[first]["serving_no"], manual)
        self.assertGreater(rows[second]["serving_no"], 1)

        now += CALL_INTERVAL / 2
        self.assertEqual(snapshot()[first]["serving_no"], manual + 1)
