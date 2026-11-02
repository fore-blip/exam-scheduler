"""队列等待估算器（方案 §5.2）。

核心模型
--------
已知：科室当前排队 Q 人，此刻加入的预计等待为 W0 分钟。
推论：该科室在 W0 分钟内会把"当前这批人"消化完，即队列按每分钟 1 分钟等待的速率消退。
于是 t 分钟后到达时：

    wait(t) = max(0, W0 - drain_rate * t)

为什么这个式子成立（而不需要知道服务台数量）
--------------------------------------------
设服务台 c 个、单人服务 s 分钟，则 W0 = Q * s / c，吞吐率为 c/s 人/分钟。
t 分钟后，队列已消化 t*c/s 人，剩余 Q - t*c/s；此时加入的等待为
    (Q - t*c/s) * s/c = Q*s/c - t = W0 - t
与 c、s 的具体取值无关，只与 W0 有关。这正是方案 §1.3 "队列在患者等待期间仍会推进"的量化表达。

两个可调参数
------------
drain_rate = 1.0  默认。当前这批患者会被消化完，新到达者视为不确定性不参与建模（方案 §5.2 队列演化假设）。
drain_rate = 0.0  保守情景。假设不断有新患者补位，队列长度不消退，等待不随时间下降。
                  用它可以给出"悲观估计"，与默认情景一起构成方案 §5.2 要求的区间评估。
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from .models import DepartmentState


class QueueEstimator:
    """按"冻结快照 + 线性消退"估算任意时刻的排队等待。"""

    def __init__(self, states: Iterable[DepartmentState], drain_rate: float = 1.0) -> None:
        if not 0.0 <= drain_rate <= 1.0:
            raise ValueError("drain_rate 应位于 [0, 1]：1 表示队列完全消退，0 表示完全不消退")
        self._states: Dict[str, DepartmentState] = {s.department: s for s in states}
        self._drain = float(drain_rate)

    @property
    def drain_rate(self) -> float:
        return self._drain

    def has(self, department: str) -> bool:
        return department in self._states

    def state(self, department: str) -> Optional[DepartmentState]:
        return self._states.get(department)

    def departments(self):
        return self._states.keys()

    def wait_now(self, department: str) -> float:
        """此刻加入该科室的预计等待（即快照原值）。"""
        return self.wait_at(department, 0.0)

    def wait_at(self, department: str, offset_minutes: float) -> float:
        """距快照 offset_minutes 分钟后到达时，预计还要等多久。"""
        st = self._states.get(department)
        if st is None:
            raise KeyError(f"队列快照中缺少科室「{department}」，无法估算等待时间")
        if offset_minutes <= 0:
            return max(0.0, st.wait_minutes)
        return max(0.0, st.wait_minutes - self._drain * offset_minutes)

    def service_estimate(self, department: str) -> Optional[float]:
        """由"排队人数 / 等待时长"反推的单人平均服务时长，仅用于数据自检。"""
        st = self._states.get(department)
        if st is None or st.queue_length <= 0 or st.wait_minutes <= 0:
            return None
        return st.wait_minutes / st.queue_length

    # ------------------------------------------------------------------
    # 取号模式
    # ------------------------------------------------------------------
    def service_minutes(self, department: str) -> float:
        """人均服务时长。优先用科室上报值，否则由等待/人数反推。"""
        st = self._states.get(department)
        if st is None:
            raise KeyError(f"队列快照中缺少科室「{department}」")
        if st.service_minutes and st.service_minutes > 0:
            return float(st.service_minutes)
        derived = self.service_estimate(department)
        return derived if derived and derived > 0 else 3.0

    def serving_no(self, department: str) -> Optional[int]:
        st = self._states.get(department)
        return None if st is None else st.serving_no

    def supports_ticket(self, department: str) -> bool:
        """该科室是否上报了当前叫号；没有的话无法按号计算。"""
        return self.serving_no(department) is not None

    def ahead_count(self, department: str, ticket: int) -> Optional[int]:
        """我的号前面还有几个人（不含正在服务的那位）。"""
        current = self.serving_no(department)
        if current is None:
            return None
        return max(0, int(ticket) - current - 1)

    def call_offset(self, department: str, ticket: int) -> Optional[float]:
        """我的号预计还有多少分钟被叫到（相对快照时刻）。

        口径：科室当前叫到 N，每个患者占用 s 分钟，
        我的号 T 要等 (T - N) 个服务时长。T < N 表示号已被叫过（过号作废）。
        """
        current = self.serving_no(department)
        if current is None:
            return None
        return (int(ticket) - current) * self.service_minutes(department)

    def ticket_state(self, department: str, ticket: int) -> str:
        """取号状态：waiting / now / missed / unknown。"""
        current = self.serving_no(department)
        if current is None:
            return "unknown"
        if int(ticket) < current:
            return "missed"
        if int(ticket) == current:
            return "now"
        return "waiting"

    def consistency_warnings(self) -> list[str]:
        """数据合理性自检：人均服务时长明显异常时给出提示（方案 §10 风险1）。"""
        warnings: list[str] = []
        for dept, st in self._states.items():
            if st.queue_length > 0 and st.wait_minutes <= 0:
                warnings.append(f"{dept}：{st.queue_length} 人排队但等待为 0 分钟，口径可能不一致")
                continue
            s = self.service_estimate(dept)
            if s is not None and (s < 1.0 or s > 60.0):
                warnings.append(
                    f"{dept}：反推人均服务时长 {s:.1f} 分钟，超出常见范围，建议核对排队人数口径"
                )
        return warnings
