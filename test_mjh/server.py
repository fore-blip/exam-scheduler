"""独立候选集测试服务。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

from simulator import queue_mock

from .candidates import catalog_payload, evaluate_sequences
from .optimizer import optimize_candidates
from .queue_simulator import QueueSimulator


FRONTEND = Path(__file__).resolve().parent / "frontend"
PAGE = ("candidates.html", "text/html")
ASSETS = {
    "/": PAGE,
    "/candidates": PAGE,
    "/candidates/": PAGE,
    "/candidates.css": ("candidates.css", "text/css"),
    "/candidates.js": ("candidates.js", "application/javascript"),
    "/optimize": PAGE,
    "/optimize/": PAGE,
    "/queues": ("queues.html", "text/html"),
    "/queues/": ("queues.html", "text/html"),
    "/queues.js": ("queues.js", "application/javascript"),
}


def evaluate_payload(body: object) -> dict:
    if not isinstance(body, dict):
        raise ValueError("请求体应为对象")
    orders = body.get("orders")
    states = body.get("states", {})
    confirmed = body.get("confirmed", [])
    if not isinstance(orders, list) or not orders or not all(isinstance(code, str) for code in orders):
        raise ValueError("请填写至少一项医嘱编码")
    if not isinstance(states, dict) or any(
        value is not None and not isinstance(value, bool) for value in states.values()
    ):
        raise ValueError("生理状态须为已满足、未满足或待确认")
    if not isinstance(confirmed, list) or not all(isinstance(code, str) for code in confirmed):
        raise ValueError("前置确认须为规则编码列表")
    return evaluate_sequences(orders, states, set(confirmed))


class Handler(BaseHTTPRequestHandler):
    def send_content(self, status: int, body: bytes, media_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, data: dict) -> None:
        self.send_content(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/queues":
            self.send_json(200, self.server.queue_simulator.snapshot())
            return
        if route == "/api/candidates/catalog":
            try:
                self.send_json(200, catalog_payload())
            except sqlite3.Error:
                self.send_json(500, {"error": "无法读取候选集规则库"})
            return
        if route in ASSETS:
            filename, media_type = ASSETS[route]
            self.send_content(200, (FRONTEND / filename).read_bytes(), media_type)
            return
        self.send_json(404, {"error": "未找到该路径"})

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route not in ("/api/candidates", "/api/optimize", "/api/queues/call-next"):
            self.send_json(404, {"error": "未找到该路径"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("请求体长度无效")
            body = json.loads(self.rfile.read(length))
            if route == "/api/queues/call-next":
                department = body.get("department") if isinstance(body, dict) else None
                if not isinstance(department, str):
                    raise ValueError("请指定科室")
                self.send_json(200, self.server.queue_simulator.call_next(department))
            else:
                result = evaluate_payload(body)
                if route == "/api/optimize":
                    result = optimize_candidates(result, self.server.queue_simulator.snapshot())
                self.send_json(200, result)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "请求体不是合法 JSON"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except sqlite3.Error:
            self.send_json(500, {"error": "无法读取候选集规则库"})
        except ModuleNotFoundError:
            self.send_json(503, {"error": "当前环境缺少 CP-SAT 依赖，请安装 test_mjh/requirements.txt。"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.queue_simulator = QueueSimulator()
        stop = Event()

        def simulate_queue() -> None:
            while not stop.wait(queue_mock.TICK_SECONDS):
                server.queue_simulator.tick_once()

        simulator = Thread(target=simulate_queue, daemon=True)
        simulator.start()
        print(f"候选集测试页：http://127.0.0.1:{server.server_port}/", flush=True)
        print(f"实时模拟队列：http://127.0.0.1:{server.server_port}/queues", flush=True)
        print(f"CP-SAT 优化页：http://127.0.0.1:{server.server_port}/optimize", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            simulator.join()


if __name__ == "__main__":
    main()
