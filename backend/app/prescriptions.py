"""医嘱存储（演示用内存态）。

真实场景应由 HIS 的医嘱接口提供，这里用进程内字典模拟：
医生端开单 → 用户端实时读到 → 求解器按清单排程。

进程重启后恢复成预置的三位待开单患者。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from .models import Patient, Prescription

_PATIENTS: Dict[str, Patient] = {}
_PRESCRIPTIONS: Dict[str, Prescription] = {}

# 预置患者：演示时医生端打开就有人可看
_SEEDS = [
    ("P001", "张伟", 46, "男", "内科", "胸闷、心悸三天，活动后加重"),
    ("P002", "李敏", 32, "女", "内科", "上腹隐痛两周，餐后明显"),
    ("P003", "王强", 58, "男", "内科", "体检发现血脂异常，要求复查"),
]


def _seed() -> None:
    _PATIENTS.clear()
    _PRESCRIPTIONS.clear()
    for pid, name, age, gender, dept, chief in _SEEDS:
        _PATIENTS[pid] = Patient(
            patient_id=pid, name=name, age=age, gender=gender,
            department=dept, chief_complaint=chief,
        )


_seed()


def list_patients() -> List[dict]:
    """医生端的患者列表。已开单的会带上医嘱内容。"""
    out: List[dict] = []
    for p in _PATIENTS.values():
        rx = _PRESCRIPTIONS.get(p.prescription_id) if p.prescription_id else None
        out.append({
            "patient_id": p.patient_id,
            "name": p.name,
            "age": p.age,
            "gender": p.gender,
            "department": p.department,
            "chief_complaint": p.chief_complaint,
            "status": p.status,
            "prescription": rx.to_dict() if rx else None,
        })
    return out


def issue(patient_id: str, items: List[str], doctor: str = "门诊医生") -> Prescription:
    """医生开单。用户端下一次拉取即可看到。"""
    patient = _PATIENTS.get(patient_id)
    if patient is None:
        raise KeyError(f"未知患者：{patient_id}")
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
    """演示重置：清空医嘱，患者回到待开单状态。"""
    _seed()
