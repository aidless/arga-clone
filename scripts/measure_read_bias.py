#!/usr/bin/env python3
"""量化“先读再改”在预算判据下的成本。

从已落盘的 episode JSON 里重算：超预算率、超预算 episode 里的读/写拆分、
以及“额外调用主要来自读”的比例。只读 demo/*.json 与 scenarios/budget.py，
不跑模型、不改世界。

用法:
    python3 scripts/measure_read_bias.py
    python3 scripts/measure_read_bias.py demo/agent_compare_calm.json
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scenarios"))
from budget import GOLD_CALLS, budget_for  # type: ignore

DEFAULTS = [
    "demo/agent_compare_calm.json",
    "demo/agent_compare_ambiguous.json",
    "demo/agent_compare_80x5.json",
    "demo/agent_compare_clientretry.json",
    "demo/agent_compare_budget80.json",
]


def load(path: Path):
    d = json.loads(path.read_text())
    return d if isinstance(d, list) else d.get("results") or d.get("episodes") or []


def analyze(path: Path):
    rows = load(path)
    over = []
    n = 0
    for r in rows:
        sid = r.get("scenario") or r.get("scenario_id")
        if sid not in GOLD_CALLS:
            continue
        n += 1
        calls = r.get("calls_detail") or []
        calls_n = len(calls)
        g = GOLD_CALLS[sid]
        b = budget_for(g)
        if calls_n > b:
            reads = sum(1 for c in calls if c.get("method") == "GET")
            writes = calls_n - reads
            excess = calls_n - g
            # “读主导额外调用”：读占超出 gold 的多数（gold 里读通常很少/为0）
            read_dominant_extra = excess > 0 and reads >= excess * 0.6
            over.append({
                "scenario": sid, "verdict": r.get("verdict"),
                "calls": calls_n, "gold": g, "budget": b,
                "reads": reads, "writes": writes, "excess": excess,
                "read_dominant_extra": read_dominant_extra,
                "model": r.get("model"),
            })
    return n, over


def report(path: Path):
    n, over = analyze(path)
    if n == 0:
        print(f"{path.name}: no matching episodes")
        return
    over_pass = [x for x in over if x["verdict"] == "PASS"]
    tot_rw = sum(x["reads"] + x["writes"] for x in over) or 1
    read_share = sum(x["reads"] for x in over) / tot_rw
    read_dom = sum(1 for x in over if x["read_dominant_extra"])
    print(f"== {path.name} ==")
    print(f"episodes={n}  over-budget={len(over)} ({len(over)/n:.1%})  over&PASS={len(over_pass)}  over&FAIL={len(over)-len(over_pass)}")
    print(f"read share in over-budget calls: {read_share:.1%}  read-dominant-extra: {read_dom}/{len(over)}")
    # show worst/read-heavy examples
    for x in sorted(over, key=lambda z: (-int(z["read_dominant_extra"]), -z["calls"]))[:8]:
        print(f"  {x['scenario']:42s} {x['verdict']:6s} n={x['calls']:2d} gold={x['gold']} bud={x['budget']} reads={x['reads']} writes={x['writes']}")
    print()


def main(argv):
    paths = [Path(a) for a in argv[1:]] if len(argv) > 1 else [ROOT / p for p in DEFAULTS]
    for p in paths:
        if not p.exists():
            print(f"skip missing: {p}")
            continue
        report(p)


if __name__ == "__main__":
    main(sys.argv)
