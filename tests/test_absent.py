"""不存在的端点：twin 必须和真 API 一样说"没有"，而不是给假成功。

为什么单开一桶：
stub 的语义是"这个端点真实存在，我们还没仿真"。可 agent 会**发明**端点 ——
`POST /repos/acme/svc/issues/56`（真 GitHub 更新 issue 用 PATCH，单条 issue 的 POST 是 404）、
或者拿 slack 的路径去打 github 的 Host。对这些请求回 200 + `_twin_stub` 是有害的：
agent 看到"调用成功"，就以为状态改好了，然后基于一个没发生的事实继续往下做。
判据在那边读真实状态 → FAIL，而失分的原因被记成"模型能力不足"。

所以这里要的是**第三态**：不是 stub（我们欠着），不是 404 空手（真 API 也不会给假数据），
而是"照真 API 的形状说没有"，并在报表里单列，不计入 observed coverage 的分母 ——
它不是我的工程债。
"""

from __future__ import annotations

import pytest

from argaclone.server import App

GH = "ghp_absent123456"


def _app(tmp_path):
    app = App(tmp_path)
    run_id = app.provision({"twins": "github", "ttl": 600})["run_id"]
    run = app.store.get(run_id)
    st = run.state["github"]
    st["repos"]["acme/svc"] = {"full_name": "acme/svc", "name": "svc", "private": False,
                               "default_branch": "main", "stargazers_count": 1}
    st["branches"]["acme/svc"] = {"main": "a" * 40}
    st["issues"]["acme/svc"] = {"56": {"number": 56, "title": "x", "state": "open",
                                       "labels": [], "user": {"login": "d1"}}}
    st["pulls"]["acme/svc"] = {}
    st["commits"]["acme/svc"] = []
    return app, run_id


def _call(app, run_id, method, path):
    return app.gateway.handle(
        method, f"github.{run_id}.twins.local", path,
        {"Authorization": f"Bearer {GH}", "Content-Type": "application/json"},
        b"{}" if method in ("POST", "PATCH", "PUT") else b"",
    )


def test_post_on_a_single_issue_is_absent_not_stub(tmp_path):
    """真 GitHub 这条路径不存在，twin 不许给它"改成功"的假象。"""
    app, run_id = _app(tmp_path)
    resp = _call(app, run_id, "POST", "/repos/acme/svc/issues/56")
    assert resp.status == 404, resp.body
    assert resp.headers.get("X-Twin-Absent") == "true"
    assert resp.headers.get("X-Twin-Stub") is None, "不存在的端点不该记成'还没实现'"
    assert resp.body["_twin_absent"] is True
    assert resp.body["_twin_reason"], "要写清为什么它是 404，否则读日志的人只能猜"


def test_absent_does_not_swallow_the_real_route(tmp_path):
    """缺席判定必须在路由之后。放前面会把 GET 单条 issue 一起吃掉。"""
    app, run_id = _app(tmp_path)
    resp = _call(app, run_id, "GET", "/repos/acme/svc/issues/56")
    assert resp.status == 200, resp.body
    assert resp.body["number"] == 56


def test_patch_on_a_single_issue_still_works(tmp_path):
    app, run_id = _app(tmp_path)
    resp = _call(app, run_id, "PATCH", "/repos/acme/svc/issues/56")
    assert resp.status == 200, resp.body
    assert resp.body["number"] == 56


def test_another_providers_path_on_github_host_is_absent(tmp_path):
    """拿 slack 的路径打 github 的 Host 是多 provider 任务里的常见错误。

    真 GitHub 对这种路径回 404；twin 给 stub 200 会让 agent 以为"那条命令生效了"。
    """
    app, run_id = _app(tmp_path)
    resp = _call(app, run_id, "POST", "/api/conversations.list")
    assert resp.status == 404, resp.body
    assert resp.headers.get("X-Twin-Absent") == "true"


def test_api_v3_prefix_is_not_absent(tmp_path):
    """`/api/v3` 是 GitHub Enterprise 的正常入口，不能被 /api/* 的缺席规则吞掉。"""
    app, run_id = _app(tmp_path)
    resp = _call(app, run_id, "GET", "/api/v3/repos/acme/svc/issues/56")
    assert resp.status == 200, resp.body
    assert resp.body["number"] == 56


def test_absent_is_counted_separately_from_stub_debt(tmp_path):
    """报表要能回答"这些假请求里有多少是 agent 自己编的"。"""
    app, run_id = _app(tmp_path)
    _call(app, run_id, "POST", "/repos/acme/svc/issues/56")
    _call(app, run_id, "GET", "/repos/acme/svc/actions/runners")   # 真的没实现 → stub

    report = app.stub_hits(run_id, "github")
    assert report["total_hits"] == 1, report
    assert report["absent_hits"] == 1, report
    # 按**规则模式**归并：agent 每次编的 owner/repo 都不同，按具体路径记会得到
    # 一长串各 1 次的条目，看不出"它反复在试同一类错"。
    assert [e["endpoint"] for e in report["absent_endpoints"]] == [
        "POST /repos/*/*/issues/*"
    ], report["absent_endpoints"]
    assert not any(s["endpoint"].startswith("POST /repos/acme/svc/issues")
                   for s in report["summary"])


def test_coverage_lists_absent_paths_as_declared(tmp_path):
    """缺席规则也是"我们想清楚不仿真"的边界，要能被问到。"""
    app, run_id = _app(tmp_path)
    resp = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                              "/admin/coverage", {"Authorization": f"Bearer {GH}"}, b"")
    assert resp.status == 200
    paths = {d["prefix"] for d in resp.body["absent_paths"]}
    assert "/repos/*/*/issues/*" in paths and "/api/*" in paths, paths


def test_red_without_the_absent_table_the_request_falls_back_to_stub(tmp_path):
    """RED：证明"不再给假成功"是这张表干的，不是别的分支顺手拦的。"""
    from argaclone.twins.github_twin import GitHubTwin

    class NoAbsent(GitHubTwin):
        absent_paths = ()

    app = App(tmp_path)
    run_id = app.provision({"twins": "github", "ttl": 600})["run_id"]
    app.registry.register(NoAbsent())
    st = app.store.get(run_id).state["github"]
    st["repos"]["acme/svc"] = {"full_name": "acme/svc", "name": "svc", "private": False,
                               "default_branch": "main", "stargazers_count": 1}
    st["branches"]["acme/svc"] = {"main": "a" * 40}
    st["issues"]["acme/svc"] = {}
    st["pulls"]["acme/svc"] = {}
    st["commits"]["acme/svc"] = []

    resp = _call(app, run_id, "POST", "/repos/acme/svc/issues/56")
    assert resp.status == 200, "没有缺席表时必须回落到 stub —— 否则这条 RED 不成立"
    assert resp.headers.get("X-Twin-Stub") == "true"


def test_cross_provider_paths_are_absent_not_stub(tmp_path):
    """跨 provider 的路径必须 404，不能给 stub 200。

    120 episodes 里剩下的 stub 命中大半是这一类：agent 把 slack 的
    `/api/conversations.list` 打在 stripe 的 Host 上，或把 stripe 的
    `/v1/customers/search` 打在 slack 上。按"端点真实存在但没仿真"记的话，
    agent 会拿到 200 + 空数据，以为调用成功了。
    """
    app = App(tmp_path)
    run = app.provision({"twins": "stripe,slack,github", "ttl_seconds": 60})

    def call(provider, method, path):
        return app.gateway.handle(
            method, f"{provider}.{run['run_id']}.twins.local", path,
            {"authorization": f"Bearer {run['credentials'][provider]}"}, b"",
        )

    cross = [
        ("stripe", "POST", "/api/conversations.list"),
        ("stripe", "POST", "/api/conversations.history"),
        ("slack", "GET", "/api/v1/customers/search"),
        ("slack", "GET", "/api/repos/acme/service/branches"),
        ("slack", "GET", "/api/users/mlin"),
    ]
    for provider, method, path in cross:
        resp = call(provider, method, path)
        assert resp.status == 404, (provider, path, resp.status)
        assert resp.headers.get("X-Twin-Absent") == "true", (provider, path)
        assert "X-Twin-Stub" not in resp.headers, (provider, path)

    # 真端点不能被这条规则连带打掉
    assert call("slack", "POST", "/api/conversations.list").status == 200
    assert call("stripe", "GET", "/v1/customers").status == 200
