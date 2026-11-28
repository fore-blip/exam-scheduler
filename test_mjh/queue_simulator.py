"""测试队列和各科室独立的叫号时钟。"""

import random
import time
from datetime import datetime
from threading import Lock

from simulator import queue_mock


class QueueSimulator:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = Lock()
        self.ticks = 0
        self.departments = {name: dict(dept) for name, dept in queue_mock.DEPARTMENTS.items()}
        first_call = self.clock() + queue_mock.CALL_INTERVAL
        self.next_call = dict.fromkeys(self.departments, first_call)

    def tick_once(self):
        with self.lock:
            self.ticks += 1
            now = self.clock()
            for name, dept in self.departments.items():
                self._advance(name, dept, now)
                pull = (dept["base"] - dept["queue"]) * 0.25
                arrivals = max(0, min(6, round(pull + random.gauss(0, 1.0))))
                dept["queue"] = min(dept["cap"], dept["queue"] + arrivals)
                dept["extra"] = max(0.0, round(dept["extra"] - 1.5, 1))

    def _advance(self, name, dept, now):
        deadline = self.next_call[name]
        if now >= deadline:
            intervals = 1 + int((now - deadline) // queue_mock.CALL_INTERVAL)
            self.next_call[name] = deadline + intervals * queue_mock.CALL_INTERVAL
            called = min(intervals, dept["queue"])
            dept["queue"] -= called
            dept["serving"] += called

    def call_next(self, name):
        with self.lock:
            dept = self.departments.get(name)
            if dept is None:
                raise ValueError("未知科室")
            now = self.clock()
            self._advance(name, dept, now)
            if not dept["queue"]:
                raise ValueError("当前无人排队")
            dept["serving"] += 1
            dept["queue"] -= 1
            self.next_call[name] = now + queue_mock.CALL_INTERVAL
            return {"serving_no": dept["serving"], "queue_length": dept["queue"]}

    def snapshot(self):
        with self.lock:
            now = self.clock()
            departments = []
            for name, dept in self.departments.items():
                self._advance(name, dept, now)
                departments.append({
                    "department": name,
                    "queue_length": dept["queue"],
                    "serving_no": dept["serving"],
                    "wait_minutes": queue_mock.wait_of(dept),
                    "next_call_in_seconds": round(self.next_call[name] - now, 1),
                })
            return {
                "snapshot_time": datetime.now().isoformat(timespec="seconds"),
                "tick": self.ticks,
                "departments": departments,
            }
