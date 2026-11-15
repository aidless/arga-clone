"""保真度报表：每个 twin 到底被实现了多少、被用到多少、有多少请求掉进 stub。

为什么要这个脚本：Arga 的 `100% fidelity` 只有 GitHub twin 有 stub-hits 接口能验证，
其余 30+ 个 twin 无法被证伪。一个不可证伪的保真度声明，本质上是一句营销口号。

这个脚本给出的是一个**可复算的下界**，并且刻意只叫 observed coverage：

    observed_coverage = 被命中过的已实现路由 / (被命中过的已实现路由 + 落进 stub 的端点)

它测的不是"跟真 SaaS 有多像"，而是"在本次跑动里，有多少被用到的能力是真实现了的"。
两个数字都必须能从落盘数据重算：
  - 分子分母来自 twin 自己报的 `/admin/coverage` 与 `twin.routes`
  - stub 侧来自 `run.stub_recent`（每次 stub 都留痕，不是我事后统计的）

⚠️ 命中判定用**路由正则匹配实际路径**，不是字符串比对。踩过一次：
`/repos/acme/service/contents/content/pricing` 里的 `acme`/`service` 不是长 id，
不会归一成占位符，于是跟模板 `PUT /repos/{owner}/{repo}/contents/{path}`
永远字符串对不上，命中数恒为 0 —— 报表会印出一张"什么都没被用到"的假表。

用法：
    python3 scripts/fidelity_report.py                 # 用全部有 gold 的题跑一遍
    python3 scripts/fidelity_report.py --json out.json # 同时落盘
    python3 scripts/fidelity_report.py --from-json demo/agent_compare_full.json
                                                       # 从已有跑分结果算（不重跑）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from argaclone.server import App  # noqa: E402

import verify_solvable as vs  # noqa: E402

SCENARIOS = ROOT / "scenarios"


def _blank() -> dict:
    return {
        "hit_paths": set(),
        "stub_calls": 0,
        "stub_endpoints": set(),
        # 其中 twin 主动声明过"不打算实现"的部分。与 stub 分开统计，
        # 否则报表会把画好的边界算成欠账（详见 Twin.declared_stubs）。
        "declared_endpoints": set(),
        "declared_calls": 0,
        # 第三态：真实 provider 里不存在的端点（agent 编的）。既不是欠账，
        # 也不进覆盖率分母 —— 它照真 API 回 404，是**正确的**行为。
        "absent_endpoints": set(),
        "absent_calls": 0,
    }


def collect_from_gold(app: App) -> tuple[dict, list[str]]:
    """跑全部 gold 解法，收集每个 twin 的命中路径与 stub。"""
    stats: dict[str, dict] = {}
    used: list[str] = []
    for sid in sorted(vs.GOLD):
        view = app.provision(
            {"twins": "stripe,slack,github", "ttl": 600, "scenario_id": sid}
        )
        run_id = view["run_id"]
        runner = app.runner_for(run_id)
        for a in vs.GOLD[sid](view):
            runner.call(a)
        app.commit_trace(run_id)
        rep = app.grade(run_id)
        if rep["verdict"] != "PASS":
            fails = ",".join(
                c["id"] for c in rep["checks"] if not c["passed"] and c["kind"] == "outcome"
            )
            print(f"  ! {sid} gold 未通过（{rep['verdict']}: {fails}），不计入", file=sys.stderr)
            continue
        used.append(sid)
        _absorb(stats, app, run_id, runner)
    return stats, used


# 每个 provider 的路径里，哪些位置是"实体名"而不是"端点类型"。
# 不归一就会把 `/repos/acme/service/contents` 和 `/repos/acme/billing/contents`
# 数成两个不同的 stub 端点，把缺口放大成假数字（实测：134 → 归约后大幅下降）。
_SLOT_PATTERNS = {
    "github": [(r"^/repos/[^/]+/[^/]+", "/repos/{owner}/{repo}"),
               (r"^/orgs/[^/]+", "/orgs/{org}"),
               (r"^/users/[^/]+", "/users/{user}")],
}


def _canonical(provider: str, path: str) -> str:
    """把实际路径归约成"端点类型"，供 stub 缺口统计用。

    输入可能已经带了方法前缀（calls_detail 的 `endpoint` 就是 `"GET /repos/..."`），
    所以先剥前缀再归约——否则拼出来是 `"GET GET /repos/..."`，
    而 `^/repos` 这种锚定正则也永远匹配不上，归约静默失效。
    """
    path = re.sub(r"^(GET|POST|PUT|PATCH|DELETE|HEAD)\s+", "", path.strip())
    path = path.split("?")[0].rstrip("/") or "/"
    for pattern, repl in _SLOT_PATTERNS.get(provider, []):
        path = re.sub(pattern, repl, path)
    return path


def collect_from_json(path: Path) -> tuple[dict, list[str]]:
    """从 agent_compare 的结果文件算。需要 call 级明细（calls_detail）。"""
    data = json.loads(path.read_text())
    stats: dict[str, dict] = {}
    used: list[str] = []
    for ep in data:
        detail = ep.get("calls_detail") or []
        if not detail:
            continue
        used.append(ep["scenario"])
        for call in detail:
            provider = call.get("provider", "")
            entry = stats.setdefault(provider, _blank())
            if call.get("absent"):
                entry["absent_calls"] += 1
                raw = call.get("endpoint") or call.get("path", "")
                entry["absent_endpoints"].add(
                    f"{call.get('method', 'GET').upper()} {_canonical(provider, raw)}"
                )
            elif call.get("stub"):
                entry["stub_calls"] += 1
                raw = call.get("endpoint") or call.get("path", "")
                key = f"{call.get('method', 'GET').upper()} {_canonical(provider, raw)}"
                entry["stub_endpoints"].add(key)
                # 旧结果文件没有 declared 字段：那时记成"欠账"而不是猜成边界，
                # 因为把边界错记成欠账只是多一行待办，反过来会掩盖真实缺口。
                if call.get("declared"):
                    entry["declared_calls"] += 1
                    entry["declared_endpoints"].add(key)
            else:
                entry["hit_paths"].add((call["method"].upper(), call["path"]))
    return stats, sorted(set(used))


def _absorb(stats: dict, app: App, run_id: str, runner) -> None:
    for c in runner.trace()["calls"]:
        stats.setdefault(c["provider"], _blank())["hit_paths"].add(
            (c["method"].upper(), c["path"])
        )
    run = app.store.get(run_id, allow_expired=True)
    for provider, hits in run.stub_hits.items():
        entry = stats.setdefault(provider, _blank())
        entry["stub_calls"] += sum(hits.values())
        entry["stub_endpoints"].update(hits)
    for provider, hits in run.declared_stub_hits.items():
        entry = stats.setdefault(provider, _blank())
        entry["declared_calls"] += sum(hits.values())
        entry["declared_endpoints"].update(hits)
    for provider, hits in run.absent_hits.items():
        entry = stats.setdefault(provider, _blank())
        entry["absent_calls"] += sum(hits.values())
        entry["absent_endpoints"].update(hits)


def build_rows(app: App | None, stats: dict) -> list[dict]:
    rows = []
    for provider in sorted(stats):
        entry = stats[provider]
        routes = []
        if app is not None and app.registry.has(provider):
            routes = app.registry.get(provider).routes
        hit = sum(
            1
            for r in routes
            if any(m == r.method and r.pattern.match(p) for m, p in entry["hit_paths"])
        )
        stubbed = len(entry["stub_endpoints"])
        denominator = hit + stubbed
        rows.append(
            {
                "provider": provider,
                "implemented_routes": len(routes),
                "hit_routes": hit,
                "stubbed_endpoints": stubbed,
                "stub_calls": entry["stub_calls"],
                # 用 .get 兜底：stats 也可能来自旧结果文件或手写夹具，
                # 缺字段是"没观测到"，不是"报表坏了"。
                "declared_endpoints": len(entry.get("declared_endpoints", ())),
                "declared_calls": entry.get("declared_calls", 0),
                # "还欠着"的那部分：stub 端点减掉声明过的边界。
                # 两列都在，读报表的人就不会把边界当工程债。
                "unplanned_endpoints": stubbed - len(entry.get("declared_endpoints", ())),
                # agent 自己编的端点：单独一列，不进分母。
                # 混进 stub 会让"还欠多少"看起来比实际大，也会让人去"补"一个
                # 真实 provider 里根本不存在的东西。
                "absent_endpoints": len(entry.get("absent_endpoints", ())),
                "absent_calls": entry.get("absent_calls", 0),
                "observed_coverage": round(hit / denominator, 3) if denominator else None,
                # 分母是"被碰到的路由/端点"，不是"真 SaaS 的全部端点"，所以这是下界
                "unused_routes": len(routes) - hit,
            }
        )
    return rows


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-json", type=Path, default=None, help="从已有跑分结果算，不重跑")
    ap.add_argument("--json", type=Path, default=None, help="报表落盘路径")
    args = ap.parse_args(argv)

    # from-json 也必须建 registry：报表要用它查「每个 provider 实现了多少条路由」。
    # 一开始这里写 app = None，结果 implemented_routes 恒 0、命中恒 0、
    # 覆盖率印成 0.0% —— 而 stub 那一侧有真数字，于是整张表看起来
    # 像是"所有东西都掉进 stub"，把真问题（缺端点）和假问题（报表坏了）混在一起。
    app = App(SCENARIOS)
    if args.from_json:
        stats, used = collect_from_json(args.from_json)
    else:
        stats, used = collect_from_gold(app)
    if not stats:
        print("没有收集到任何数据", file=sys.stderr)
        return 2

    rows = build_rows(app, stats)

    print(f"保真度报表（基于 {len(used)} 道有 gold 的题）")
    print()
    header = (
        f'{"provider":10} {"实现路由":>8} {"命中":>6} {"stub端点":>8} '
        f'{"欠账":>6} {"声明":>6} {"无效路径":>8} {"stub调用":>8} {"观测覆盖":>8} {"未用到":>7}'
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        cov = "—" if r["observed_coverage"] is None else f'{r["observed_coverage"]:.1%}'
        print(
            f'{r["provider"]:10} {r["implemented_routes"]:>8} {r["hit_routes"]:>6} '
            f'{r["stubbed_endpoints"]:>8} {r["unplanned_endpoints"]:>6} '
            f'{r["declared_endpoints"]:>6} {r["absent_endpoints"]:>8} '
            f'{r["stub_calls"]:>8} {cov:>8} '
            f'{r["unused_routes"]:>7}'
        )
    total_hit = sum(r["hit_routes"] for r in rows)
    total_stub = sum(r["stubbed_endpoints"] for r in rows)
    if total_hit + total_stub:
        print()
        print(
            f"合计观测覆盖 {total_hit}/{total_hit + total_stub} = "
            f"{total_hit / (total_hit + total_stub):.1%}"
        )
    print()
    print("口径：observed coverage = 命中过的已实现路由 / (命中过的已实现路由 + 落进 stub 的端点)。")
    print("      分母是「本次被碰到的路由/端点」，不是「真 SaaS 的全部端点」，所以它是下界；")
    print("      它也不是 fidelity —— 保真度还牵着状态语义、错误形状与时序。")
    print("      「欠账端点」= 掉进 stub 且没声明过；「声明边界」= twin 主动说了不实现。")
    print("      两列分开是为了不把已想清楚的取舍当成未完成的工程量。")
    print("      「无效路径」= 真实 provider 里不存在的端点（agent 自己编的，或 Host 用错）。")
    print("      它们照真 API 回 404，不进分母 —— 那是正确行为，不是我的工程债。")

    if args.json:
        args.json.write_text(
            json.dumps({"scenarios": used, "rows": rows}, ensure_ascii=False, indent=2)
        )
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
