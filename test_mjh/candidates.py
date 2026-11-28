"""SQLite ????????"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from itertools import islice, permutations
from pathlib import Path

DATABASE = Path(__file__).resolve().parents[1] / "backend" / "config" / "medical_rules.sqlite3"
PRECEDENCE_CONFIG = DATABASE.with_name("exam_precedence.json")

# 对表 A 中少数明确条件的演示映射。关联表本身只表示相关性。
DEMO_REQUIREMENTS = {
    "EX01": (),
    "EX05": ("S01",),
    "EX06": (),
    "EX15": ("S01",),
    "EX18": ("S02",),
    "EX19": ("S02",),
    "EX33": ("S05",),
    "EX37": ("S05",),
    "EX38": (),
}

PREREQUISITES_SQL = """
    SELECT rule_id, before_node, after_exam_id, timing, before_exam_id
    FROM precedence_rules
    WHERE origin = 'direct' AND strength = 'H' AND after_exam_id IS NOT NULL
    UNION ALL
    SELECT rule.rule_id, exam.name, rule.after_exam_id, '需有已核实的前置结果', rule.before_exam_id
    FROM exam_precedence_rules AS rule
    JOIN exam_catalog AS exam ON exam.exam_id = rule.before_exam_id
    WHERE rule.requires_prior_result = 1
    ORDER BY rule_id
"""


def catalog_payload() -> dict:
    with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return {
            "exams": [dict(row) for row in db.execute(
                "SELECT exam_id, name, department, service_minutes FROM exam_catalog ORDER BY exam_id"
            )],
            "conditions": [dict(row) for row in db.execute(
                "SELECT condition_id, name, assessment FROM physiological_conditions ORDER BY condition_id"
            )],
            "required_conditions": [
                dict(row) for row in db.execute(
                    "SELECT exam_id, condition_id FROM exam_condition_links ORDER BY exam_id, condition_id"
                )
                if row["condition_id"] in DEMO_REQUIREMENTS.get(row["exam_id"], ())
            ],
            "prerequisites": [dict(row) for row in db.execute(PREREQUISITES_SQL)],
            "exam_precedence": [dict(row) for row in db.execute(
                """
                SELECT rule_id, before_exam_id, after_exam_id, recommended_gap_minutes
                FROM exam_precedence_rules
                ORDER BY rule_id
                """
            )],
        }


def load_config_precedence(exam_ids: set[str], existing_rule_ids: set[str]) -> list[dict]:
    try:
        config = json.loads(PRECEDENCE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取 exam_precedence.json，请检查文件和 JSON 格式。") from exc
    rules = config.get("rules") if isinstance(config, dict) else None
    if not isinstance(rules, list):
        raise ValueError("exam_precedence.json 的 rules 必须是列表。")
    used_ids = set(existing_rule_ids)
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise ValueError(f"exam_precedence.json 第 {index} 条规则必须是对象。")
        rule_id = rule.get("rule_id")
        before, after = rule.get("before_exam_id"), rule.get("after_exam_id")
        gap, note = rule.get("recommended_gap_minutes"), rule.get("note")
        if not isinstance(rule_id, str) or not rule_id.startswith("EP-") or len(rule_id) <= 3 or rule_id in used_ids:
            raise ValueError(f"exam_precedence.json 第 {index} 条规则编号无效或重复。")
        if not all(isinstance(code, str) and code in exam_ids for code in (before, after)) or before == after:
            raise ValueError(f"{rule_id} 必须引用两个不同的检查编码。")
        if gap is not None and (type(gap) is not int or gap < 0):
            raise ValueError(f"{rule_id} 的建议间隔须为非负整数或 null。")
        if not isinstance(note, str) or not note.strip():
            raise ValueError(f"{rule_id} 缺少规则说明 note。")
        used_ids.add(rule_id)
    return rules


def evaluate_candidates(
    orders: list[str],
    states: dict[str, bool | None],
    confirmed_prerequisites: set[str],
    *,
    allow_scheduled_prerequisites: bool = False,
) -> list[dict]:
    """confirmed_prerequisites 保存已核实满足前置要求的规则 ID。"""
    with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as db:
        catalog = dict(db.execute("SELECT exam_id, name FROM exam_catalog"))
        condition_names = dict(db.execute(
            "SELECT condition_id, name FROM physiological_conditions"
        ))
        linked_conditions = set(db.execute(
            "SELECT exam_id, condition_id FROM exam_condition_links"
        ))
        prerequisites = db.execute(PREREQUISITES_SQL).fetchall()

    for exam_id in orders:
        if exam_id not in catalog:
            raise ValueError(f"未知医嘱编码 {exam_id}")
    for state_id in states:
        if state_id not in condition_names:
            raise ValueError(f"未知生理状态编码 {state_id}")
    known_prerequisites = {rule[0] for rule in prerequisites}
    for rule_id in sorted(confirmed_prerequisites - known_prerequisites):
        raise ValueError(f"该前置规则编码未纳入演示判定：{rule_id}")

    results = []
    for exam_id in dict.fromkeys(orders):
        blockers = []
        pending = []
        suggestions = []
        for rule_id, before_node, after_exam_id, timing, before_exam_id in prerequisites:
            scheduled = allow_scheduled_prerequisites and before_exam_id in orders
            if after_exam_id == exam_id and rule_id not in confirmed_prerequisites and not scheduled:
                blockers.append(f"{rule_id} 缺少已核实的前置记录（{before_node}，{timing}）")
                suggestions.append(f"请由科室核实「{before_node}」及对应要求，确认后重新生成。")

        requirements = DEMO_REQUIREMENTS.get(exam_id)
        if requirements is None:
            pending.append("该项目的准备要求尚未加入演示判定")
            suggestions.append("请向检查科室确认准备要求，并补录可执行规则。")
        else:
            for state_id in requirements:
                if (exam_id, state_id) not in linked_conditions:
                    pending.append(f"{state_id} 缺少检查与生理状态的关联配置")
                    suggestions.append(f"请核对并补录 {state_id} 的关联配置。")
                    continue
                value = states.get(state_id)
                condition = condition_names[state_id]
                if value is False:
                    blockers.append(f"当前不满足{condition}要求")
                    suggestions.append(f"请向检查科室确认{condition}的准备要求，满足后更新状态。")
                elif value is None:
                    pending.append(f"{condition}状态待确认")
                    suggestions.append(f"请核实{condition}状态并更新表单。")

        if blockers:
            status = "暂不可选"
        elif pending:
            status = "待确认"
        else:
            status = "当前可选"
        results.append({
            "exam_id": exam_id,
            "name": catalog[exam_id],
            "status": status,
            "reasons": blockers + pending,
            "suggestions": suggestions,
        })
    return results


def enumerate_sequences(orders: list[str], edges: list[dict], limit: int) -> list[list[str]]:
    """枚举拓扑顺序，最多返回 limit 条。"""
    predecessors = {code: set() for code in orders}
    for edge in edges:
        predecessors[edge["after_exam_id"]].add(edge["before_exam_id"])

    unchecked = set(orders)
    while unchecked:
        ready = {code for code in unchecked if predecessors[code].isdisjoint(unchecked)}
        if not ready:
            return []
        unchecked.difference_update(ready)

    sequences: list[list[str]] = []

    def visit(prefix: list[str], remaining: list[str]) -> None:
        if not remaining:
            sequences.append(prefix)
            return
        for code in remaining:
            if predecessors[code].isdisjoint(remaining):
                visit(prefix + [code], [item for item in remaining if item != code])
                if len(sequences) >= limit:
                    return

    visit([], orders)
    return sequences


def rejected_sequences(
    codes: list[str], edges: list[dict], project_reasons: list[str], limit: int,
) -> list[dict]:
    if project_reasons or any(edge["before_exam_id"] == edge["after_exam_id"] for edge in edges):
        orders = list(islice(permutations(codes), limit))
    else:
        # Every invalid permutation reverses at least one edge. Generate from those
        # reversed edges directly so valid permutations never delay this list.
        orders = []
        seen = set()
        for edge in edges:
            reverse = {"before_exam_id": edge["after_exam_id"], "after_exam_id": edge["before_exam_id"]}
            for order in enumerate_sequences(codes, [reverse], limit):
                key = tuple(order)
                if key not in seen:
                    seen.add(key)
                    orders.append(key)
                    if len(orders) >= limit:
                        break
            if len(orders) >= limit:
                break

    rejected = []
    for order in orders:
        positions = {code: index for index, code in enumerate(order)}
        reasons = list(project_reasons)
        for edge in edges:
            before, after = edge["before_exam_id"], edge["after_exam_id"]
            if positions[before] >= positions[after]:
                reasons.append(f"{edge['rule_id']} 要求 {before} 先于 {after}，该排列无法满足。")
        rejected.append({"order": list(order), "reasons": reasons})
    return rejected


def evaluate_sequences(
    orders: list[str], states: dict[str, bool | None], confirmed: set[str],
) -> dict:
    results = evaluate_candidates(orders, states, confirmed, allow_scheduled_prerequisites=True)
    codes = [result["exam_id"] for result in results]
    rules = catalog_payload()
    order_rules = {
        rule["rule_id"]: rule for rule in rules["prerequisites"]
        if rule["rule_id"] not in confirmed
    }
    order_rules.update((rule["rule_id"], rule) for rule in rules["exam_precedence"])
    config_rules = load_config_precedence(
        {exam["exam_id"] for exam in rules["exams"]},
        set(order_rules) | {rule["rule_id"] for rule in rules["prerequisites"]},
    )
    order_rules.update((rule["rule_id"], rule) for rule in config_rules)
    edges = [
        rule for rule in order_rules.values()
        if rule["before_exam_id"] in codes and rule["after_exam_id"] in codes
    ]
    by_code = {result["exam_id"]: result for result in results}
    deferred = {code for code, result in by_code.items() if result["status"] != "当前可选"}
    while True:
        also_deferred = {
            edge["after_exam_id"] for edge in edges if edge["before_exam_id"] in deferred
        }
        if also_deferred <= deferred:
            break
        deferred.update(also_deferred)
    for edge in edges:
        before, after = edge["before_exam_id"], edge["after_exam_id"]
        if before in deferred:
            result = by_code[after]
            result["status"] = "暂不可选"
            result["reasons"].append(f"{edge['rule_id']} 的前置项目 {before} {by_code[before]['name']} 已暂缓。")
            result["suggestions"].append(f"先处理前置项目 {before} 的待办条件，或核实已有前置结果。")

    eligible = [code for code in codes if code not in deferred]
    active_edges = [edge for edge in edges if edge["before_exam_id"] in eligible and edge["after_exam_id"] in eligible]
    limit = 100
    sequences = []
    reason = None
    project_reasons = [
        f"{result['exam_id']} {result['name']}（{result['status']}）：{detail}"
        for result in results if result["status"] != "当前可选"
        for detail in result["reasons"]
    ]
    if not eligible:
        reason = "所有项目均暂缓，当前没有可排列的项目。"
    else:
        sequences = enumerate_sequences(eligible, active_edges, limit + 1)
        if not sequences:
            reason = "项目先后约束存在环，无法排列。"
    rejected = rejected_sequences(codes, edges, project_reasons, limit + 1)
    return {
        "results": results,
        "edges": edges,
        "sequences": sequences[:limit],
        "deferred_count": len(deferred),
        "truncated": len(sequences) > limit,
        "rejected": rejected[:limit],
        "rejected_truncated": len(rejected) > limit,
        "reason": reason,
    }
