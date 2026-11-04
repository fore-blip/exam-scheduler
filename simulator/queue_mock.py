"""模拟医院排队叫号系统 —— 动态队列数据源（默认端口 9000）。

它扮演"HIS / 叫号系统"的角色，每隔 2 秒更新一次各科室的排队人数与预计等待，
对外提供 HTTP 快照接口。用来替代真实医院接口做联调。

接口
----
GET  /queue/snapshot   全量快照（推荐）
POST /queue/snapshot   同上，兼容旧约定
POST /queue/event      手动注入事件：{"department": "超声科", "delta_queue": 8}
                      或 {"department": "超声科", "extra_wait": 6}
GET  /                 实时看板，可直接在浏览器观察数字跳动

启动
----
python his_queue_mock.py --port 9000

队列怎么变
----------
每个 tick：先走掉 0~3 人（被叫号），再来 0~4 人（新患者），
人数在 [0, cap] 内随机游走。等待时长 = 人数 × 单人平均服务时长 + 临时惩罚。
所以它是"真在动"的，不是随机跳一个假数字。
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

TICK_SECONDS = 2.0

# 叫号推进节奏：所有科室都从 1 号开始叫，每隔 CALL_INTERVAL 秒往后走一位。
# 注意这是"演示时钟"，与下面 service 的真实服务时长是两件事——
# 号推进得快，是为了在演示时能立刻看到号码跳动、等待时间往下掉；
# 而等待时长本身仍按 人数 × 人均服务时长 真实折算，不做任何压缩。
SERVING_START = 1
CALL_INTERVAL = 60.0

# 长时间没人访问就把演示时钟归零：页面关掉过一会儿再打开，看到的又是 1 号。
# 只要页面开着（前端每 3 秒轮询一次），就永远不会触发。
IDLE_RESET = 120.0

_START_TS = time.monotonic()
_LAST_HIT = 0.0

# 初始值与演示算例对齐：人数 × service ≈ 当前写死的等待时长
# base 是基线人数，队列会围绕它上下波动，不会一路涨到上限
DEPARTMENTS: Dict[str, Dict[str, Any]] = {
    "检验科": {"queue": 18, "base": 18, "service": 2.2, "cap": 45, "extra": 0.0, "serving": SERVING_START},
    "心电科": {"queue": 6,  "base": 6,  "service": 2.5, "cap": 30, "extra": 0.0, "serving": SERVING_START},
    "超声科": {"queue": 25, "base": 25, "service": 3.0, "cap": 45, "extra": 0.0, "serving": SERVING_START},
    "放射科": {"queue": 9,  "base": 9,  "service": 2.8, "cap": 32, "extra": 0.0, "serving": SERVING_START},
    "呼吸科": {"queue": 4,  "base": 4,  "service": 3.0, "cap": 20, "extra": 0.0, "serving": SERVING_START},
}

LOCK = threading.Lock()
STATE = {"tick": 0, "started_at": datetime.now()}
HISTORY: Dict[str, list] = {name: [] for name in DEPARTMENTS}


def wait_of(dept: Dict[str, Any]) -> float:
    """此刻加入该科室的预计等待 = 排队人数 × 人均服务时长（真实口径）。"""
    return round(dept["queue"] * dept["service"] + dept["extra"], 1)


def _touch_locked() -> None:
    """记录一次访问；若距上次访问超过 IDLE_RESET，把演示时钟归零。

    必须在 LOCK 内调用。这样"没人看的时候"时钟是停的，
    下一个人打开页面时看到的就是从 1 号开始的新一轮。
    """
    global _START_TS, _LAST_HIT
    now = time.monotonic()
    if IDLE_RESET > 0 and (not _LAST_HIT or now - _LAST_HIT > IDLE_RESET):
        _START_TS = now
        for dept in DEPARTMENTS.values():
            dept["serving"] = SERVING_START
            dept["issued"] = 0
    _LAST_HIT = now


def _target_serving() -> int:
    """按真实时间算出的"此刻应叫到的号"：1 号起，每 CALL_INTERVAL 秒 +1。"""
    elapsed = max(0.0, time.monotonic() - _START_TS)
    return SERVING_START + int(elapsed // CALL_INTERVAL)


def _sync_serving() -> Dict[str, int]:
    """把所有科室的叫号推进到当前时刻该到的号，返回每科本次前进的号数。

    必须在 LOCK 内调用。用真实时间而不是 tick 计数，
    这样即使进程被挂起、ticker 抖了一下，号也不会落后。
    """
    target = _target_serving()
    steps: Dict[str, int] = {}
    for name, dept in DEPARTMENTS.items():
        current = int(dept["serving"])
        step = max(0, target - current)
        if step:
            dept["serving"] = target
        steps[name] = step
    return steps


def tick_once() -> None:
    """推进一个时间片：先按演示时钟推进叫号，再有新患者到达。

    到达人数按"离基线越远、回归越强"生成，保证队列围绕 baseline 波动，
    而不是一路涨到上限——否则演示十几分钟后所有科室都会饱和。
    """
    with LOCK:
        STATE["tick"] += 1
        called = _sync_serving()
        for name, dept in DEPARTMENTS.items():
            gone = called.get(name, 0)
            pull = (dept["base"] - dept["queue"]) * 0.25
            arrivals = int(round(gone + pull + random.gauss(0, 1.0)))
            arrivals = max(0, min(6, arrivals))
            dept["queue"] = max(0, min(dept["cap"], dept["queue"] - gone + arrivals))
            if dept["extra"] > 0:                     # 设备暂停的惩罚随时间衰减
                dept["extra"] = max(0.0, round(dept["extra"] - 1.5, 1))

            HISTORY[name].append(wait_of(dept))
            if len(HISTORY[name]) > 36:
                HISTORY[name].pop(0)


def _ticker() -> None:
    while True:
        time.sleep(TICK_SECONDS)
        try:
            tick_once()
        except Exception:
            pass


def snapshot() -> Dict[str, Any]:
    with LOCK:
        _touch_locked()          # 空闲归来时先把时钟归零
        _sync_serving()          # 读的时候也校正一次，避免 tick 未到导致号滞后
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        target = _target_serving()
        next_in = CALL_INTERVAL - (max(0.0, time.monotonic() - _START_TS) % CALL_INTERVAL)
        return {
            "snapshot_time": now,
            "tick": STATE["tick"],
            "tick_seconds": TICK_SECONDS,
            "call_interval_seconds": round(CALL_INTERVAL, 1),
            "serving_start": SERVING_START,
            "next_call_in_seconds": round(next_in, 1),
            "departments": [
                {
                    "department": name,
                    "queue_length": int(dept["queue"]),
                    "wait_minutes": wait_of(dept),
                    "serving_no": int(dept["serving"]),
                    "service_minutes": float(dept["service"]),
                    # 现在去取号机会拿到的号，患者端挂号页直接展示
                    "next_ticket": max(int(dept.get("issued") or 0),
                                       int(dept["serving"]) + int(dept["queue"])) + 1,
                    "updated_at": now,
                    "history": HISTORY[name][-20:],
                }
                for name, dept in DEPARTMENTS.items()
            ],
        }


def take_ticket(department: str) -> Dict[str, Any]:
    """取号机：给该科室发一个新号。

    号 = 已发出的最大号 + 1，且不小于「当前叫号 + 排队人数」。
    所以「前面还有几个人」与队列口径一致，不会出现号比队列还靠前的怪事。
    """
    with LOCK:
        _touch_locked()
        _sync_serving()
        dept = DEPARTMENTS.get(department)
        if dept is None:
            return {"ok": False, "error": f"未知科室：{department}"}

        floor = int(dept["serving"]) + int(dept["queue"])
        issued = max(int(dept.get("issued") or 0), floor)
        ticket = issued + 1
        dept["issued"] = ticket

        ahead = max(0, ticket - int(dept["serving"]) - 1)
        return {
            "ok": True,
            "department": department,
            "ticket": ticket,
            "serving_no": int(dept["serving"]),
            "ahead": ahead,
            "wait_minutes": round(ahead * float(dept["service"]), 1),
            "service_minutes": float(dept["service"]),
        }


def reset_calling() -> Dict[str, Any]:
    """把叫号重置回起始号，并让演示时钟从此刻重新计时。

    演示前按一下，号就不会累积到几百号，方便现场临时取号。
    """
    global _START_TS, _LAST_HIT
    with LOCK:
        _START_TS = time.monotonic()
        _LAST_HIT = _START_TS
        for dept in DEPARTMENTS.values():
            dept["serving"] = SERVING_START
            dept["issued"] = 0
    return {"ok": True, "serving_start": SERVING_START,
            "call_interval_seconds": round(CALL_INTERVAL, 1)}


def apply_event(body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("department", ""))
    if name not in DEPARTMENTS:
        return {"ok": False, "error": f"未知科室：{name}"}

    with LOCK:
        dept = DEPARTMENTS[name]
        if "delta_queue" in body:
            dept["queue"] = max(0, min(dept["cap"], dept["queue"] + int(body["delta_queue"])))
            action = f"人数 {'+' if int(body['delta_queue']) >= 0 else ''}{int(body['delta_queue'])}"
        elif "extra_wait" in body:
            dept["extra"] = max(0.0, dept["extra"] + float(body["extra_wait"]))
            action = f"附加等待 +{float(body['extra_wait']):.0f} 分钟"
        else:
            return {"ok": False, "error": "需要 delta_queue 或 extra_wait"}
        current = {"department": name, "queue_length": int(dept["queue"]),
                   "wait_minutes": wait_of(dept)}

    return {"ok": True, "action": action, **current}


DASHBOARD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>医院叫号系统 · 模拟数据源</title>
<style>
 body{margin:0;background:#F4F6F9;color:#16202C;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
 .wrap{max-width:900px;margin:0 auto;padding:24px}
 h1{font-size:17px;margin:0 0 4px}
 .sub{font-size:12.5px;color:#6B7785;margin-bottom:18px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
 .card{background:#fff;border:1px solid #E5E9F0;border-radius:12px;padding:14px 16px}
 .row{display:flex;align-items:baseline;gap:8px}
 .name{font-size:13.5px;font-weight:500}
 .q{font-size:26px;font-weight:600;letter-spacing:-.5px;margin-left:auto}
 .q small{font-size:11px;font-weight:400;color:#6B7785;margin-left:2px}
 .w{font-size:12.5px;color:#6B7785;margin-top:2px}
 .w b{color:#0F766E;font-weight:500}
 .spark{height:26px;margin-top:8px;display:flex;align-items:flex-end;gap:1px}
 .spark i{flex:1;background:#CFE3DF;border-radius:1px;min-height:2px;transition:height .3s}
 .acts{display:flex;gap:6px;margin-top:10px}
 .acts button{font:inherit;font-size:11.5px;padding:3px 9px;border-radius:6px;border:1px solid #E5E9F0;background:#FBFCFD;cursor:pointer;color:#5A6B7F}
 .acts button:hover{border-color:#C9D2DE;background:#F4F6F9}
 .foot{margin-top:18px;font-size:12px;color:#9AA5B1;display:flex;gap:14px;align-items:center}
 .foot button{font:inherit;font-size:11.5px;padding:3px 9px;border-radius:6px;border:1px solid #E5E9F0;background:#FBFCFD;cursor:pointer;color:#5A6B7F}
 .foot button:hover{border-color:#C9D2DE;background:#F4F6F9}
 .dot{width:7px;height:7px;border-radius:50%;background:#16A34A;display:inline-block;margin-right:5px}
 @keyframes p{0%,100%{opacity:1}50%{opacity:.2}}
 .dot{animation:p 1.8s infinite}
</style></head><body><div class="wrap">
<h1>医院叫号系统 · 模拟数据源</h1>
<div class="sub">这是"医院那一侧"的模拟服务，每 2 秒更新一次。左侧调度系统从这里取数。</div>
<div class="grid" id="grid"></div>
<div class="foot"><span><i class="dot"></i><span id="status">连接中</span></span><span id="call"></span><span id="tick"></span><button id="reset">重置叫号</button></div>
</div>
<script>
const COLORS=["#2563EB","#7C3AED","#0D9488","#D97706","#DB2777"];
let alive=true;
async function pull(){
  try{
    const r=await fetch("/queue/snapshot",{cache:"no-store"});
    const d=await r.json();
    render(d);
    document.getElementById("status").textContent="实时更新中";
  }catch(e){
    document.getElementById("status").textContent="连接中断";
  }
  if(alive) setTimeout(pull,2000);
}
function render(d){
  document.getElementById("tick").textContent="第 "+d.tick+" 次刷新";
  const call=document.getElementById("call");
  if(call && d.call_interval_seconds){
    call.textContent="叫号：每 "+Math.round(d.call_interval_seconds)+" 秒 +1 号 · 下一号 "
      + Math.ceil(d.next_call_in_seconds) + " 秒后";
  }
  const grid=document.getElementById("grid");
  if(grid.children.length!==d.departments.length){
    grid.innerHTML=d.departments.map((x,i)=>`<div class="card" data-n="${x.department}">
      <div class="row"><span class="name"><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${COLORS[i%5]};margin-right:6px"></i>${x.department}</span>
      <span class="q" data-q>0<small>人</small></span></div>
      <div class="w">当前叫到 <b data-s-no>0</b> 号 · 预计等待 <b data-w>0</b> 分钟</div>
      <div class="spark" data-s></div>
      <div class="acts"><button data-a="5">＋5 人</button><button data-a="-5">−5 人</button><button data-e="6">设备暂停</button></div>
    </div>`).join("");
  }
  d.departments.forEach(x=>{
    const card=grid.querySelector(`[data-n="${x.department}"]`);
    card.querySelector("[data-q]").innerHTML=x.queue_length+"<small>人</small>";
    card.querySelector("[data-w]").textContent=x.wait_minutes;
    card.querySelector("[data-s-no]").textContent=x.serving_no;
    const max=Math.max(1,...x.history);
    card.querySelector("[data-s]").innerHTML=x.history.map(v=>`<i style="height:${Math.round(v/max*24)}px"></i>`).join("");
  });
}
document.addEventListener("click",async e=>{
  const b=e.target.closest("button"); if(!b) return;
  if(b.id==="reset"){
    await fetch("/queue/reset",{method:"POST"});
    pull();
    return;
  }
  const card=b.closest(".card"); if(!card) return; const name=card.dataset.n;
  const body=b.dataset.a?{department:name,delta_queue:+b.dataset.a}
                       :{department:name,extra_wait:+b.dataset.e};
  await fetch("/queue/event",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  pull();
});
pull();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "QueueMock/0.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_OPTIONS(self) -> None:
        """允许浏览器跨端口调试本服务。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/queue/snapshot":
            # delay_ms 仅供调试：慢响应可以用来验证前端的超时与降级
            delay = self._delay_ms(parsed.query)
            if delay:
                time.sleep(delay)
            self._json(200, snapshot())
        elif route in ("/", "/index.html"):
            self._send(200, DASHBOARD.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._json(404, {"error": "未找到该路径"})

    @staticmethod
    def _delay_ms(query: str) -> float:
        from urllib.parse import parse_qs
        raw = parse_qs(query).get("delay_ms", [""])[0]
        try:
            return min(10.0, max(0.0, float(raw) / 1000.0))
        except (TypeError, ValueError):
            return 0.0

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if route == "/queue/snapshot":
            self._json(200, snapshot())
            return
        if route == "/queue/reset":
            self._json(200, reset_calling())
            return
        if route == "/queue/take":
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
                return
            result = take_ticket(str(body.get("department", "")))
            self._json(200 if result.get("ok") else 400, result)
            return
        if route != "/queue/event":
            self._json(404, {"error": "未找到该路径"})
            return
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return
        result = apply_event(body)
        self._json(200 if result.get("ok") else 400, result)


def main() -> None:
    global CALL_INTERVAL, SERVING_START, IDLE_RESET, _START_TS

    parser = argparse.ArgumentParser(description="模拟医院排队叫号数据源")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--call-interval", type=float, default=CALL_INTERVAL,
                        help="叫号推进速度：每隔多少秒往后走一位（默认 60）")
    parser.add_argument("--serving-start", type=int, default=SERVING_START,
                        help="起始叫号，所有科室统一从这里开始（默认 1）")
    parser.add_argument("--idle-reset", type=float, default=IDLE_RESET,
                        help="多久没人访问就把叫号归零，单位秒（默认 120，设 0 关闭）")
    args = parser.parse_args()

    CALL_INTERVAL = max(1.0, float(args.call_interval))
    SERVING_START = max(1, int(args.serving_start))
    IDLE_RESET = max(0.0, float(args.idle_reset))
    for dept in DEPARTMENTS.values():
        dept["serving"] = SERVING_START
    _START_TS = time.monotonic()

    threading.Thread(target=_ticker, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 56)
    print(" 模拟叫号数据源已启动")
    print(f" 快照接口：http://{args.host}:{args.port}/queue/snapshot")
    print(f" 实时看板：http://{args.host}:{args.port}/")
    print(f" 刷新周期：{TICK_SECONDS:.0f} 秒")
    print(f" 叫号节奏：每 {CALL_INTERVAL:.0f} 秒 +1 号，全部从 {SERVING_START} 号起")
    print(" 空闲归零：" + ("已关闭" if IDLE_RESET <= 0
                          else f"{IDLE_RESET:.0f} 秒无人访问就重新从 {SERVING_START} 号开始"))
    print(" 等待时长：仍按 人数 × 人均服务时长 真实折算，不做压缩")
    print(" 按 Ctrl+C 结束")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
