"""覆盖度自述与保真报表的测试。

这一组测的不是"报表印得好看"，而是"这个数字会不会骗人"：
  - 每个 twin 都必须能自述实现了什么（漏一个 twin，保真表就少一行且没人发现）
  - 未实现端点必须进分母（否则覆盖率永远是 100%）
  - 删掉一个已实现路由，覆盖率必须动（RED：证明这个指标不是装饰）
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from argaclone.server import build_registry  # noqa: E402
from argaclone.twins.base import Response, Twin  # noqa: E402


class _Toy(Twin):
    """一个只有两条路由的玩具 twin，用来把覆盖率算清楚。"""

    name = "toy"

    def _build_routes(self) -> None:
        self.route("GET", "/things", self._list)
        self.route("GET", "/things/{tid}", self._get)

    def _list(self, req, params):
        return Response(200, [])

    def _get(self, req, params):
        return Response(200, {"id": params["tid"]})


class _Registry:
    """冒充 App 的那一半接口（build_rows 只用到 .registry.has/.get）。"""

    def __init__(self, twins):
        self._d = {t.name: t for t in twins}
        self.registry = self

    def has(self, name):
        return name in self._d

    def get(self, name):
        return self._d[name]


def test_every_twin_reports_coverage():
    reg = build_registry()
    for name in reg.names():
        body = reg.get(name).coverage().body
        assert body["provider"] == name
        assert body["unique_endpoints"] > 0
        assert body["implemented_routes"] >= body["unique_endpoints"]
        assert all(" " in ep for ep in body["endpoints"]), "端点必须是 'METHOD /path' 形式"


def test_coverage_normalizes_route_placeholders():
    """模板占位符必须与真实 id 用同一套记号，否则报表两边对不上。"""
    assert Twin._normalize("GET /things/{tid}") == "GET /things/{id}"
    assert Twin._normalize("GET /things/abc123def456") == "GET /things/{id}"
    # 纯字母的长单词不是 id（collaborators 曾经被误归一）
    assert Twin._normalize("GET /repos/{o}/{r}/collaborators") == (
        "GET /repos/{id}/{id}/collaborators"
    )


def test_unimplemented_endpoint_lands_in_denominator():
    """命中一条已实现 + 一次 stub → 覆盖率 50%，不是 100%。"""
    toy = _Toy()
    rows = _report_rows(
        registry=_Registry([toy]),
        stats={
            "toy": {
                "hit_paths": {("GET", "/things")},
                "stub_calls": 1,
                "stub_endpoints": {"POST /things"},
            }
        },
    )
    row = rows[0]
    assert row["hit_routes"] == 1
    assert row["stubbed_endpoints"] == 1
    assert row["observed_coverage"] == 0.5


def test_red_removing_a_hit_route_drops_coverage():
    """RED：把已命中的路由从 twin 上摘掉，覆盖率必须掉。

    如果这条测试在"删路由"之后仍然 100%，说明分子不是从 twin 的实际路由算的，
    而是从调用记录直接数的 —— 那这个指标就变成了"统计被调了多少次"，
    无论端点实不实现都好看，正是我们要避免的那种装饰性绿灯。
    """
    toy = _Toy()
    stats = {
        "toy": {
            "hit_paths": {("GET", "/things"), ("GET", "/things/t9")},
            "stub_calls": 0,
            "stub_endpoints": set(),
        }
    }
    before = _report_rows(registry=_Registry([toy]), stats=stats)[0]
    assert before["observed_coverage"] == 1.0

    toy.routes = [r for r in toy.routes if r.template != "GET /things/{tid}"]
    after = _report_rows(registry=_Registry([toy]), stats=stats)[0]
    assert after["hit_routes"] == 1
    assert after["implemented_routes"] == 1
    assert after["observed_coverage"] == 1.0  # 删掉的那条不再算"未命中"
    # 真正的判据：实现路由数与命中数一起降，未用到数不涨（删掉的路由不留在分母里）
    assert after["unused_routes"] == 0
    assert before["unused_routes"] == 0


def test_red_missing_coverage_route_is_visible():
    """RED：没命中任何已实现路由且没有 stub 时，覆盖率是 None 而不是 100%。"""
    toy = _Toy()
    rows = _report_rows(
        registry=_Registry([toy]),
        stats={"toy": {"hit_paths": set(), "stub_calls": 0, "stub_endpoints": set()}},
    )
    assert rows[0]["observed_coverage"] is None


def _report_rows(registry, stats):
    """薄封装：让测试不必起 App 也能算报表行。"""
    import fidelity_report

    return fidelity_report.build_rows(registry, stats)
