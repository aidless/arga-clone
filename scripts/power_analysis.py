"""功效分析：40 道题到底能不能分辨两个模型。

为什么必须先做这一步：
120 episodes 跑完，三个可用模型的分差是 10–27pt，但 McNemar 的 p 有 0.001、0.09、0.125 三种。
于是同一批数据能同时支持"显著"和"分不出"两种结论 —— 差别不在数据，在**样本量**。
在扩题之前先算清楚"要多少题才能稳定分辨 10pt"，否则 80 题跑完还是这套争论。

方法：**按观测到的配对不一致率做模拟**，不套公式。
    p01 = A 过 B 不过的比例，p10 = B 过 A 不过的比例
给定 N 道题，从这两个概率采样一张 2×2 表，跑与 `stats.py` 完全相同的
`mcnemar_exact`，统计 p<α 的次数比例 = 功效。

为什么模拟而不是正态近似：40 题、比例接近 1 时，McNemar 的近似的实际第一类错误
会偏离 0.05 不少；而这条流水线里所有结论都建立在"p<0.05"上，尺子本身偏了就没意义。
模拟用的是**同一个** mcnemar_exact，所以算出来的功效与真实判定口径一致。

用法：
    python3 scripts/power_analysis.py --from-json demo/agent_compare_480.json
    python3 scripts/power_analysis.py --from-json demo/agent_compare_480.json \
        --n 40 80 120 160 240 --delta 5 10 15 --out /tmp/power.json
"""

from __future__ import annotations

import argparse
import json
import random
from itertools import combinations
from pathlib import Path

from stats import mcnemar_exact

ALPHA = 0.05


def _pair_counts(rows: list[dict], a: str, b: str) -> dict[str, int]:
    """按 (题目, 轮次) 对齐，数出四格。用多数票：三轮里过两轮算过。"""
    by_key: dict[tuple[str, int], dict[str, str]] = {}
    for r in rows:
        key = (r["scenario"], r.get("run", 0))
        by_key.setdefault(key, {})[r["model"]] = r["verdict"]

    per_item: dict[str, list[tuple[str, str]]] = {}
    for (scenario, _run), d in by_key.items():
        if a in d and b in d:
            per_item.setdefault(scenario, []).append((d[a], d[b]))

    def majority(vs: list[str]) -> str:
        wins = sum(1 for v in vs if v == "PASS")
        return "PASS" if wins * 2 > len(vs) else "FAIL"

    both = a_only = b_only = neither = 0
    for vs in per_item.values():
        av = majority([x for x, _ in vs])
        bv = majority([y for _, y in vs])
        if av == "PASS" and bv == "PASS":
            both += 1
        elif av == "PASS":
            a_only += 1
        elif bv == "PASS":
            b_only += 1
        else:
            neither += 1
    return {"both": both, "a_only": a_only, "b_only": b_only, "neither": neither,
            "n": both + a_only + b_only + neither}


def _simulate(p01: float, p10: float, n: int, sims: int, seed: int) -> float:
    """在给定不一致率下，N 道题能把 p 压到 0.05 以下的概率。"""
    rng = random.Random(seed)
    hits = 0
    for _ in range(sims):
        b = sum(1 for _ in range(n) if rng.random() < p01)
        c = sum(1 for _ in range(n) if rng.random() < p10)
        if mcnemar_exact(b, c) < ALPHA:
            hits += 1
    return hits / sims


def power_table(rows: list[dict], models: list[str], ns: list[int],
                deltas: list[int], sims: int) -> list[dict]:
    out = []
    for a, b in combinations(models, 2):
        cnt = _pair_counts(rows, a, b)
        n = cnt["n"]
        if n == 0:
            continue
        p01 = cnt["a_only"] / n
        p10 = cnt["b_only"] / n
        for delta in deltas:
            # 目标：净差 = delta 个百分点。按观测到的总不一致率分配两个方向，
            # 只挪动净差 —— 保持"两者有多不一致"不变，只改它偏向谁。
            discord = p01 + p10
            want = delta / 100.0
            if discord < want:
                # 净差比总不一致还大，物理上不可能（p01,p10 ≥ 0 且差 = want）
                continue
            np01 = (discord + want) / 2
            np10 = (discord - want) / 2
            for n_req in ns:
                if n_req > n and n_req != n:
                    # 用同一组概率、更大的 N 重新采样（这是外推，不是观测）
                    pass
                pw = _simulate(max(np01, 0.0), max(np10, 0.0), n_req, sims,
                               seed=hash((a, b, delta, n_req)) & 0xFFFF)
                out.append({
                    "a": a, "b": b, "delta_pt": delta, "n": n_req,
                    "p01": round(np01, 4), "p10": round(np10, 4),
                    "power": round(pw, 3),
                    "observed": n_req == n,
                })
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-json", type=Path, required=True)
    ap.add_argument("--n", type=int, nargs="+", default=[40, 80, 120, 160, 240])
    ap.add_argument("--delta", type=int, nargs="+", default=[5, 10, 15, 20],
                    help="要分辨的净差（百分点）")
    ap.add_argument("--sims", type=int, default=400)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    rows = json.loads(args.from_json.read_text())
    models = sorted({r["model"] for r in rows})
    # 作废样本不进功效计算：它会把自己那一对的不一致率抬到 90% 以上，
    # 于是"要多少题"被一个通道故障的读数决定。
    models = [m for m in models if sum(1 for r in rows if r["model"] == m and r["verdict"] == "PASS") > 2]
    table = power_table(rows, models, args.n, args.delta, args.sims)

    print(f"功效分析（McNemar 精确检验，α={ALPHA}，每格 {args.sims} 次模拟）")
    print()
    hdr = f'{"A":26} {"B":26} {"净差":>5} {"N":>5} {"p01":>6} {"p10":>6} {"功效":>6}'
    print(hdr)
    print("-" * len(hdr))
    for r in table:
        mark = " *" if r["observed"] else ""
        print(f'{r["a"][:26]:26} {r["b"][:26]:26} {r["delta_pt"]:>4}pt {r["n"]:>5} '
              f'{r["p01"]:>6.3f} {r["p10"]:>6.3f} {r["power"]:>6.1%}{mark}')
    print()
    print("`*` = 与本次观测的题量相同，其余是按同一组不一致率外推的。")
    print("功效 < 0.8 的格子意味着：即使真实差距就是那么多，也有两成以上概率判成'分不出'。")

    if args.json:
        args.json.write_text(json.dumps({"alpha": ALPHA, "sims": args.sims, "rows": table},
                                        ensure_ascii=False, indent=2))
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
