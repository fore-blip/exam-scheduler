"""本地演示服务器。

只用 Python 标准库，不依赖 Flask / FastAPI，避免装包。
算法仍由 service.py 提供，规则库依然只有 rules.py 一处。

启动：
    python webapp.py
然后浏览器打开终端里打印的地址（默认 http://127.0.0.1:8765）。
"""

from __future__ import annotations

import argparse
import json
import os
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

from . import data_source
from . import rules as rules_mod
from . import service

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(BASE_DIR, "frontend")


def _hhmm(minute: int) -> str:
    minute = int(minute) % (24 * 60)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def build_snapshot_payload() -> Dict[str, Any]:
    """只返回队列快照，供前端做高频轮询（比整份 catalog 轻）。"""
    states = data_source.get_snapshot(data_source.SNAPSHOT_TIME)
    status = data_source.last_status()
    return {
        "snapshot_time": _iso(data_source.SNAPSHOT_TIME),
        "server_time": datetime.now().strftime("%H:%M:%S"),
        "status": status,
        "departments": [
            {
                "name": dept,
                "queue_length": st.queue_length,
                "wait_minutes": st.wait_minutes,
                "serving_no": st.serving_no,
                "service_minutes": st.service_minutes,
            }
            for dept, st in sorted(states.items(), key=lambda kv: -kv[1].wait_minutes)
        ],
    }


def build_catalog_payload() -> Dict[str, Any]:
    catalog = rules_mod.build_catalog()
    now = data_source.SNAPSHOT_TIME
    states = data_source.get_snapshot(now)
    deps = rules_mod.build_dependencies()
    windows = rules_mod.build_windows()
    request = data_source.get_patient_request()

    return {
        "snapshot_time": _iso(now),
        "patient_id": request.get("patient_id", "-"),
        "default_codes": list(request.get("exam_codes", [])),
        "queue_status": data_source.last_status(),
        "departments": [
            {
                "name": dept,
                "queue_length": st.queue_length,
                "wait_minutes": st.wait_minutes,
                "serving_no": st.serving_no,
                "service_minutes": st.service_minutes,
            }
            for dept, st in sorted(states.items(), key=lambda kv: -kv[1].wait_minutes)
        ],
        "exams": [
            {
                "code": e.code,
                "name": e.name,
                "department": e.department,
                "service_minutes": e.service_minutes,
                "requires_fasting": e.requires_fasting,
                "note": e.note,
                "requires": [
                    {"code": d.before, "lag": d.lag_minutes, "reason": d.reason}
                    for d in deps
                    if d.after == e.code
                ],
                "window": next(
                    (
                        {
                            "open": _hhmm(w.open_minute),
                            "close": _hhmm(w.close_minute),
                            "reason": w.reason,
                        }
                        for w in windows
                        if w.exam == e.code
                    ),
                    None,
                ),
            }
            for e in catalog.values()
        ],
    }


def build_recommend_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    codes = [str(c) for c in (body.get("codes") or [])]
    fasting = bool(body.get("fasting", True))
    tickets = {str(k): int(v) for k, v in (body.get("tickets") or {}).items()
               if v not in (None, "")}
    now = data_source.SNAPSHOT_TIME
    states = data_source.get_snapshot(now)

    plan = service.recommend(codes, now=now, states=states, fasting=fasting, tickets=tickets)

    comparison: Dict[str, Any] = {}
    if plan.feasible:
        raw = service.compare_baselines(
            codes, medical_order=codes, now=now, states=states, fasting=fasting,
            tickets=tickets,
        )
        for name, other in raw.items():
            comparison[name] = (
                None if other is None or not other.feasible else round(other.total_minutes, 1)
            )

    reasons = service.explain_steps(plan, states) if plan.feasible else []

    return {
        "feasible": plan.feasible,
        "status": plan.status,
        "snapshot_time": _iso(now),
        "total_minutes": round(plan.total_minutes, 1),
        "total_wait": round(plan.total_wait, 1),
        "total_idle": round(plan.total_idle, 1),
        "order": plan.order(),
        "steps": [
            {
                "code": s.exam.code,
                "name": s.exam.name,
                "department": s.exam.department,
                "service_minutes": s.exam.service_minutes,
                "arrive_at": round(s.arrive_at, 1),
                "wait_minutes": round(s.wait_minutes, 1),
                "start_at": round(s.start_at, 1),
                "finish_at": round(s.finish_at, 1),
                "ticket": s.ticket,
                "call_at": None if s.call_at is None else round(s.call_at, 1),
                "missed": s.missed,
                "shared_with": s.shared_with,
                "reason": reasons[i] if i < len(reasons) else "",
            }
            for i, s in enumerate(plan.steps)
        ],
        "conflicts": plan.conflicts,
        "notes": plan.notes,
        "comparison": comparison,
        "queue_status": data_source.last_status(),
        "nodes": plan.nodes,
        "elapsed_ms": round(plan.elapsed_ms, 2),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ExamSchedulerDemo/0.1"

    def log_message(self, fmt, *args):  # 保持终端安静
        return

    # ------------------------------------------------------------------
    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _serve_static(self, filename: str) -> None:
        path = os.path.normpath(os.path.join(STATIC_DIR, filename))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._send_json(404, {"error": "文件不存在"})
            return
        ctype = "text/html; charset=utf-8"
        if path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/catalog":
            try:
                self._send_json(200, build_catalog_payload())
            except Exception as exc:  # 配置错误时把真实原因吐给前端
                self._send_json(500, {"error": f"读取规则库失败：{exc}"})
            return

        if route == "/api/snapshot":
            try:
                self._send_json(200, build_snapshot_payload())
            except Exception as exc:
                self._send_json(500, {"error": f"读取队列快照失败：{exc}"})
            return

        name = "index.html" if route in ("/", "/index.html", "") else route.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, name))
        if target.startswith(STATIC_DIR) and os.path.isfile(target):
            self._serve_static(name)
        else:
            self._send_json(404, {"error": "未找到该路径"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in ("/api/recommend", "/api/take"):
            self._send_json(404, {"error": "未找到该路径"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return

        if route == "/api/take":
            department = str(body.get("department", ""))
            if not department:
                self._send_json(400, {"error": "缺少 department"})
                return
            try:
                self._send_json(200, data_source.take_ticket(department))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": f"取号失败：{exc}"})
            return

        try:
            self._send_json(200, build_recommend_payload(body))
        except service.RequestError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": f"求解失败：{exc}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="检查路线规划演示服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--queue-api", default=data_source.API_BASE_URL,
                        help="排队数据源地址，默认 http://127.0.0.1:9000")
    parser.add_argument("--local", action="store_true",
                        help="不连数据源，改用本地写死的队列数据")
    parser.add_argument("--cache-ttl", type=float, default=None,
                        help="队列快照在服务端缓存多少秒，默认 2.0；设 0 可每次实时拉取")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    problems = rules_mod.validate()
    if problems:
        print("[规则库校验未通过]")
        for p in problems:
            print("  -", p)
        return

    # 数据源：先探测，连不上就降级到本地，但要让人看见
    data_source.configure(enabled=not args.local, base_url=args.queue_api,
                          cache_ttl=args.cache_ttl)
    if args.local:
        print("[数据源] 使用本地写死数据（--local）")
    elif data_source.probe():
        print(f"[数据源] 已连接 {args.queue_api}")
    else:
        print(f"[数据源] 连接 {args.queue_api} 失败，降级为本地数据")
        print("        如果要看动态队列，请先运行：python his_queue_mock.py")

    server = None
    port = args.port
    for candidate in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("8765 起连续 20 个端口都被占用，请用 --port 指定其他端口")

    url = f"http://127.0.0.1:{port}"
    print("=" * 56)
    print(" 检查路线规划 · 演示服务已启动")
    print(f" 地址：{url}")
    print(" 按 Ctrl+C 结束")
    print("=" * 56)

    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
