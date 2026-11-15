"""把 agent_compare 的结果折成可引用的数字。

为什么单独一个脚本：README 里每个百分数都要能从这里重算出来。
手写数字（"glm 大约 95%"）在下一轮跑完就会过期，而过期的数字比没有数字更糟 ——
读者无法分辨哪个是真读数、哪个是上一轮的记忆。

三件事：
1. 每个模型的 pass rate + **Wilson 95% 置信区间**（不是 ±sqrt(p(1-p)/n) 的
   正态近似 —— 40 题、p 接近 0.9 时那个近似会给出下界溢出的区间）。
2. 每域 pass rate（附分母）。8 题的域，一格 = 12.5pt，不能当精度读。
3. 两模型之间的**配对**比较用 McNemar 精确检验：同一道题，A 过 B 不过 vs
   B 过 A 不过。跨题比总分会把"题目难度差异"混进来。

用法：
    python3 scripts/stats.py demo/agent_compare_480.json
    python3 scripts/stats.py demo/agent_compare_480.json --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

Z = 1.959963984540054  # 95%


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval。小样本、极端比例都成立。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def mcnemar_exact(b: int, c: int) -> float:
    """双侧精确 McNemar（二项检验 p=0.5）。b/c 是两张不一致的对数。"""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def majority(verdicts: list[str]) -> str:
    """多轮取多数票。平票（如 3 轮里 1 PASS 1 FAIL 1 ERROR）算 FAIL —— 不猜。"""
    c = Counter(verdicts)
    top = max(c.values())
    winners = [v for v, n in c.items() if n == top]
    if len(winners) == 1:
        return winners[0]
    return "FAIL" if "PASS" not in winners else "FAIL"


def load(path: str) -> list[dict]:
    return json.loads(Path(path).read_text())


def aggregate(rows: list[dict]) -> dict:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    out = {"models": {}, "domains": {}, "pairs": [], "environment": {}}

    for model, rs in sorted(by_model.items()):
        n = len(rs)
        k = sum(1 for r in rs if r["verdict"] == "PASS")
        lo, hi = wilson(k, n)
        out["models"][model] = {
            "episodes": n,
            "pass": k,
            "pass_rate": k / n if n else 0.0,
            "ci95": [lo, hi],
            "unsafe": sum(1 for r in rs if r["verdict"] == "UNSAFE"),
            "error": sum(1 for r in rs if r["verdict"] == "ERROR"),
            "avg_calls": round(sum(r.get("calls") or 0 for r in rs) / n, 1) if n else 0,
        }

    # 分域：每 (模型, 域) 一个 pass rate + 分母
    per_domain: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rows:
        d = r.get("domain") or "?"
        cell = per_domain[d][r["model"]]
        cell[1] += 1
        if r["verdict"] == "PASS":
            cell[0] += 1
    for d, models in sorted(per_domain.items()):
        out["domains"][d] = {
            m: {"pass": k, "n": n, "rate": k / n if n else 0.0}
            for m, (k, n) in sorted(models.items())
        }

    # 配对：同题多轮先取多数票，再跨模型逐题配对
    per_task: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in rows:
        per_task[(r["model"], r["scenario"])].append(r["verdict"])
    majority_verdict = {k: majority(v) for k, v in per_task.items()}
    tasks = sorted({sc for (_, sc) in majority_verdict})

    models = sorted(by_model)
    for i in range(len(models)):
        for j in range(i + 1, len(models)):
            a, b = models[i], models[j]
            wins_a = wins_b = both = neither = 0
            for sc in tasks:
                va = majority_verdict.get((a, sc))
                vb = majority_verdict.get((b, sc))
                pa, pb = va == "PASS", vb == "PASS"
                if pa and pb:
                    both += 1
                elif pa and not pb:
                    wins_a += 1
                elif pb and not pa:
                    wins_b += 1
                else:
                    neither += 1
            out["pairs"].append({
                "a": a, "b": b,
                "a_only": wins_a, "b_only": wins_b,
                "both_pass": both, "neither": neither,
                "delta": (wins_a - wins_b) / len(tasks) if tasks else 0.0,
                "p_exact": mcnemar_exact(wins_a, wins_b),
            })

    # 环境侧：这些数不是模型的能力，是环境的噪声底
    err = Counter()
    for r in rows:
        for cid in r.get("outcome_failures") or []:
            err[f"outcome:{cid}"] += 1
        for cid in r.get("safety_failures") or []:
            err[f"safety:{cid}"] += 1
    out["environment"] = {
        "stub_calls": sum(r.get("stub_calls") or 0 for r in rows),
        "parse_failures": sum(r.get("parse_failures") or 0 for r in rows),
        "unfinished": sum(1 for r in rows if r.get("finished") is False),
        "episodes": len(rows),
        "top_failing_checks": err.most_common(15),
    }
    return out


def print_report(agg: dict) -> None:
    print("=" * 78)
    print(f"{'model':<34}{'pass':>6}{'n':>6}{'rate':>8}   95% Wilson CI")
    print("-" * 78)
    for m, s in agg["models"].items():
        lo, hi = s["ci95"]
        print(f"{m:<34}{s['pass']:>6}{s['episodes']:>6}{s['pass_rate']*100:>7.1f}%"
              f"   [{lo*100:.1f}%, {hi*100:.1f}%]  unsafe={s['unsafe']} err={s['error']}")

    print()
    print("per-domain pass rate (分母 = 该域题数 × 轮数)")
    domains = sorted(agg["domains"])
    models = list(agg["models"])
    print(f"{'domain':<22}" + "".join(f"{m[:20]:>22}" for m in models))
    for d in domains:
        row = f"{d:<22}"
        for m in models:
            cell = agg["domains"][d].get(m)
            row += f"{cell['pass']}/{cell['n']} ({cell['rate']*100:.0f}%)".rjust(22) if cell else " " * 22
        print(row)

    print()
    print("配对比较（同题多轮取多数票后逐题对齐，McNemar 精确检验）")
    for p in agg["pairs"]:
        print(f"  {p['a']} vs {p['b']}: "
              f"仅前者过 {p['a_only']}，仅后者过 {p['b_only']}，"
              f"都过 {p['both_pass']}，都不过 {p['neither']}  "
              f"→ Δ={p['delta']*100:+.1f}pt, p={p['p_exact']:.3f}"
              + ("  (显著)" if p["p_exact"] < 0.05 else "  (不显著)"))

    e = agg["environment"]
    print()
    print("环境噪声（不是模型能力）")
    print(f"  episodes={e['episodes']}  stub_calls={e['stub_calls']}  "
          f"parse_failures={e['parse_failures']}  未跑完={e['unfinished']}")
    print("  最高频失败项：")
    for cid, n in e["top_failing_checks"][:10]:
        print(f"    {n:>3}x  {cid}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rows = load(args.results)
    agg = aggregate(rows)
    print_report(agg)
    if args.json:
        Path(args.json).write_text(json.dumps(agg, indent=2, ensure_ascii=False) + "\n")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
