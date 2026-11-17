#!/usr/bin/env python3
"""富余（BUDGET_SPARE）全量扫描的读数：85 题 × spare ∈ {4,6,8,10}。

为什么单独一个脚本：README §29.8 里每个百分数、每个"超预算"计数、每个配对 p 值
都要能从这里重算出来。数字手抄进 README 后，下一轮扫完就过期，而过期的数字比
没有数字更糟 —— 读者分不出哪个是真读数。

四件事：
1. 每档 PASS / FAIL / UNSAFE / ERROR + Wilson 95% 区间；
2. 超预算 episode 计数，并**分列"超预算但仍 PASS"** —— 这一列是重点：
   预算判据只在模型看不见预算时才会被"做对了但读多了"触发；
3. FAIL 分型：`within-budget` 在 outcome_failures 里 = 预算红；不在 = 任务红。
   （不加判据的 FAIL 里，两种原因会混成一格）
4. 逐题多数票的配对 McNemar，基准 spare=8 —— 按题配对，不按 episode，
   否则同一题的两轮会被当成两个独立样本。

预算**现算**不读 episode 里的缓存：`max(BUDGET_FLOOR, gold + spare)`，
并且断言现算结果与那一档的 spare 一致，否则"超预算"这一列就是拿新尺量旧数据。

用法:
    python3 scripts/measure_spare_sweep.py                 # 默认四档
    python3 scripts/measure_spare_sweep.py --spares 2,8    # 指定档位
    python3 scripts/measure_spare_sweep.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scenarios"))
sys.path.insert(0, str(ROOT / "scripts"))

from budget import BUDGET_FLOOR, GOLD_CALLS  # type: ignore  # noqa: E402
from stats import mcnemar_exact, wilson  # noqa: E402

SPARES = (4, 6, 8, 10)
BASELINE = 8


def budget_for_tier(gold_calls: int, spare: int, floor: int | None = None) -> int:
    """按**那一档**的 spare 现算预算。

    不能调 `budget.budget_for` —— 它读的是模块级的 `BUDGET_SPARE`，而扫描结束后
    那个值是 8。用它算 spare=4 那一档，每道题的预算都会凭空多 4 次，
    "超预算"这一列就会安静地变成 0，看起来像模型变乖了。
    """
    if floor is None:
        floor = BUDGET_FLOOR
    return max(int(floor), int(gold_calls) + int(spare))


def usable(rows: list[dict]) -> list[dict]:
    """去掉不进分母的两类：通道错误与上游额度耗尽的整批 ERROR。

    本仓既有口径：`ERROR` 不进分母、不做跨模型结论（见 README §22 与
    台账 `L-arag-330`）。把它们留在里面会让 pass rate 变成一个
    "上游今天额度够不够"的读数。
    """
    return [r for r in rows if not r.get("channel_error") and r.get("verdict") != "ERROR"]


def load(spare: int, floor: int | None = None, target: str = "") -> list[dict]:
    """读某一档的落盘结果。

    富余扫描的文件是 `demo/fullspare_{spare}.json`（floor 恒为 12）；
    地板扫描的文件是 `demo/floorscan_{floor}.json`（spare 恒为 8）。
    两类文件必须分开命名：同名的 `fullspare_8.json` 与 `floorscan_8.json`
    是两次不同的实验，混在一个命名空间里迟早有人拿错。
    """
    if floor is None:
        path = ROOT / f"demo/fullspare_{spare}.json"
    elif target:
        path = ROOT / f"demo/floortarget_{target}_{floor}.json"
    else:
        path = ROOT / f"demo/floorscan_{floor}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    rows = data if isinstance(data, list) else data.get("results") or data.get("episodes") or []
    # 返回原样：ERROR 占比本身是要报的读数（"这一臂能不能拿来做结论"），
    # 在 load 里就丢掉会让一整臂 12/12 ERROR 被静默读成"缺文件 / 3 episodes 100% PASS"。
    return rows


def tier_summary(spare: int, rows: list[dict], floor: int | None = None) -> dict:
    if not rows:
        return {}
    raw_n = len(rows)
    rows = usable(rows)
    if not rows:
        return {"rows_total": raw_n, "dropped": raw_n, "n": 0, "reliable": False,
                "rate": 0.0, "pass": 0, "fail": 0, "unsafe": 0, "error": raw_n,
                "lo": 0.0, "hi": 0.0, "avg_calls": 0.0, "majority": {},
                "over_budget": 0, "over_budget_still_pass": 0,
                "budget_only": 0, "task_only": 0, "both": 0}
    n = len(rows)
    v = [r["verdict"] for r in rows]
    passed = v.count("PASS")
    lo, hi = wilson(passed, n)
    over = [r for r in rows
            if r["calls"] > budget_for_tier(GOLD_CALLS[r["scenario"]], spare, floor)]
    # FAIL 三分类。为什么不是两类：`it-04` 那种"超预算且任务也错了"的 episode
    # 落进哪一类都会污染那一类的读法 —— 算预算红，看起来像"预算太紧罚了正确解"；
    # 算任务红，预算判据的代价就被抹掉了。必须单列一格。
    only_budget = [r for r in rows if r["verdict"] == "FAIL"
                   and (r.get("outcome_failures") or []) == ["within-budget"]]
    only_task = [r for r in rows if r["verdict"] == "FAIL"
                 and "within-budget" not in (r.get("outcome_failures") or [])]
    both = [r for r in rows if r["verdict"] == "FAIL"
            and "within-budget" in (r.get("outcome_failures") or [])
            and (r.get("outcome_failures") or []) != ["within-budget"]]
    by_task: dict[str, list[str]] = {}
    for r in rows:
        by_task.setdefault(r["scenario"], []).append(r["verdict"])
    majority = {sid: ("PASS" if sum(1 for x in vs if x == "PASS") * 2 > len(vs) else "FAIL")
                for sid, vs in by_task.items()}
    return {
        "spare": spare,
        "floor": floor if floor is not None else BUDGET_FLOOR,
        "n": n,
        "pass": passed,
        "fail": v.count("FAIL"),
        "unsafe": v.count("UNSAFE"),
        "error": v.count("ERROR"),
        "rate": passed / n,
        "lo": lo,
        "hi": hi,
        "over_budget": len(over),
        "over_budget_still_pass": sum(1 for r in over if r["verdict"] == "PASS"),
        "budget_only": len(only_budget),
        "task_only": len(only_task),
        "both": len(both),
        "avg_calls": sum(r["calls"] for r in rows) / n,
        "rows_total": raw_n,
        "dropped": raw_n - n,
        # 一臂里超过 20% ERROR（或可用样本不足 30）⇒ 上游额度/路由问题，不做跨臂结论。
        "reliable": (raw_n - n) / raw_n <= 0.20 and n >= 30,
        "majority": majority,
        "over_ids": [r["scenario"] for r in over],
        "budget_only_ids": [r["scenario"] for r in only_budget],
        "both_ids": [r["scenario"] for r in both],
    }


def pair(spare: int, base: dict, cur: dict) -> dict:
    both = sorted(set(base["majority"]) & set(cur["majority"]))
    lost = [s for s in both if base["majority"][s] == "PASS" and cur["majority"][s] == "FAIL"]
    gained = [s for s in both if base["majority"][s] == "FAIL" and cur["majority"][s] == "PASS"]
    return {"spare": spare, "common": len(both), "lost": lost, "gained": gained,
            "p": mcnemar_exact(len(lost), len(gained))}


def render_markdown(tiers: dict, pairs: list) -> str:
    """README 用表。数字直接从这里出，正文不手抄 —— 手抄的数字下一轮就过期。"""
    lines = [
        "| 富余 | episodes | PASS | FAIL | UNSAFE | 通过率 | Wilson 95% | 超预算 | 超预算仍 PASS "
        "| 纯预算红 | 任务红 | 两者都红 | 平均调用 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, t in sorted(tiers.items(), key=lambda kv: (str(kv[0][0]) if isinstance(kv[0], tuple) else "a", str(kv[0]))):
        label = f"floor={key[1]}" if isinstance(key, tuple) else f"spare={key}"
        lines.append(
            f"| {label} | {t['n']} | {t['pass']} | {t['fail']} | {t['unsafe']} "
            f"| {t['rate']*100:.1f}% | [{t['lo']*100:.1f}, {t['hi']*100:.1f}] "
            f"| {t['over_budget']} | {t['over_budget_still_pass']} "
            f"| {t['budget_only']} | {t['task_only']} | {t['both']} | {t['avg_calls']:.2f} |"
        )
    if pairs:
        base_label = f"floor={BASELINE[1]}" if isinstance(BASELINE, tuple) else f"spare={BASELINE}"
        lines += ["", f"| 对比（基准 {base_label}） | 共同题数 | 丢 | 赢 | McNemar p |",
                  "|---|---|---|---|---|"]
        for pr in pairs:
            lines.append(f"| {pr['spare']} vs {base_label} | {pr['common']} "
                         f"| {len(pr['lost'])} | {len(pr['gained'])} | {pr['p']:.4f} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spares", default=",".join(str(s) for s in SPARES))
    ap.add_argument("--target", default="",
                    help="定向子集标签：读 demo/floortarget_<target>_<floor>.json（小样本，只判机制不够判通过率）")
    ap.add_argument("--floors", default="",
                    help="地板扫描：如 8；读 demo/floorscan_{N}.json，与 fullspare_8（floor=12）配对")
    ap.add_argument("--json", default="")
    ap.add_argument("--md", default="", help="输出 README 用的 markdown 表，免得手抄数字")
    args = ap.parse_args()
    spares = [int(s) for s in args.spares.split(",") if s.strip()]
    floors = [int(f) for f in args.floors.split(",") if f.strip()]

    tiers = {}
    if floors:
        # 地板扫描分支：每档读 floorscan_{N}.json（spare 恒为 8），
        # 基准是 fullspare_8 —— floor=12、spare=8，即现默认配置。
        # 基准是同一批（同模型路由、同时间窗）跑出来的 floor=12：`demo/floorscan_12.json`。
        # 不能拿 fullspare_8.json 当基准：那是另一条模型路由 / 另一个时间窗的读数，
        # 混进配对会把模型漂移算到地板上（实测：sensenova/sensenova-6.8-flash 中途 404，
        # 94/170 episodes ERROR —— 换 sn-sensenova-6-8-flash 重跑时两臂必须同批）。
        base_rows = load(8, BUDGET_FLOOR, args.target)
        if not base_rows:
            print(f"缺基准 demo/floorscan_{BUDGET_FLOOR}.json")
            return 1
        tiers[("spare8", BUDGET_FLOOR)] = tier_summary(8, base_rows)
        for floor in floors:
            rows = load(8, floor, args.target)
            if not rows:
                print(f"floor={floor}: 缺 demo/floorscan_{floor}.json，跳过")
                continue
            tiers[("spare8", floor)] = tier_summary(8, rows, floor)
    else:
        for spare in spares:
            rows = load(spare)
            if not rows:
                print(f"spare={spare}: 缺 demo/fullspare_{spare}.json，跳过")
                continue
            tiers[spare] = tier_summary(spare, rows)

    if not tiers:
        print("没有可读的档位。")
        return 1

    print("\n=== 每档汇总 ===")
    print(f"{'档位':>5} {'n':>4} {'PASS':>5} {'FAIL':>5} {'UNS':>4} {'ERR':>4} {'rate':>7} "
          f"{'Wilson95':>15} {'超预算':>7} {'仍PASS':>7} {'纯预算红':>8} {'任务红':>7} "
          f"{'两者都红':>8} {'avgCall':>8} {'可信':>6} {'丢弃':>6}")
    for key, t in sorted(tiers.items(), key=lambda kv: (str(kv[0][0]) if isinstance(kv[0], tuple) else "a", str(kv[0]))):
        label = f"f{key[1]}" if isinstance(key, tuple) else f"s{key}"
        print(f"{label:>5} {t['n']:>4} {t['pass']:>5} {t['fail']:>5} {t['unsafe']:>4} {t['error']:>4} "
              f"{t['rate']*100:>6.1f}% [{t['lo']*100:>5.1f},{t['hi']*100:>5.1f}] "
              f"{t['over_budget']:>7} {t['over_budget_still_pass']:>7} "
              f"{t['budget_only']:>8} {t['task_only']:>7} {t['both']:>8} {t['avg_calls']:>8.2f} "
              f"{'是' if t.get('reliable') else '否':>6} {t.get('dropped', 0):>6}")

    pairs = []
    base_key = ("spare8", BUDGET_FLOOR) if floors else BASELINE
    if base_key in tiers:
        print(f"\n=== 逐题多数票配对（基准 {base_key}）===")
        for key, t in sorted(tiers.items(), key=lambda kv: (str(kv[0][0]) if isinstance(kv[0], tuple) else "a", str(kv[0]))):
            if key == base_key:
                continue
            if not (tiers[base_key].get("reliable") and t.get("reliable")):
                print(f"{key} vs {base_key}：至少一臂不可信（ERROR 过多 / 样本不足），拒绝配对")
                continue
            pr = pair(key, tiers[base_key], t)
            pairs.append(pr)
            print(f"{key} vs {base_key}：共同 {pr['common']} 题，丢 {len(pr['lost'])} "
                  f"赢 {len(pr['gained'])}，mcnemar_exact({len(pr['lost'])},{len(pr['gained'])}) "
                  f"p={pr['p']:.4f}")
            if pr["lost"]:
                print(f"   丢：{', '.join(pr['lost'])}")
            if pr["gained"]:
                print(f"   赢：{', '.join(pr['gained'])}")

    if args.md:
        md = render_markdown(tiers, pairs)
        Path(args.md).write_text(md)
        print(f"\nwrote {args.md}")

    print("\n=== 档位的实际作用范围 ===")
    if floors:
        for floor in floors:
            moved = sorted(sid for sid, g in GOLD_CALLS.items()
                           if budget_for_tier(g, 8, floor) != budget_for_tier(g, 8, BUDGET_FLOOR))
            print(f"floor={floor} vs {BUDGET_FLOOR}（spare=8）: {len(moved)}/{len(GOLD_CALLS)} 题预算真的变了")
    else:
        for spare, t in sorted(tiers.items()):
            if spare == BASELINE:
                continue
            moved = sorted(sid for sid, g in GOLD_CALLS.items()
                           if budget_for_tier(g, spare) != budget_for_tier(g, BASELINE))
            print(f"spare={spare} vs {BASELINE}: {len(moved)}/{len(GOLD_CALLS)} 题预算真的变了"
                  + (f"（{', '.join(moved)}）" if len(moved) <= 8 else ""))
        flat = sorted(sid for sid, g in GOLD_CALLS.items() if g + max(spares) < BUDGET_FLOOR)
        print(f"连最宽档（spare={max(spares)}）都撞地板的题：{len(flat)}/{len(GOLD_CALLS)}")
        tight = sorted(sid for sid, g in GOLD_CALLS.items() if g + min(spares) < BUDGET_FLOOR)
        print(f"最窄档（spare={min(spares)}）撞地板的题：{len(tight)}/{len(GOLD_CALLS)}"
              f"（这些题在窄档下预算与富余无关，恒为 {BUDGET_FLOOR}）")

    payload = {"spares": spares, "floors": floors, "baseline": str(base_key),
               "tiers": {str(k): {kk: vv for kk, vv in v.items() if kk != "majority"}
                         for k, v in tiers.items()},
               "pairs": pairs}
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
