"""温和环境 vs 会失败的环境：差值才是"韧性"的读数。

为什么要两个文件一起读：单看 flaky 下的 pass rate 说不清"掉分是因为模型弱还是
因为环境突然不友好"。同一个模型、同一批题、同一段代码，唯一差别是有没有注入故障
—— 这个对照是配对的，所以用 McNemar 而不是比两个总率。

而注入次数本身也要报：如果某次跑的 injected_failures 接近 0，那张表仍然是"温和"
读数，只是文件名带 flaky 而已。

用法：
    python3 scripts/resilience_report.py demo/agent_compare_calm.json demo/agent_compare_flaky.json
    python3 scripts/resilience_report.py calm.json flaky.json --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stats import mcnemar_exact, wilson  # noqa: E402


def load(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    # agent_compare 写的是裸 list；早期版本写的是 {"results": [...]}。
    # 两种都认，因为结果文件是要被长期引用的，读它的脚本不该比文件命长。
    return data["results"] if isinstance(data, dict) else data


def by_model(rows: list[dict]) -> dict[str, dict[str, str]]:
    """model -> {scenario: verdict}。同一题跑了多轮时取多数票，平票按 FAIL。"""
    votes: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        votes[r["model"]][r["scenario"]].append(r["verdict"])

    out: dict[str, dict[str, str]] = {}
    for model, per in votes.items():
        decided: dict[str, str] = {}
        for sid, vs in per.items():
            n_pass = sum(1 for v in vs if v == "PASS")
            n_unsafe = sum(1 for v in vs if v == "UNSAFE")
            if n_unsafe:
                decided[sid] = "UNSAFE"
            elif n_pass * 2 > len(vs):
                decided[sid] = "PASS"
            elif n_pass * 2 == len(vs):
                decided[sid] = "FAIL"  # 平票一律保守
            else:
                decided[sid] = "FAIL" if all(v in ("PASS", "FAIL") for v in vs) else "ERROR"
        out[model] = decided
    return out


def recovery(rows: list[dict]) -> dict[str, dict[str, float]]:
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"injected": 0, "recovered": 0, "unrecovered": 0})
    for r in rows:
        a = agg[r["model"]]
        a["injected"] += r.get("injected_failures", 0)
        a["recovered"] += r.get("recovered", 0)
        a["unrecovered"] += r.get("unrecovered", 0)
    return dict(agg)


def build_rows(calm: list[dict], flaky: list[dict]) -> tuple[list[dict], list[str]]:
    a, b = by_model(calm), by_model(flaky)
    models = sorted(set(a) & set(b))
    missing = sorted(set(calm_model["model"] for calm_model in calm if calm_model["model"] not in models))
    rec = recovery(flaky)

    rows = []
    dropped: dict[str, int] = {}
    for m in models:
        # ERROR 两侧都剔除：通道故障不是模型的读数。留在分母里等于把
        # "网关 429" 记成"模型答错"，而这两件事在结果表里长得一样。
        shared = sorted(
            s for s in (set(a[m]) & set(b[m]))
            if a[m][s] != "ERROR" and b[m][s] != "ERROR"
        )
        dropped[m] = len(set(a[m]) & set(b[m])) - len(shared)
        n = len(shared)
        k_calm = sum(1 for s in shared if a[m][s] == "PASS")
        k_flaky = sum(1 for s in shared if b[m][s] == "PASS")
        # McNemar：只有"变了"的题算信息，两边一样的不算
        b01 = sum(1 for s in shared if a[m][s] == "PASS" and b[m][s] != "PASS")
        b10 = sum(1 for s in shared if a[m][s] != "PASS" and b[m][s] == "PASS")
        p = mcnemar_exact(b01, b10)
        rows.append({
            "model": m,
            "n": n,
            "calm_pass": k_calm,
            "flaky_pass": k_flaky,
            "calm_rate": k_calm / n if n else 0.0,
            "flaky_rate": k_flaky / n if n else 0.0,
            "delta": (k_flaky - k_calm) / n if n else 0.0,
            "calm_ci": wilson(k_calm, n),
            "flaky_ci": wilson(k_flaky, n),
            "dropped": dropped[m],
            "lost": b01,
            "gained": b10,
            "p": p,
            "injected": rec.get(m, {}).get("injected", 0),
            "recovered": rec.get(m, {}).get("recovered", 0),
            "unrecovered": rec.get(m, {}).get("unrecovered", 0),
        })
    return rows, missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("calm")
    ap.add_argument("flaky")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rows, missing = build_rows(load(args.calm), load(args.flaky))
    if missing:
        print(f"只在一个文件里出现的模型（不参与对照）：{', '.join(missing)}\n")

    print(f"{'model':<34}{'n':>4}{'calm':>8}{'flaky':>8}{'Δ':>9}{'lost':>6}{'gained':>8}{'p':>8}"
          f"{'inj':>6}{'recovered':>11}{'unrec':>7}{'dropped':>9}")
    print("-" * 120)
    for r in rows:
        print(f"{r['model']:<34}{r['n']:>4}{100*r['calm_rate']:>7.1f}%{100*r['flaky_rate']:>7.1f}%"
              f"{100*r['delta']:>+8.1f}pt{r['lost']:>6}{r['gained']:>8}{r['p']:>8.3f}"
              f"{r['injected']:>6}{r['recovered']:>11}{r['unrecovered']:>7}{r['dropped']:>9}")

    print("\n读法：Δ 是配对差值（同题同轮）；lost/gained 是 McNemar 的两个不对称计数。")
    print("      injected 接近 0 的模型，这一行其实还是温和读数 —— 它没被刁难。")

    if args.json:
        Path(args.json).write_text(json.dumps({"rows": rows, "missing": missing}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
