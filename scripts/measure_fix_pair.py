#!/usr/bin/env python3
"""修复前后对照读数（README §31）。

只读落盘 JSON，不重跑。两臂同模型、同时间窗、逐块交错跑，所以差异可以归给
题库改动（驱动脚本已自证两套题库只差那两道题）。

三条口径必须一起报，缺一条就会把结论读错：

1. **两题单独看**：修复前后那两道题的通过率。它们从 0% / 6.7% 跳上去是**必然的**，
   不是"模型变强了"，所以这一格只用来确认修复生效，不参与任何配对检验。
2. **全库配对**：把这两题**排除**后剩下的 83 题做逐 episode 配对。包含它们会把
   "判据改了"的效果算成"行为变了"，是 §29.2 那种口径混用。
3. **ERROR 不进分母**：本仓口径。两臂丢弃数不同必须并列，否则"看起来提升了"
   可能只是这一臂扔掉的更多。
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from stats import mcnemar_exact, wilson  # noqa: E402

FIXED = ("ecom-02-cancel-without-refund", "dev-14-record-deployment-status")
ARMS = {"before": "demo/fixpair_before.json", "after": "demo/fixpair_after.json"}


def usable(rows):
    """ERROR / 通道断的发不进分母（本仓口径）。"""
    return [r for r in rows if not r.get("channel_error") and r.get("verdict") != "ERROR"]


def load(path):
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"缺文件：{path}（先跑 scripts/prefix_pair_scan.sh）")
    return json.loads(p.read_text())


def rate_block(name, rows):
    ok = usable(rows)
    n = len(ok)
    passed = sum(1 for r in ok if r.get("verdict") == "PASS")
    lo, hi = wilson(passed, n) if n else (0.0, 0.0)
    return {
        "arm": name,
        "rows_total": len(rows),
        "n": n,
        "dropped": len(rows) - n,
        "pass": passed,
        "fail": sum(1 for r in ok if r.get("verdict") == "FAIL"),
        "unsafe": sum(1 for r in ok if r.get("verdict") == "UNSAFE"),
        "rate": passed / n if n else 0.0,
        "ci": [lo, hi],
    }


def per_scenario(rows):
    out: dict[str, dict[str, int]] = {}
    for r in usable(rows):
        sid = r.get("scenario") or ""
        d = out.setdefault(sid, {"n": 0, "pass": 0})
        d["n"] += 1
        d["pass"] += 1 if r.get("verdict") == "PASS" else 0
    return out


def main() -> int:
    before = load(ARMS["before"])
    after = load(ARMS["after"])

    print("== 各臂总览（ERROR 不进分母）")
    for name, rows in (("before", before), ("after", after)):
        b = rate_block(name, rows)
        print(
            f"  {b['arm']:7s} n={b['n']:3d}/{b['rows_total']} dropped={b['dropped']:3d} "
            f"PASS={b['pass']:3d} FAIL={b['fail']:3d} UNSAFE={b['unsafe']:2d} "
            f"rate={b['rate']*100:5.1f}% [{b['ci'][0]*100:.1f},{b['ci'][1]*100:.1f}]"
        )

    print("\n== 修掉的两题（必然跳变，不参与配对检验）")
    pb, pa = per_scenario(before), per_scenario(after)
    for sid in FIXED:
        b = pb.get(sid, {"n": 0, "pass": 0})
        a = pa.get(sid, {"n": 0, "pass": 0})
        print(f"  {sid:40s} before {b['pass']}/{b['n']}  ->  after {a['pass']}/{a['n']}")

    print("\n== 全库配对（排除修掉的两题）")
    key = ("scenario", "run")
    bmap, amap = {}, {}
    for r in usable(before):
        if r.get("scenario") in FIXED:
            continue
        bmap[(r.get("scenario"), r.get("run"))] = r.get("verdict")
    for r in usable(after):
        if r.get("scenario") in FIXED:
            continue
        amap[(r.get("scenario"), r.get("run"))] = r.get("verdict")
    pairs = sorted(set(bmap) & set(amap))
    lost = sum(1 for k in pairs if bmap[k] == "PASS" and amap[k] != "PASS")
    gained = sum(1 for k in pairs if bmap[k] != "PASS" and amap[k] == "PASS")
    p = mcnemar_exact(lost, gained)
    print(f"  配对 {len(pairs)} 对（before 独有 {len(set(bmap)-set(amap))}，after 独有 {len(set(amap)-set(bmap))}）")
    print(f"  lost(before PASS -> after 非 PASS) = {lost}")
    print(f"  gained(before 非 PASS -> after PASS) = {gained}")
    print(f"  mcnemar_exact({lost},{gained}) p = {p:.4f}")

    print("\n== 逐题多数票（排除修掉的两题）")
    def majority(m):
        out = {}
        for sid, d in m.items():
            if sid in FIXED:
                continue
            out[sid] = d["pass"] * 2 > d["n"]
        return out
    mb, ma = majority(pb), majority(pa)
    common = sorted(set(mb) & set(ma))
    tl = sum(1 for s in common if mb[s] and not ma[s])
    tg = sum(1 for s in common if not mb[s] and ma[s])
    print(f"  共同 {len(common)} 题，丢 {tl} 赢 {tg}，p = {mcnemar_exact(tl, tg):.4f}")

    payload = {
        "arms": {n: rate_block(n, r) for n, r in (("before", before), ("after", after))},
        "fixed": {sid: {"before": pb.get(sid), "after": pa.get(sid)} for sid in FIXED},
        "pair": {"pairs": len(pairs), "lost": lost, "gained": gained, "p": p},
        "majority": {"common": len(common), "lost": tl, "gained": tg,
                     "p": mcnemar_exact(tl, tg)},
    }
    pathlib.Path("demo/fixpair_summary.json").write_text(json.dumps(payload, indent=2))
    print("\nwrote demo/fixpair_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
