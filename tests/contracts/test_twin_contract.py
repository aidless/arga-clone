"""跨 twin 的契约测试：每加一个 provider 都必须自动满足这些不变量。

为什么单独一层：手写 33 个 twin 时，最容易缺失的不是"业务逻辑"，是**边界行为**——
鉴权忘了查、未实现端点回了 404、stub 标记漏了一个字段。这些缺口在平时的
happy path 测试里完全看不见，但会让 agent 在真跑时一路错下去。

这一层用参数化覆盖全部已注册 twin，所以**新增 twin 自动被纳入**，
不需要有人记得"还要给它写测试"。这是工厂能省下的第二笔钱。
"""

from __future__ import annotations

import json

import pytest

from argaclone.runner import is_success
from argaclone.server import App, build_registry

# 每个 provider 的"合法凭据形状"。故意只写能用/不能用两条，不写具体业务。
TOKENS = {
    "stripe": "sk_test_contract",
    "slack": "xoxb-contract-token",
    "github": "ghp_contract1234567890",
    "jira": "jira-contract-token",
    "linear": "lin_api_contract",
    "notion": "ntn_contract-token",
    "gmail": "ya29.contract-token",
    "drive": "ya29.contract-token",
    "calendar": "ya29.contract-token",
    "hubspot": "pat-hi-contract-token",
    "salesforce": "00Dtw1n000000AAA!AQ4AQcontract",
}

# 一个"肯定没实现"的路径。挑得刻意无关，避免撞上将来新增的实现。
UNIMPLEMENTED = {
    "stripe": ("GET", "/v1/terminal/readers"),
    "slack": ("POST", "/api/admin.analytics.getFile"),
    "github": ("GET", "/repos/acme/api/actions/secrets"),
    "jira": ("GET", "/rest/api/3/auditing/record"),
    "linear": ("POST", "/graphql"),  # GraphQL 单入口，用一条不认识的根字段
    "notion": ("GET", "/v1/auditLogs"),
    "gmail": ("GET", "/gmail/v1/users/me/history"),
    "drive": ("GET", "/drive/v3/drives"),
    "calendar": ("GET", "/calendar/v3/users/me/settings"),
    "hubspot": ("GET", "/crm/v3/associations/contacts/companies"),
    "salesforce": ("GET", "/services/data/v59.0/sobjects/Account/describe/compactLayouts"),
}


@pytest.fixture
def app(tmp_path):
    # function scope：tmp_path 是 function 级 fixture，module 级会 ScopeMismatch。
    # 每个用例一个干净 App 也更安全 —— twin 实例无状态，但 scenario 目录会被写。
    return App(tmp_path)


def _provision(app, provider):
    view = app.provision({"twins": provider, "ttl": 300})
    return view, view["run_id"], view["credentials"][provider]


def _call(app, provider, run_id, method, path, token, body=None, extra_headers=None):
    """token=None 表示**不发** Authorization 头；token="" 表示发一个空的 `Bearer `。

    两者都要能表达：前者是"没登录"，后者是"登录了但令牌为空"。
    只测前者会漏掉"只查前缀不查值"这类缺陷（jira/linear/notion 原来就中招）。
    """
    headers = {
        "content-type": "application/json",
        "notion-version": "2022-06-28",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    headers.update(extra_headers or {})
    host = f"{provider}.{run_id}.twins.local"
    return app.gateway.handle(
        method, host, path, headers, json.dumps(body).encode() if body is not None else b""
    )


ALL = build_registry().names()


def test_contract_table_covers_all_twins():
    """新增 twin 必须在契约表里登记凭据与一个未实现路径。

    这条断言的存在意义：工厂让"加一个 twin"变便宜了，便宜之后最容易发生的
    是漏登记 —— 于是新 twin 静默地不受契约约束。宁可在 CI 里红一次。
    """
    assert set(ALL) == set(TOKENS), f"契约表缺 provider: {set(ALL) - set(TOKENS)}"
    assert set(ALL) == set(UNIMPLEMENTED), f"缺少未实现路径样本: {set(ALL) - set(UNIMPLEMENTED)}"


@pytest.mark.parametrize("provider", ALL)
def test_contract_missing_token_is_not_success(app, provider):
    """没带凭据的请求，任何 twin 都不能被当成成功。

    跨 provider 用 `is_success` 而不是状态码来判：Slack 的所有失败都是
    HTTP 200 + `ok:false`，只看状态码会把每次失败记成成功。

    注意这里打的是**未实现端点**：早期实现先走 stub 后鉴权，于是没令牌也能
    拿到 200 的 stub 数据 —— 未实现路径成了鉴权后门。契约守的就是这条。
    """
    _, run_id, _ = _provision(app, provider)
    method, path = UNIMPLEMENTED[provider]
    resp = _call(app, provider, run_id, method, path, None, body={} if method == "POST" else None)
    payload = resp.body if isinstance(resp.body, dict) else {}
    if provider == "linear":
        # GraphQL 的鉴权失败在 errors 里，不在 data 里
        assert "errors" in payload
        assert payload["errors"][0]["extensions"]["type"] == "authentication error"
        return
    assert not is_success(provider, resp.status, payload)


@pytest.mark.parametrize("provider", ALL)
def test_contract_empty_token_is_not_success(app, provider):
    """`Authorization: Bearer `（空值）必须被拒。

    jira/linear/notion 原来只判"头是不是以 bearer 开头"，空令牌直接通过。
    这是最容易漏的一类鉴权缺陷：所有正常测试都带着真令牌，没人会试空串。
    """
    _, run_id, _ = _provision(app, provider)
    method, path = UNIMPLEMENTED[provider]
    resp = _call(app, provider, run_id, method, path, "", body={} if method == "POST" else None)
    payload = resp.body if isinstance(resp.body, dict) else {}
    if provider == "linear":
        assert "errors" in payload
        assert payload["errors"][0]["extensions"]["type"] == "authentication error"
        return
    assert not is_success(provider, resp.status, payload)


# 已知局限（写进 README 而不写成断言）：twin 不维护 token 表，
# 所以"形状合法但值是假的"令牌（`Bearer definitely-not-valid`）会被放行。
# stripe/slack/github 只是碰巧因为校验前缀而挡住了一部分。
# 真 SaaS 会查库，twin 查不了 —— 这不是可修的缺陷，是保真成本的边界。


@pytest.mark.parametrize("provider", ALL)
def test_contract_unimplemented_endpoint_is_marked(app, provider):
    """未实现端点必须显眼地假：header + body 双标记，且数组被包成对象。"""
    _, run_id, token = _provision(app, provider)
    method, path = UNIMPLEMENTED[provider]
    body = {"query": "{ somethingWeDoNotSupport { id } }"} if provider == "linear" else {}
    resp = _call(app, provider, run_id, method, path, token, body=body if method == "POST" else None)
    if provider == "linear":
        # Linear 的 dispatch 会先解析 GraphQL 根字段，未认识的根字段回 errors 而不是 stub。
        # 这是 GraphQL 的形状决定的：它没有"路径"可以缺，只有"字段"可以缺。
        assert "errors" in (resp.body or {})
        return
    assert resp.headers.get("X-Twin-Stub") == "true", f"{provider} 未标记 stub"
    assert resp.body.get("_twin_stub") is True
    assert "_twin_warning" in resp.body
    assert isinstance(resp.body.get("data"), list), "数组必须被包成对象里的 data 字段"


@pytest.mark.parametrize("provider", ALL)
def test_contract_stub_hits_are_counted_and_clearable(app, provider):
    """每次 stub 都要留痕，且能被清空。没有这条，"保真度"就无法被量化。"""
    _, run_id, token = _provision(app, provider)
    method, path = UNIMPLEMENTED[provider]
    body = {"query": "{ viewer { id } }"} if provider == "linear" else {}
    before = app.stub_hits(run_id, provider)["total_hits"]
    _call(app, provider, run_id, method, path, token, body=body if method == "POST" else None)
    after = app.stub_hits(run_id, provider)["total_hits"]
    # Linear 走 GraphQL 分支不产生 stub，这是它的形状，不是缺陷
    assert after == before if provider == "linear" else after > before
    app.store.clear_stub_hits(run_id)
    assert app.stub_hits(run_id, provider)["total_hits"] == 0


@pytest.mark.parametrize("provider", ALL)
def test_contract_reports_coverage(app, provider):
    """每个 twin 都必须能自述实现了什么。保真报表的分母靠它。"""
    _, run_id, token = _provision(app, provider)
    resp = _call(app, provider, run_id, "GET", "/admin/coverage", token)
    assert resp.status == 200
    assert resp.body["provider"] == provider
    assert resp.body["unique_endpoints"] > 0
    assert all(" " in ep for ep in resp.body["endpoints"])


@pytest.mark.parametrize("provider", ALL)
def test_contract_twin_level_stub_report(app, provider):
    """每个 twin 自己也要能报 stub 命中，不能只有 run 级 admin 能报。

    这条断言在把 `/admin/stub-hits` 提到基类之前对 gmail/drive/calendar/hubspot/
    salesforce 是红的：五个 twin 没有这条路由。而 run 级接口能报，所以没人发现——
    双通道里总有一条没人测，迟早漂移成两种语义。
    """
    _, run_id, token = _provision(app, provider)
    method, path = UNIMPLEMENTED[provider]
    body = {"query": "{ viewer { id } }"} if provider == "linear" else {}
    _call(app, provider, run_id, method, path, token, body=body if method == "POST" else None)

    resp = _call(app, provider, run_id, "GET", "/admin/stub-hits", token)
    assert resp.status == 200, resp.body
    assert resp.body["provider" if "provider" in resp.body else "total_hits"] is not None
    assert "total_hits" in resp.body
    # 声明拆分字段也要在：报表靠它把"欠账"和"边界"分开
    assert "declared_hits" in resp.body and "unplanned_hits" in resp.body

    cleared = _call(app, provider, run_id, "POST", "/admin/stub-hits/clear", token)
    assert cleared.status == 200
    assert app.stub_hits(run_id, provider)["total_hits"] == 0


def test_red_twin_level_route_comes_from_the_base_branch(app):
    """RED：证明这条保证来自基类 `dispatch` 的那一支，不是各 twin 自己实现的。

    把 dispatch 换成"只走 handle_admin"（等于回到把分支提到基类之前的状态），
    同一个请求必须 404。如果它仍然 200，说明还有别的地方在兜底 ——
    那基类这一支就不是承重的，重构它会静默降级。
    """
    from argaclone.twins.base import Request
    from argaclone.twins.gmail_twin import GmailTwin

    class PreHoistGmail(GmailTwin):
        def dispatch(self, req):
            return self.handle_admin(req)

    twin = PreHoistGmail()
    app.registry.register(twin)
    _, run_id, _ = _provision(app, "gmail")
    run = app.store.get(run_id)
    req = Request(
        method="GET", path="/admin/stub-hits", query={}, headers={}, body=b"",
        run=run, run_id=run_id, store=app.store,
    )
    assert twin.dispatch(req).status == 404
