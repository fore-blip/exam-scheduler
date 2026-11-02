"""数据源：科室队列快照 + 患者检查申请。

两种模式
--------
远程模式（默认）：从模拟叫号系统 / 医院排队服务拉取实时快照。
    接口：GET {API_BASE_URL}/queue/snapshot
    失败时自动降级为"上一次成功的快照"，并标记 stale，不阻断推荐。

本地模式：使用下方写死的数据，用于离线调试。

规划基准时刻
------------
SNAPSHOT_TIME 是**演示场景的基准时刻**（09:05），不是真实系统时间。
队列数据实时变化，但"从几点开始算"固定在这一刻，方便反复演示同一场景。
真实部署时应换成 datetime.now()。

接口约定
--------
GET {API_BASE_URL}/queue/snapshot
{
  "snapshot_time": "2026-10-31T23:50:12",
  "departments": [
    {"department": "检验科", "queue_length": 18, "wait_minutes": 40.0}
  ]
}

只要响应能映射成 (department, queue_length, wait_minutes) 三元组，其余字段都可以忽略。
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import DepartmentState

# ===========================================================================
# 配置
# ===========================================================================
USE_REMOTE_API = True                     # True 时从端口拉取，False 时用本地写死数据
API_BASE_URL = "http://127.0.0.1:9000"    # 模拟叫号系统地址
API_TIMEOUT = 2.0                         # 秒
CACHE_TTL = 2.0                           # 秒。避免每个请求都打一次医院接口

# 演示场景的基准时刻
SNAPSHOT_TIME = datetime(2026, 10, 31, 9, 5)

# 本地兜底数据（远程可用时不会用到）
DEPARTMENT_SNAPSHOT: Dict[str, dict] = {
    "检验科": {"queue_length": 18, "wait_minutes": 40},
    "心电科": {"queue_length": 6, "wait_minutes": 15},
    "超声科": {"queue_length": 25, "wait_minutes": 75},
    "放射科": {"queue_length": 9, "wait_minutes": 25},
    "呼吸科": {"queue_length": 4, "wait_minutes": 12},
}

# 患者申请：一次问诊后同时开立的多项检查
PATIENT_REQUEST: dict = {
    "patient_id": "P20261031001",
    "exam_codes": ["CBC", "BIO", "ECG", "ECHO", "US_ABD", "CXR"],
    "is_fasting": True,
    "completed": {},
}

# ===========================================================================
# 运行时状态
# ===========================================================================
_cache: Dict[str, Any] = {"states": None, "at": 0.0}
_status: Dict[str, Any] = {
    "ok": True,
    "source": "remote",
    "error": "",
    "fetched_at": None,
    "age_seconds": None,
}


def configure(enabled: Optional[bool] = None, base_url: Optional[str] = None,
              timeout: Optional[float] = None, cache_ttl: Optional[float] = None) -> None:
    """在启动时切换数据源。"""
    global USE_REMOTE_API, API_BASE_URL, API_TIMEOUT, CACHE_TTL
    if enabled is not None:
        USE_REMOTE_API = bool(enabled)
    if base_url:
        API_BASE_URL = base_url.rstrip("/")
    if timeout is not None:
        API_TIMEOUT = float(timeout)
    if cache_ttl is not None:
        CACHE_TTL = max(0.0, float(cache_ttl))
    _cache["states"] = None
    _cache["at"] = 0.0


# ===========================================================================
# 远程获取
# ===========================================================================
def fetch_snapshot_from_api(
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """从排队服务拉取快照，返回原始 JSON。"""
    url = f"{(base_url or API_BASE_URL).rstrip('/')}/queue/snapshot"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout or API_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _to_states(raw: Dict[str, Any], now: Optional[datetime]) -> Dict[str, DepartmentState]:
    states: Dict[str, DepartmentState] = {}
    for item in raw.get("departments", []):
        name = item.get("department")
        if not name:
            continue
        serving = item.get("serving_no")
        service = item.get("service_minutes")
        states[name] = DepartmentState(
            department=name,
            queue_length=int(item.get("queue_length", 0)),
            wait_minutes=float(item.get("wait_minutes", 0.0)),
            updated_at=now or SNAPSHOT_TIME,
            serving_no=None if serving is None else int(serving),
            service_minutes=None if service is None else float(service),
        )
    return states


def _local_states(now: Optional[datetime]) -> Dict[str, DepartmentState]:
    return {
        dept: DepartmentState(
            department=dept,
            queue_length=int(item.get("queue_length", 0)),
            wait_minutes=float(item.get("wait_minutes", 0.0)),
            updated_at=now or SNAPSHOT_TIME,
            serving_no=item.get("serving_no"),
            service_minutes=item.get("service_minutes"),
        )
        for dept, item in DEPARTMENT_SNAPSHOT.items()
    }


# ===========================================================================
# 统一入口
# ===========================================================================
def get_snapshot(now: Optional[datetime] = None) -> Dict[str, DepartmentState]:
    """取得科室队列快照。

    远程模式下带 2 秒缓存；拉取失败时退回到上一次成功的结果并标记 stale，
    只有在从未成功过的情况下才使用本地写死数据。
    """
    if not USE_REMOTE_API:
        _status.update(ok=True, source="local", error="", age_seconds=0.0)
        return _local_states(now)

    now_ts = time.time()
    if _cache["states"] is not None and now_ts - _cache["at"] < CACHE_TTL:
        _status["age_seconds"] = round(now_ts - _cache["at"], 1)
        return _cache["states"]

    try:
        raw = fetch_snapshot_from_api()
        states = _to_states(raw, now)
        if not states:
            raise ValueError("接口返回的科室列表为空")
        _cache.update(states=states, at=now_ts)
        _status.update(
            ok=True, source="remote", error="",
            fetched_at=datetime.now().strftime("%H:%M:%S"), age_seconds=0.0,
        )
        return states
    except Exception as exc:
        if _cache["states"] is not None:
            _status.update(
                ok=False, source="cache", error=str(exc),
                age_seconds=round(now_ts - _cache["at"], 1),
            )
            return _cache["states"]

        _status.update(ok=False, source="local", error=str(exc), age_seconds=None)
        return _local_states(now)


def last_status() -> Dict[str, Any]:
    """最近一次取数的健康状态，供前端显示"数据新鲜度"。"""
    return dict(_status)


def probe(base_url: Optional[str] = None, timeout: float = 1.5) -> bool:
    """启动时探测数据源是否可达。"""
    try:
        fetch_snapshot_from_api(base_url, timeout)
        return True
    except Exception:
        return False


def take_ticket(department: str) -> Dict[str, Any]:
    """向叫号系统取一个新号（相当于在取号机上点一下）。

    远程模式调 POST {API_BASE_URL}/queue/take；失败或本地模式时，
    按当前快照推一个保守号，保证前端流程不断。
    """
    if USE_REMOTE_API:
        url = f"{API_BASE_URL.rstrip('/')}/queue/take"
        request = urllib.request.Request(
            url,
            data=json.dumps({"department": department}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            data.setdefault("source", "remote")
            return data
        except Exception as exc:
            fallback_error = str(exc)
    else:
        fallback_error = "本地模式"

    st = get_snapshot(None).get(department)
    if st is None:
        return {"ok": False, "error": f"取号失败：{fallback_error}"}
    return {
        "ok": True,
        "department": department,
        "ticket": int(st.queue_length) + 1,
        "serving_no": st.serving_no,
        "ahead": int(st.queue_length),
        "wait_minutes": float(st.wait_minutes),
        "source": "fallback",
        "error": fallback_error,
    }


def get_patient_request() -> dict:
    """取得患者检查申请。真实场景由医嘱系统按 patient_id 查询。"""
    return dict(PATIENT_REQUEST)


def build_request(
    exam_codes: List[str],
    *,
    patient_id: str = "UNKNOWN",
    is_fasting: bool = True,
    completed: Optional[Dict[str, float]] = None,
) -> dict:
    return {
        "patient_id": patient_id,
        "exam_codes": list(exam_codes),
        "is_fasting": is_fasting,
        "completed": dict(completed or {}),
    }
