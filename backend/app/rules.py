"""检查项目目录与耦合规则库。

**这是整个项目唯一需要业务人员修改的文件。**
改完直接运行 `python main.py` 即可看到新的推荐结果，不需要动算法代码。

三个编辑区：
    1. EXAM_CATALOG  检查项目（科室、占用时长、是否需空腹）
    2. RULES         耦合规则（前置依赖 / 时间窗 / 互斥）
    3. 默认参数

规则类型（type 字段）
--------------------
"before"  前置依赖：before 完成后，至少间隔 lag_minutes 分钟，after 才能开始。
          语义是"必须拿到 before 的检查结果（或报告）才能做 after"。
          lag_minutes = 0 表示"人做完即可"，>0 表示还需等报告出具。

"window"  时间窗：该检查只能在 [start, end] 之间开始（当日时刻，24 小时制）。

"mutex"   互斥：两项检查不能同时开立。命中时不会自动删医嘱，而是报冲突交临床确认
          （方案 §2.2「检查项目相斥」）。

关于 service_minutes 与 lag_minutes 的区别
------------------------------------------
service_minutes 是"患者本人被占用"的时长（排队叫号、摆位、实际检查）；
lag_minutes 是"患者已经离开，但结果还没出来"的时长。
混用这两者会显著高估或低估总耗时，这是本模型最容易配错的地方。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import Dependency, Exam, TimeWindow

# ---------------------------------------------------------------------------
# 编辑区 1：检查项目
#   service_minutes  患者被占用的时长（分钟）
#   requires_fasting 是否要求空腹；患者未空腹时该检查不可执行
# ---------------------------------------------------------------------------
EXAM_CATALOG: Dict[str, dict] = {
    "CBC": {
        "name": "血常规",
        "department": "检验科",
        "service_minutes": 5,
        "requires_fasting": False,
        "note": "采血窗口，含一次穿刺",
    },
    "CRP": {
        "name": "C反应蛋白",
        "department": "检验科",
        "service_minutes": 5,
        "requires_fasting": False,
        "note": "可与血常规共用同一份血样",
    },
    "MP_AB": {
        "name": "支原体抗体",
        "department": "检验科",
        "service_minutes": 5,
        "requires_fasting": False,
        "note": "可与血常规共用同一份血样",
    },
    "BIO": {
        "name": "生化全套",
        "department": "检验科",
        "service_minutes": 10,
        "requires_fasting": True,
        "note": "需空腹采血",
    },
    "URINE": {
        "name": "尿常规",
        "department": "检验科",
        "service_minutes": 5,
        "requires_fasting": False,
        "note": "检验科窗口取杯",
    },
    "ECG": {
        "name": "心电图",
        "department": "心电科",
        "service_minutes": 15,
        "requires_fasting": False,
        "note": "需安静平卧，婴幼儿可能需安抚",
    },
    "ECHO": {
        "name": "心脏彩超",
        "department": "超声科",
        "service_minutes": 25,
        "requires_fasting": False,
        "note": "检查时长差异大，本值为均值",
    },
    "US_ABD": {
        "name": "腹部B超",
        "department": "超声科",
        "service_minutes": 20,
        "requires_fasting": True,
        "note": "需空腹，婴幼儿需镇静时另计",
    },
    "CXR": {
        "name": "胸部X线",
        "department": "放射科",
        "service_minutes": 10,
        "requires_fasting": False,
        "note": "含摆位与防护",
    },
    "PFT": {
        "name": "肺功能",
        "department": "呼吸科",
        "service_minutes": 20,
        "requires_fasting": False,
        "note": "需配合吹气，通常 5 岁以上可做",
    },
}

# ---------------------------------------------------------------------------
# 编辑区 2：耦合规则
#   下面 6 条为**示例假设**，用于演示规则引擎的能力，不代表真实临床要求。
#   真实规则须由医院业务与临床人员审核后替换（方案 §2.2 末尾的免责说明）。
# ---------------------------------------------------------------------------
RULES: List[dict] = [
    {
        "id": "R001",
        "type": "before",
        "before": "ECG",
        "after": "ECHO",
        "lag_minutes": 10,
        "reason": "心脏彩超需结合心电图报告判读，出报告约 10 分钟",
    },
    {
        "id": "R002",
        "type": "before",
        "before": "CBC",
        "after": "BIO",
        "lag_minutes": 0,
        "reason": "生化与血常规共用一次采血，须先完成采血",
    },
    {
        "id": "R003",
        "type": "before",
        "before": "CBC",
        "after": "MP_AB",
        "lag_minutes": 0,
        "reason": "支原体抗体复用同一份血样",
    },
    {
        "id": "R004",
        "type": "before",
        "before": "CBC",
        "after": "CRP",
        "lag_minutes": 0,
        "reason": "CRP 复用同一份血样",
    },
    {
        "id": "R005",
        "type": "window",
        "exam": "US_ABD",
        "start": "08:00",
        "end": "11:30",
        "reason": "超声科上午空腹检查时段",
    },
    {
        "id": "R006",
        "type": "window",
        "exam": "PFT",
        "start": "08:00",
        "end": "11:00",
        "reason": "肺功能室上午开放",
    },
    # 互斥示例：命中时不会自动删医嘱，只报冲突交临床确认。
    # {
    #     "id": "R007",
    #     "type": "mutex",
    #     "exams": ["CXR", "CT_CHEST"],
    #     "reason": "胸部影像二选一，需临床确认",
    # },
]

# ---------------------------------------------------------------------------
# 编辑区 3：默认参数
# ---------------------------------------------------------------------------
DEFAULT_DRAIN_RATE = 1.0   # 队列消退速率：1.0 完全消退，0.0 完全不消退（保守）
SOLVER_TIME_LIMIT = 5.0    # 单次求解时间上限（秒）

# --- 取号与检查解耦后的参数 ---
TRAVEL_MINUTES = 4.0       # 换科室 / 去取号的单程耗时（演示用固定值，后续可按科室距离细化）
ESTIMATE_BUFFER = 1.15     # 估算等待的安全系数：展示值比理论值长约 15%，降低过号概率
REMIND_BUFFER = 1.25       # 判断「来不来得及取号」时用更保守的系数
REMIND_MIN_WAIT = 8.0      # 等待不足这个分钟数就不值得跑一趟取号

# ----------------------------------------------------------------------
# 挂号科室（患者选这个，医生按这个分页面）
#
# 本演示采用「单层」结构：患者直接挂到检查科室，该科室医生开单。
# code 用于 URL（/doctor/lab），name 用于界面展示，prefix 用于生成就诊号。
# exams 是该科室自己能开的项目；医生页会把这些项目排在前面并标注「本科室」，
# 但仍可开具其他科室的项目（原需求：医生可开 CT、胃镜、心电图等）。
# ----------------------------------------------------------------------
DEPARTMENTS = [
    {"code": "lab",  "name": "检验科", "prefix": "J",
     "desc": "血常规、生化、炎症指标、尿常规",
     "exams": ["CBC", "BIO", "CRP", "MP_AB", "URINE"]},
    {"code": "ecg",  "name": "心电科", "prefix": "X",
     "desc": "心电图",
     "exams": ["ECG"]},
    {"code": "us",   "name": "超声科", "prefix": "C",
     "desc": "腹部B超、心脏彩超",
     "exams": ["US_ABD", "ECHO"]},
    {"code": "rad",  "name": "放射科", "prefix": "F",
     "desc": "胸部X线等影像检查",
     "exams": ["CXR"]},
    {"code": "resp", "name": "呼吸科", "prefix": "H",
     "desc": "肺功能",
     "exams": ["PFT"]},
]

DEPT_BY_CODE = {d["code"]: d for d in DEPARTMENTS}
DEPT_BY_NAME = {d["name"]: d for d in DEPARTMENTS}


def dept_of_code(code: str):
    """按 code 取科室配置，未知返回 None。"""
    return DEPT_BY_CODE.get(str(code or "").strip())


def dept_of_name(name: str):
    """按中文名取科室配置，未知返回 None。"""
    return DEPT_BY_NAME.get(str(name or "").strip())


# ---------------------------------------------------------------------------
# 以下为构建与校验逻辑，通常无需修改
# ---------------------------------------------------------------------------
def _parse_hhmm(text: str) -> int:
    """'08:30' -> 510（距当日 00:00 的分钟数）。"""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到：{text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间取值非法：{text!r}")
    return hour * 60 + minute


def build_catalog() -> Dict[str, Exam]:
    return {
        code: Exam(code=code, **spec)
        for code, spec in EXAM_CATALOG.items()
    }


def build_dependencies() -> List[Dependency]:
    deps: List[Dependency] = []
    for rule in RULES:
        if rule.get("type") != "before":
            continue
        deps.append(
            Dependency(
                rule_id=rule.get("id", ""),
                before=rule["before"],
                after=rule["after"],
                lag_minutes=float(rule.get("lag_minutes", 0)),
                reason=rule.get("reason", ""),
            )
        )
    return deps


def build_windows() -> List[TimeWindow]:
    windows: List[TimeWindow] = []
    for rule in RULES:
        if rule.get("type") != "window":
            continue
        windows.append(
            TimeWindow(
                rule_id=rule.get("id", ""),
                exam=rule["exam"],
                open_minute=_parse_hhmm(rule["start"]),
                close_minute=_parse_hhmm(rule["end"]),
                reason=rule.get("reason", ""),
            )
        )
    return windows


def mutex_pairs() -> List[tuple[str, str, str]]:
    pairs: List[tuple[str, str, str]] = []
    for rule in RULES:
        if rule.get("type") != "mutex":
            continue
        exams = rule.get("exams", [])
        for i in range(len(exams)):
            for j in range(i + 1, len(exams)):
                pairs.append((exams[i], exams[j], rule.get("reason", "")))
    return pairs


def find_cycle(edges: Dict[str, List[str]]) -> Optional[List[str]]:
    """在依赖图中找环，返回环上的节点序列；无环返回 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {node: WHITE for node in edges}
    stack: List[str] = []

    def dfs(node: str) -> Optional[List[str]]:
        color[node] = GRAY
        stack.append(node)
        for nxt in edges.get(node, []):
            if color.get(nxt, WHITE) == GRAY:
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt, WHITE) == WHITE:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in list(edges):
        if color[node] == WHITE:
            found = dfs(node)
            if found:
                return found
    return None


def validate() -> List[str]:
    """规则库自检，返回问题清单（空列表表示通过）。"""
    problems: List[str] = []

    codes = set(EXAM_CATALOG)
    seen_ids: set[str] = set()

    for code, spec in EXAM_CATALOG.items():
        for key in ("name", "department", "service_minutes"):
            if key not in spec:
                problems.append(f"检查项目 {code} 缺少字段 {key}")
        if spec.get("service_minutes", 0) <= 0:
            problems.append(f"检查项目 {code} 的 service_minutes 必须为正")

    edges: Dict[str, List[str]] = {code: [] for code in codes}

    for rule in RULES:
        rid = rule.get("id", "<无ID>")
        if rid in seen_ids:
            problems.append(f"规则 ID 重复：{rid}")
        seen_ids.add(rid)

        rtype = rule.get("type")
        if rtype == "before":
            before, after = rule.get("before"), rule.get("after")
            if before not in codes:
                problems.append(f"规则 {rid} 引用了未定义的检查项目：{before}")
            if after not in codes:
                problems.append(f"规则 {rid} 引用了未定义的检查项目：{after}")
            if before == after:
                problems.append(f"规则 {rid} 的前置项目与后置项目相同：{before}")
            if float(rule.get("lag_minutes", 0)) < 0:
                problems.append(f"规则 {rid} 的 lag_minutes 不能为负")
            if before in edges and after in edges and before != after:
                edges[before].append(after)

        elif rtype == "window":
            exam = rule.get("exam")
            if exam not in codes:
                problems.append(f"规则 {rid} 引用了未定义的检查项目：{exam}")
            try:
                open_min = _parse_hhmm(rule["start"])
                close_min = _parse_hhmm(rule["end"])
                if open_min >= close_min:
                    problems.append(f"规则 {rid} 的时间窗起点不早于终点")
            except (KeyError, ValueError) as exc:
                problems.append(f"规则 {rid} 时间窗解析失败：{exc}")

        elif rtype == "mutex":
            for exam in rule.get("exams", []):
                if exam not in codes:
                    problems.append(f"规则 {rid} 引用了未定义的检查项目：{exam}")

        else:
            problems.append(f"规则 {rid} 的 type 无法识别：{rtype!r}")

    cycle = find_cycle(edges)
    if cycle:
        problems.append("前置依赖存在环，无法排定顺序：" + " → ".join(cycle))

    return problems


def assert_valid() -> None:
    problems = validate()
    if problems:
        raise ValueError("规则库校验未通过：\n  - " + "\n  - ".join(problems))
