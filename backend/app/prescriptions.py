"""就诊与医嘱存储（演示用内存态）。

流程对应真实医院的分层：

    患者挂号（选科室）→ 该科室医生看到患者 → 医生开单 → 患者端拿到清单排路线

科室隔离在**服务端**完成：医生端只能通过 `list_patients(dept_code=...)` 拿到
本科室患者，开单时也会校验患者是否属于该科室。前端拿不到其他科室的数据，
不是靠界面隐藏。

进程重启后恢复成预置患者（分布在四个科室，呼吸科故意留空以体现隔离）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from . import rules
from .models import Patient, Prescription

_PATIENTS: Dict[str, Patient] = {}
_PRESCRIPTIONS: Dict[str, Prescription] = {}
_SEQ: Dict[str, int] = {}          # 各科室就诊号流水

# 预置患者：每个科室一位，呼吸科留空（用来演示「本科室暂无患者」）
_SEEDS = [
    ("P001", "张伟", 46, "男", "lab", "体检发现血脂异常，要求复查"),
    ("P002", "李敏", 32, "女", "ecg", "心悸、胸闷三天，活动后加重"),
    ("P003", "王强", 58, "男", "us", "上腹隐痛两周，餐后明显"),
    ("P004", "赵芳", 41, "女", "rad", "咳嗽两周，需排除肺部感染"),
]


def _next_visit_no(dept_code: str) -> str:
    n = _SEQ.get(dept_code, 0) + 1
    _SEQ[dept_code] = n
    prefix = (rules.dept_of_code(dept_code) or {}).get("prefix", "A")
    return f"{prefix}{n:03d}"


def _seed() -> None:
    _PATIENTS.clear()
    _PRESCRIPTIONS.clear()
    _SEQ.clear()
    for pid, name, age, gender, dept_code, chief in _SEEDS:
        cfg = rules.dept_of_code(dept_code)
        _PATIENTS[pid] = Patient(
            patient_id=pid,
            name=name,
            age=age,
            gender=gender,
            department=cfg["name"] if cfg else "",
            dept_code=dept_code,
            visit_no=_next_visit_no(dept_code),
            chief_complaint=chief,
            created_at=datetime.now(),
        )


_seed()


def _to_dict(p: Patient) -> dict:
    rx = _PRESCRIPTIONS.get(p.prescription_id) if p.prescription_id else None
    return {
        "patient_id": p.patient_id,
        "name": p.name,
        "age": p.age,
        "gender": p.gender,
        "department": p.department,
        "dept_code": p.dept_code,
        "visit_no": p.visit_no,
        "chief_complaint": p.chief_complaint,
        "status": p.status,
        "prescription": rx.to_dict() if rx else None,
    }


def register(name: str, age: int, gender: str, dept_code: str,
             chief: str = "") -> dict:
    """患者挂号：选定科室后进入该科室的待诊队列，只有该科室医生能看到。"""
    cfg = rules.dept_of_code(dept_code)
    if cfg is None:
        raise ValueError(f"未知科室：{dept_code!r}")

    name = (name or "").strip()
    if not name:
        raise ValueError("请填写姓名")

    pid = "P" + uuid.uuid4().hex[:6].upper()
    patient = Patient(
        patient_id=pid,
        name=name,
        age=max(0, min(120, int(age or 0))),
        gender=(gender or "未填").strip(),
        department=cfg["name"],
        dept_code=cfg["code"],
        visit_no=_next_visit_no(cfg["code"]),
        chief_complaint=(chief or "").strip() or "未填写",
        created_at=datetime.now(),
    )
    _PATIENTS[pid] = patient
    return _to_dict(patient)


def get_patient(patient_id: str) -> Optional[dict]:
    p = _PATIENTS.get(patient_id)
    return _to_dict(p) if p else None


def list_patients(dept_code: Optional[str] = None) -> List[dict]:
    """科室维度的患者列表。

    dept_code 为空时返回全部（仅供调试/统计），医生端一律传具体科室。
    """
    out: List[dict] = []
    for p in _PATIENTS.values():
        if dept_code and p.dept_code != dept_code:
            continue
        out.append(_to_dict(p))
    # 已开单的排后面，未处理的按挂号顺序在前
    out.sort(key=lambda x: (x["status"] == "issued", x["visit_no"]))
    return out


def waiting_count(dept_code: str) -> int:
    """该科室尚未开单的人数，用于患者端展示「前方还有几位」。"""
    return sum(1 for p in _PATIENTS.values()
               if p.dept_code == dept_code and p.status == "waiting")


def issue(patient_id: str, items: List[str], doctor: str = "门诊医生",
          dept_code: Optional[str] = None) -> Prescription:
    """医生开单。

    dept_code 非空时做归属校验：只有患者所挂科室的医生才能开单。
    这是隔离的硬约束，不是界面层的隐藏。
    """
    patient = _PATIENTS.get(patient_id)
    if patient is None:
        raise KeyError(f"未知患者：{patient_id}")
    if dept_code and patient.dept_code != dept_code:
        raise PermissionError(
            f"该患者挂的是「{patient.department}」，不属于本科室，无法开单"
        )
    cleaned = [str(i) for i in items if i]
    if not cleaned:
        raise ValueError("检查项目不能为空")

    rx = Prescription(
        prescription_id="RX" + uuid.uuid4().hex[:8],
        patient_id=patient_id,
        patient_name=patient.name,
        department=patient.department,
        doctor=doctor,
        items=cleaned,
        status="issued",
        created_at=datetime.now(),
    )
    _PRESCRIPTIONS[rx.prescription_id] = rx
    patient.status = "issued"
    patient.prescription_id = rx.prescription_id
    return rx


def get_by_patient(patient_id: str) -> Optional[Prescription]:
    patient = _PATIENTS.get(patient_id)
    if patient and patient.prescription_id:
        return _PRESCRIPTIONS.get(patient.prescription_id)
    return None


def get(prescription_id: str) -> Optional[Prescription]:
    return _PRESCRIPTIONS.get(prescription_id)


def reset() -> None:
    """演示重置：清空挂号与医嘱，恢复预置患者。"""
    _seed()
