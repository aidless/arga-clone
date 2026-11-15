"""声明式 stub：把"还欠着的活"和"画好的边界"分开。

为什么这条要单独测：两种命中在响应体里长得几乎一样（都是 `_twin_stub: true` + 假数据），
只差一个 `_twin_declared` 字段与报表里的一列。如果 `_declared_stub_reason` 匹配失败，
一切照旧 —— 报表悄悄把边界算成欠账，而这个错永远不会有异常。
"""

from __future__ import annotations

import pytest

from argaclone.gateway import Gateway
from argaclone.server import App, build_registry
from argaclone.store import Store
from argaclone.twins.github_twin import GitHubTwin

SCENARIOS = "scenarios"


def _app(tmp_path):
    return App(tmp_path)


def _call(app, run_id, provider, method, path, token):
    return app.gateway.handle(
        method,
        f"{provider}.{run_id}.twins.local",
        path,
        {"authorization": f"Bearer {token}"},
        b"",
    )


@pytest.fixture()
def rig(tmp_path):
    app = _app(tmp_path)
    view = app.provision({"twins": "github,stripe", "ttl_seconds": 300})
    run_id = view["run_id"]
    creds = view["credentials"]
    return app, run_id, creds


def test_declared_endpoint_says_so(rig):
    """声明的端点命中时，响应体必须自己说明"这是决定不做"。"""
    app, run_id, creds = rig
    resp = _call(app, run_id, "github", "GET", "/repos/acme/svc/actions/secrets", creds["github"])

    assert resp.status == 200
    assert resp.headers["X-Twin-Stub"] == "true"
    assert resp.headers.get("X-Twin-Declared") == "true"
    assert resp.body["_twin_stub"] is True
    assert resp.body["_twin_declared"] is True
    # 理由必须非空且真的是理由，不是占位符
    assert len(resp.body["_twin_reason"]) > 10


def test_undeclared_endpoint_is_not_marked_declared(rig):
    """没声明过的端点仍然是 stub，但**不能**被标成"决定不做"。"""
    app, run_id, creds = rig
    resp = _call(app, run_id, "stripe", "GET", "/v1/tax_rates", creds["stripe"])

    assert resp.headers["X-Twin-Stub"] == "true"
    assert "X-Twin-Declared" not in resp.headers
    assert "_twin_declared" not in resp.body


def test_report_splits_declared_from_unplanned(rig):
    """报表必须能拆成两列：这是这条能力的全部意义所在。"""
    app, run_id, creds = rig
    _call(app, run_id, "github", "GET", "/repos/acme/svc/actions/secrets", creds["github"])
    _call(app, run_id, "stripe", "GET", "/v1/tax_rates", creds["stripe"])
    _call(app, run_id, "stripe", "GET", "/v1/tax_rates", creds["stripe"])

    report = app.stub_hits(run_id)
    assert report["total_hits"] == 3
    assert report["declared_hits"] == 1
    assert report["unplanned_hits"] == 2
    # 端点模板里 owner/repo 是纯字母，不会被归一成 {id}（归一化只认 id 形状），
    # 所以这里逐字比对；跨仓库的聚合交给 fidelity_report 的 provider 专属归约。
    assert report["declared_endpoints"] == ["GET /repos/acme/svc/actions/secrets"]
    # recent 里每条也要带标记，否则"哪一次撞的"无法回答
    assert [r["planned"] for r in report["recent"]] == [False, False, True]


def test_coverage_lists_declarations(rig):
    """/admin/coverage 要自述声明，报表不能靠读源码猜。"""
    app, run_id, creds = rig
    resp = _call(app, run_id, "github", "GET", "/admin/coverage", creds["github"])
    declared = resp.body["declared_stubs"]

    assert declared, "github 声明过边界，coverage 里必须能看到"
    assert all({"method", "prefix", "reason"} <= set(d) for d in declared)
    # 声明按前缀写（`/repos/`、`/orgs/`），因为声明是给人读的边界，不是端点清单
    assert any(d["prefix"].startswith("/repos/*/*/actions") for d in declared)


def test_red_dropping_the_declaration_moves_hits_into_the_debt_column(rig):
    """RED：证明分类真的由声明决定，而不是被路径名猜出来的。

    把同一个 twin 的声明清空，同一个请求就必须从 declared 掉进 unplanned。
    如果不掉，说明报表根本没在用声明 —— 那这条能力就是装饰。
    """
    app, run_id, creds = rig
    path = "/repos/acme/svc/actions/secrets"

    _call(app, run_id, "github", "GET", path, creds["github"])
    assert app.stub_hits(run_id)["declared_hits"] == 1

    app.registry.get("github").declared_stubs = ()
    _call(app, run_id, "github", "GET", path, creds["github"])
    report = app.stub_hits(run_id)
    assert report["declared_hits"] == 1
    assert report["unplanned_hits"] == 1


def test_reset_keeps_stub_counts_but_clear_clears_both(tmp_path):
    """reset 不该抹掉观测（那是运行历史），clear 才抹。"""
    store = Store()
    run = store.create(["github"], 300)
    store.record_stub(run.run_id, "github", "GET /x", planned=True)
    store.record_stub(run.run_id, "github", "GET /y", planned=False)

    store.reset(run.run_id)
    report = store.stub_report(run.run_id)
    assert report["declared_hits"] == 1 and report["unplanned_hits"] == 1

    store.clear_stub_hits(run.run_id)
    report = store.stub_report(run.run_id)
    assert report["total_hits"] == 0 and report["declared_endpoints"] == []


def test_declared_matching_is_method_specific(tmp_path):
    """同一路径的 GET 声明不该把 PUT 也标成"决定不做"。

    只声明 GET、然后打 PUT：如果匹配逻辑漏了方法比较，PUT 也会带
    `X-Twin-Declared` —— 于是一条真正欠着的写端点被洗成"边界"。
    """
    class GetOnlyTwin(GitHubTwin):
        declared_stubs = (("GET", "/repos/*/*/actions/secrets", "只有读被声明"),)

    store = Store()
    run = store.create(["github"], 300)
    registry = build_registry()
    registry.register(GetOnlyTwin())
    gateway = Gateway(store, registry)

    def call(method):
        return gateway.handle(
            method,
            f"github.{run.run_id}.twins.local",
            "/repos/a/b/actions/secrets",
            {"authorization": "Bearer ghp_x"},
            b"" if method == "GET" else b"{}",
        )

    assert call("GET").headers.get("X-Twin-Declared") == "true"
    assert "X-Twin-Declared" not in call("PUT").headers

    report = store.stub_report(run.run_id)
    assert report["declared_hits"] == 1
    assert report["unplanned_hits"] == 1


def test_declaration_is_segment_scoped_not_prefix(rig):
    """声明必须精确到段，不能按前缀吞掉整个资源树。

    这条是 verify.sh 第 9 层当场抓出来的真缺陷：最初用前缀 `/repos/` 声明
    "secrets 不做"，结果 `/repos/o/r/hooks` 也被标成"决定不做"——
    一条真欠着的端点被洗成了边界。报表上看不出来，只有拿一个兄弟路径去撞才发现。
    """
    app, run_id, creds = rig

    declared = _call(app, run_id, "github", "GET", "/repos/acme/svc/actions/secrets", creds["github"])
    sibling = _call(app, run_id, "github", "GET", "/repos/acme/svc/hooks", creds["github"])

    assert declared.headers.get("X-Twin-Declared") == "true"
    assert "X-Twin-Declared" not in sibling.headers, "兄弟路径不能被声明吞掉"

    report = app.stub_hits(run_id)
    assert (report["declared_hits"], report["unplanned_hits"]) == (1, 1)


def test_segment_wildcard_matches_exactly_one_segment(rig):
    """`*` 只替一段：多一层或少一层都不该命中。"""
    app, run_id, creds = rig
    deep = _call(
        app, run_id, "github", "GET", "/repos/acme/svc/actions/secrets/extra", creds["github"]
    )
    assert "X-Twin-Declared" not in deep.headers
