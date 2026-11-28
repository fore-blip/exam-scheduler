"""按当前患者状态筛选下一项检查的规则库演示。"""

from __future__ import annotations

import argparse
import json
from .candidates import evaluate_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("orders", nargs="+", help="医嘱项目编码，例如 EX01 EX05")
    parser.add_argument("--met", nargs="*", default=[], help="已满足的生理状态编码")
    parser.add_argument("--unmet", nargs="*", default=[], help="未满足的生理状态编码")
    parser.add_argument("--confirmed", nargs="*", default=[], help="前置要求已核实的规则编码")
    args = parser.parse_args()
    if set(args.met) & set(args.unmet):
        parser.error("同一生理状态不能同时为已满足和未满足")
    states = dict.fromkeys(args.met, True)
    states.update(dict.fromkeys(args.unmet, False))
    try:
        results = evaluate_candidates(args.orders, states, set(args.confirmed))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
