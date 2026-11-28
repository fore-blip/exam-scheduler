"""比较固定队列快照下的枚举路线和 CP-SAT 路线。"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from .candidates import catalog_payload


TRAVEL_SECONDS = 240
BUFFER_NUMERATOR = 23
BUFFER_DENOMINATOR = 20


def buffered_wait(seconds):
    return (BUFFER_NUMERATOR * seconds + BUFFER_DENOMINATOR - 1) // BUFFER_DENOMINATOR


def travel_seconds(previous, current):
    return TRAVEL_SECONDS if previous and previous["department"] != current["department"] else 0


def timeline(order, exams, waits):
    steps = []
    finish = 0
    previous = None
    for code in order:
        exam = exams[code]
        travel = travel_seconds(previous, exam)
        arrival = finish + travel
        remaining = max(0, waits[exam["department"]] - arrival)
        wait = buffered_wait(remaining)
        start = arrival + wait
        finish = start + exam["service_minutes"] * 60
        steps.append({
            "exam_id": code, "name": exam["name"], "department": exam["department"],
            "travel": travel, "arrival": arrival, "wait": wait, "start": start, "finish": finish,
        })
        previous = exam
    return {"order": list(order), "total_seconds": finish, "steps": steps}


def solve_order(exams, waits, edges):
    from ortools.sat.python import cp_model

    codes = list(exams)
    horizon = sum(exam["service_minutes"] * 60 for exam in exams.values())
    horizon += len(codes) * (TRAVEL_SECONDS + buffered_wait(max(waits.values())))
    model = cp_model.CpModel()
    arrival, start, finish = {}, {}, {}
    for code, exam in exams.items():
        initial = waits[exam["department"]]
        arrival[code] = model.new_int_var(0, horizon, f"arrival_{code}")
        start[code] = model.new_int_var(0, horizon, f"start_{code}")
        finish[code] = model.new_int_var(0, horizon, f"finish_{code}")
        remaining = model.new_int_var(0, initial, f"remaining_{code}")
        wait = model.new_int_var(0, horizon, f"wait_{code}")
        model.add_max_equality(remaining, [0, initial - arrival[code]])
        # Encode ceiling division with two linear inequalities.
        model.add(BUFFER_DENOMINATOR * wait >= BUFFER_NUMERATOR * remaining)
        model.add(BUFFER_DENOMINATOR * wait <= BUFFER_NUMERATOR * remaining + BUFFER_DENOMINATOR - 1)
        model.add(start[code] == arrival[code] + wait)
        model.add(finish[code] == start[code] + exam["service_minutes"] * 60)

    arcs = []
    for i, first in enumerate([None, *codes]):
        for j, second in enumerate([None, *codes]):
            if i == j:
                continue
            follows = model.new_bool_var(f"arc_{i}_{j}")
            arcs.append((i, j, follows))
            if first is None:
                model.add(arrival[second] == 0).only_enforce_if(follows)
            elif second is not None:
                travel = travel_seconds(exams[first], exams[second])
                model.add(arrival[second] == finish[first] + travel).only_enforce_if(follows)
    model.add_circuit(arcs)
    for before, after in edges:
        model.add(start[after] >= finish[before])

    end = model.new_int_var(0, horizon, "end")
    model.add_max_equality(end, list(finish.values()))
    model.minimize(end)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_search_workers = 4
    status = solver.solve(model)
    route = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        order = sorted(codes, key=lambda code: solver.value(start[code]))
        route = timeline(order, exams, waits)
        if route["total_seconds"] != solver.value(end):
            raise RuntimeError("CP-SAT 与路线时间轴不一致")
    return {"status": solver.status_name(status), "route": route}


def optimize_candidates(candidates, snapshot):
    orders = candidates["sequences"]
    if not orders:
        return {**candidates, "optimization": None}
    catalog = {exam["exam_id"]: exam for exam in catalog_payload()["exams"]}
    exams = {
        row["exam_id"]: catalog[row["exam_id"]]
        for row in candidates["results"] if row["status"] == "当前可选"
    }
    queues = {row["department"]: row for row in snapshot["departments"]}
    for code, exam in exams.items():
        if not exam["service_minutes"] or exam["department"] not in queues:
            raise ValueError(f"{code} 缺少检查时长或科室队列数据。")
    waits = {
        name: int((Decimal(str(row["wait_minutes"])) * 60).to_integral_value(rounding=ROUND_CEILING))
        for name, row in queues.items()
    }
    edges = {
        (rule["before_exam_id"], rule["after_exam_id"])
        for rule in candidates["edges"]
        if rule["before_exam_id"] in exams and rule["after_exam_id"] in exams
    }
    routes = sorted((timeline(order, exams, waits) for order in orders), key=lambda row: row["total_seconds"])
    optimum = solve_order(exams, waits, edges)
    return {
        **candidates,
        "optimization": {
            "snapshot_time": snapshot["snapshot_time"],
            "routes": routes,
            **optimum,
        },
    }
