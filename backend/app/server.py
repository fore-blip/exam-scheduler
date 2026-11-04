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
from typing import Any, Dict, List
from urllib.parse import urlparse

from . import data_source
from . import prescriptions
from . import rules as rules_mod
from . import service

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(BASE_DIR, "frontend")


def _hhmm(minute: int) -> str:
    minute = int(minute) % (24 * 60)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def build_departments_payload() -> Dict[str, Any]:
    """挂号科室列表：患者端选科室、医生端做导航都用这一份。"""
    states = data_source.get_snapshot(data_source.SNAPSHOT_TIME)
    out = []
    for cfg in rules_mod.DEPARTMENTS:
        st = states.get(cfg["name"])
        out.append({
            "code": cfg["code"],
            "name": cfg["name"],
            "prefix": cfg["prefix"],
            "desc": cfg["desc"],
            "exams": list(cfg["exams"]),
            # 待诊人数按本科室的挂号记录算，不是检查队列
            "waiting": prescriptions.waiting_count(cfg["code"]),
            # 检查队列现状，帮患者判断哪个科室现在不挤
            "queue_length": st.queue_length if st else 0,
            "wait_minutes": round(st.wait_minutes, 1) if st else 0.0,
        })
    return {"departments": out}


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
                "next_ticket": st.next_ticket,
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
                "next_ticket": st.next_ticket,
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
    patient_id = str(body.get("patient_id") or "").strip()
    codes = [str(c) for c in (body.get("codes") or [])]
    fasting = bool(body.get("fasting", True))

    # 用户端只按医嘱执行，不自选项目
    patient = None
    if patient_id:
        patient = prescriptions.get_patient(patient_id)
        rx = prescriptions.get_by_patient(patient_id)
        if rx is None:
            return {
                "feasible": False,
                "status": "NO_PRESCRIPTION",
                "snapshot_time": _iso(data_source.SNAPSHOT_TIME),
                "total_minutes": 0.0, "total_wait": 0.0, "total_idle": 0.0,
                "order": [], "steps": [], "reminders": [], "comparison": {},
                "conflicts": [f"患者「{patient_id}」还没有医嘱，请先在医生端完成开单"],
                "notes": [],
            }
        codes = list(rx.items)

    tickets = {str(k): int(v) for k, v in (body.get("tickets") or {}).items()
               if v not in (None, "")}
    # 患者自己挂过的号优先：这是既成事实，不能因为请求没带就当成没挂
    if patient:
        for code, number in (patient.get("tickets") or {}).items():
            tickets.setdefault(code, int(number))
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
        # 前 N 步属于「已挂号块」。0 表示没有已挂号项，整条路线是单一最优序列。
        "ticket_block_size": plan.block_split,
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
                "wait_source": s.wait_source,
                "travel_minutes": round(s.travel_minutes, 1),
                "needs_ticket": s.needs_ticket,
                "reason": reasons[i] if i < len(reasons) else "",
            }
            for i, s in enumerate(plan.steps)
        ],
        "reminders": [r.to_dict() for r in plan.reminders],
        "conflicts": plan.conflicts,
        "notes": plan.notes,
        "comparison": comparison,
        "queue_status": data_source.last_status(),
        "nodes": plan.nodes,
        "elapsed_ms": round(plan.elapsed_ms, 2),
    }


def build_tickets_payload(patient_id: str) -> Dict[str, Any]:
    """挂号页数据：每个项目的队列现状、叫号、推荐次序、是否已取号。"""
    patient = prescriptions.get_patient(patient_id)
    if patient is None:
        raise KeyError(patient_id)

    plan = build_recommend_payload({"patient_id": patient_id})
    order = [str(c) for c in (plan.get("order") or [])]
    catalog = rules_mod.build_catalog()
    states = data_source.get_snapshot(data_source.SNAPSHOT_TIME)
    taken_map = patient.get("tickets") or {}

    exams: List[Dict[str, Any]] = []
    for code in prescriptions.items_of(patient_id):
        exam = catalog.get(code)
        if exam is None:
            continue
        st = states.get(exam.department)
        taken = taken_map.get(code)
        next_ticket = st.next_ticket if st else None
        # 现在取号大约多久后被叫到：号差 × 人均服务时长（与求解器同口径）
        call_eta = None
        if (st and next_ticket is not None and st.serving_no is not None
                and st.service_minutes):
            call_eta = round((next_ticket - st.serving_no) * st.service_minutes, 1)
        exams.append({
            "code": code,
            "name": exam.name,
            "department": exam.department,
            "service_minutes": exam.service_minutes,
            "requires_fasting": exam.requires_fasting,
            "queue_length": st.queue_length if st else 0,
            "serving_no": st.serving_no if st else None,
            "wait_minutes": round(st.wait_minutes, 1) if st else 0.0,
            "next_ticket": next_ticket,
            "call_eta": call_eta,
            "ticket": int(taken) if taken is not None else None,
            "taken": taken is not None,
            "order": (order.index(code) + 1) if code in order else None,
        })

    # 未挂号的按推荐次序在前，已挂号的沉底
    exams.sort(key=lambda x: (x["taken"], x["order"] or 999))
    return {
        "patient": patient,
        "recommended_order": order,
        "remaining": len([e for e in exams if not e["taken"]]),
        "travel_minutes": rules_mod.TRAVEL_MINUTES,
        "exams": exams,
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

        if route == "/api/departments":
            try:
                self._send_json(200, build_departments_payload())
            except Exception as exc:
                self._send_json(500, {"error": f"读取科室信息失败：{exc}"})
            return

        # 医生端：只返回本科室患者，隔离在服务端完成
        if route.startswith("/api/doctor/") and route.endswith("/patients"):
            code = route[len("/api/doctor/"):-len("/patients")].strip("/")
            cfg = rules_mod.dept_of_code(code)
            if cfg is None:
                self._send_json(404, {"error": f"未知科室：{code}"})
                return
            self._send_json(200, {
                "department": {"code": cfg["code"], "name": cfg["name"],
                               "desc": cfg["desc"], "prefix": cfg["prefix"]},
                "patients": prescriptions.list_patients(dept_code=cfg["code"]),
            })
            return

        # 挂号页：剩余项目 + 各科室叫号情况（注意要排在 /api/patient/<pid> 之前）
        if route.startswith("/api/patient/") and route.endswith("/tickets"):
            pid = route[len("/api/patient/"):-len("/tickets")].strip("/")
            try:
                self._send_json(200, build_tickets_payload(pid))
            except KeyError:
                self._send_json(404, {"error": f"未找到患者：{pid}"})
            except Exception as exc:
                self._send_json(500, {"error": f"读取挂号信息失败：{exc}"})
            return

        # 患者端：自己的就诊记录 + 医嘱
        if route.startswith("/api/patient/"):
            pid = route[len("/api/patient/"):].strip("/")
            patient = prescriptions.get_patient(pid)
            if patient is None:
                self._send_json(404, {"error": f"未找到患者：{pid}"})
                return
            self._send_json(200, {"patient": patient})
            return

        if route == "/api/patients":
            # 兼容接口：必须指定科室，避免无意中拿到全院数据
            from urllib.parse import parse_qs
            code = (parse_qs(urlparse(self.path).query).get("department") or [""])[0]
            if not code:
                self._send_json(400, {"error": "请指定 department 参数（科室编码）"})
                return
            self._send_json(200, {"patients": prescriptions.list_patients(dept_code=code)})
            return

        if route == "/api/prescription":
            from urllib.parse import parse_qs
            pid = (parse_qs(urlparse(self.path).query).get("patient_id") or [""])[0]
            rx = prescriptions.get_by_patient(pid) if pid else None
            if rx is None:
                self._send_json(404, {"error": f"患者「{pid}」暂无医嘱"})
                return
            self._send_json(200, rx.to_dict())
            return

        name = "index.html" if route in ("/", "/index.html", "") else route.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, name))
        if target.startswith(STATIC_DIR) and os.path.isfile(target):
            self._serve_static(name)
            return

        # 前端是单页应用：/doctor、/doctor/lab、/patient 这类无扩展名路径
        # 直接返回 index.html，由前端按 location.pathname 决定渲染哪一页。
        if "." not in os.path.basename(route):
            self._serve_static("index.html")
            return

        self._send_json(404, {"error": "未找到该路径"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        is_ticket_route = route.startswith("/api/patient/") and route.endswith("/tickets")
        if not is_ticket_route and route not in (
                "/api/recommend", "/api/take", "/api/register",
                "/api/prescription", "/api/prescription/reset"):
            self._send_json(404, {"error": "未找到该路径"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return

        # 挂号：按 items 的先后顺序逐个取号，这个顺序就是「挂号顺序」
        if is_ticket_route:
            pid = route[len("/api/patient/"):-len("/tickets")].strip("/")
            items = [str(i) for i in (body.get("items") or []) if i]
            if not items:
                self._send_json(400, {"error": "请至少选择一个检查项目"})
                return
            try:
                self._send_json(200, prescriptions.take_tickets(pid, items))
            except KeyError:
                self._send_json(404, {"error": f"未找到患者：{pid}"})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if route == "/api/register":
            try:
                patient = prescriptions.register(
                    name=str(body.get("name", "")),
                    age=int(body.get("age") or 0),
                    gender=str(body.get("gender", "")),
                    dept_code=str(body.get("department", "") or body.get("dept_code", "")),
                    chief=str(body.get("chief_complaint", "")),
                )
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "patient": patient})
            return

        if route == "/api/prescription":
            patient_id = str(body.get("patient_id", "")).strip()
            items = [str(i) for i in (body.get("items") or []) if i]
            doctor = str(body.get("doctor") or "门诊医生")
            # 医生所在科室：带了这个参数就会做归属校验
            dept_code = str(body.get("department") or body.get("dept_code") or "").strip()
            if not patient_id or not items:
                self._send_json(400, {"error": "需要 patient_id 与 items"})
                return
            try:
                rx = prescriptions.issue(patient_id, items, doctor,
                                         dept_code=dept_code or None)
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
                return
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "prescription": rx.to_dict()})
            return

        if route == "/api/prescription/reset":
            prescriptions.reset()
            self._send_json(200, {"ok": True})
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
