"""数据模型：检查项目、科室队列、规则、求解结果。

字段含义与《医院多检查动态调度技术方案》§3 的输入输出定义保持一致，
便于后续直接对接医院接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Exam:
    """一项待完成的检查任务。"""

    code: str                 # 检查编码，如 "CBC"
    name: str                 # 中文名，如 "血常规"
    department: str           # 所属科室，需与队列快照的 key 一致
    service_minutes: float    # 患者被占用的时长（叫号到检查结束），不含等报告
    requires_fasting: bool = False
    note: str = ""


@dataclass
class Prescription:
    """医嘱：医生开立的检查清单。

    用户端不再自选项目，只能按这份清单执行——对应「有了医生给的凭证才能去做」。
    """

    prescription_id: str
    patient_id: str
    patient_name: str
    department: str            # 就诊科室，如"内科"
    doctor: str
    items: List[str] = field(default_factory=list)   # 检查编码列表
    status: str = "pending"    # pending=待开单 / issued=已开单 / done=已完成
    created_at: Optional[datetime] = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "prescription_id": self.prescription_id,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "department": self.department,
            "doctor": self.doctor,
            "items": list(self.items),
            "status": self.status,
            "created_at": self.created_at.strftime("%H:%M") if self.created_at else "",
            "note": self.note,
        }


@dataclass
class Patient:
    """医生端看到的就诊患者。"""

    patient_id: str
    name: str
    age: int
    gender: str
    department: str            # 挂号科室中文名
    chief_complaint: str       # 主诉
    dept_code: str = ""        # 挂号科室编码（lab/ecg/us/rad/resp），用于医生端按科室隔离
    visit_no: str = ""         # 就诊号，如「检验科 J003」
    created_at: Optional[datetime] = None
    status: str = "waiting"    # waiting=待开单 / issued=已开单
    prescription_id: Optional[str] = None
    # 已取到的号：检查编码 -> 号（挂号粒度是「每个检查项目一个号」）
    tickets: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DepartmentState:
    """科室队列快照（方案 §3.2 输入B）。

    两种口径并存：

    wait_minutes  没取号时的等待——"此刻加入该科室队列要等多久"。
    serving_no    当前叫到几号。配合 service_minutes，可以算出**持号患者**
                  还要等多久：前方人数 = 我的号 − 当前叫号 − 1。
    """

    department: str
    queue_length: int
    wait_minutes: float
    updated_at: Optional[datetime] = None
    serving_no: Optional[int] = None          # 当前叫号（取号模式必需）
    service_minutes: Optional[float] = None    # 人均服务时长（取号模式必需）
    next_ticket: Optional[int] = None          # 此刻去取号会拿到的号


@dataclass(frozen=True)
class Dependency:
    """前置依赖：before 完成后，至少再等 lag_minutes 分钟，after 才能开始。"""

    rule_id: str
    before: str
    after: str
    lag_minutes: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class TimeWindow:
    """检查可执行的时间窗，用"距当日 00:00 的分钟数"表示。"""

    rule_id: str
    exam: str
    open_minute: int
    close_minute: int
    reason: str = ""


@dataclass
class PlanStep:
    """推荐序列中的一步。所有时间均为"距快照时刻的分钟数"。"""

    exam: Exam
    arrive_at: float          # 到达该科室的时刻
    wait_minutes: float       # 排队等待
    start_at: float           # 开始检查
    finish_at: float          # 检查结束
    forced_idle: float = 0.0  # 因等待前置结果而产生的空转
    ticket: Optional[int] = None   # 患者手里的号
    call_at: Optional[float] = None  # 该号预计被叫到的时刻（取号模式）
    missed: bool = False           # 号已过，本步改为现场重新取号
    shared_with: Optional[str] = None  # 与同科室某项共用一次到场（如一次采血）

    # 取号与检查解耦后新增
    wait_source: str = "estimate"  # ticket=按号精确 / estimate=按队列估算
    travel_minutes: float = 0.0    # 从上一处到本处的移动耗时
    needs_ticket: bool = False     # 本项尚未取号，执行前需要先去取

    @property
    def total_minutes(self) -> float:
        return self.wait_minutes + self.exam.service_minutes


@dataclass
class TicketReminder:
    """取号提醒：在等某一项的空档里，顺路去把另一项的号取了。"""

    at: float                      # 建议出发取号的时刻（相对快照，分钟）
    during_exam: str               # 正在等哪一项
    exam_code: str                 # 为哪一项取号
    exam_name: str
    department: str
    travel_minutes: float          # 单程耗时
    expect_call_at: float          # 取号后预计被叫到的时刻（相对快照，分钟）
    reason: str                    # 一句话解释

    def to_dict(self) -> dict:
        return {
            "at": round(self.at, 1),
            "during_exam": self.during_exam,
            "exam_code": self.exam_code,
            "exam_name": self.exam_name,
            "department": self.department,
            "travel_minutes": round(self.travel_minutes, 1),
            "expect_call_at": round(self.expect_call_at, 1),
            "reason": self.reason,
        }


@dataclass
class Plan:
    """一次求解的完整结果。"""

    steps: List[PlanStep] = field(default_factory=list)
    feasible: bool = True
    status: str = ""
    conflicts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    reminders: List[TicketReminder] = field(default_factory=list)
    nodes: int = 0
    elapsed_ms: float = 0.0
    # 前 N 步属于「已挂号块」。0 表示没有已挂号项目，整条路线是单一最优序列。
    # 展示层据此把路线切成「已挂号 / 未挂号」两段，求解本身不依赖这个字段。
    block_split: int = 0

    @property
    def total_minutes(self) -> float:
        return self.steps[-1].finish_at if self.steps else 0.0

    @property
    def total_wait(self) -> float:
        return sum(s.wait_minutes for s in self.steps)

    @property
    def total_idle(self) -> float:
        return sum(s.forced_idle for s in self.steps)

    def order(self) -> List[str]:
        return [s.exam.code for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "status": self.status,
            "total_minutes": round(self.total_minutes, 1),
            "total_wait_minutes": round(self.total_wait, 1),
            "total_idle_minutes": round(self.total_idle, 1),
            "order": self.order(),
            "steps": [
                {
                    "code": s.exam.code,
                    "name": s.exam.name,
                    "department": s.exam.department,
                    "arrive_at": round(s.arrive_at, 1),
                    "wait_minutes": round(s.wait_minutes, 1),
                    "start_at": round(s.start_at, 1),
                    "finish_at": round(s.finish_at, 1),
                }
                for s in self.steps
            ],
            "conflicts": self.conflicts,
            "notes": self.notes,
            "nodes": self.nodes,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }
