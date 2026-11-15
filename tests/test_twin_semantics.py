"""twin 语义保真测试。

这里的每个用例都对应一条"抄错就废"的细节，注释里写清为什么。
"""

from __future__ import annotations

import json

import pytest

from argaclone.runner import is_success
from argaclone.twins.base import Twin

STRIPE_KEY = "sk_test_arga"
SLACK_TOKEN = "xoxb-twin-token"


def call(app, run_id, provider, method, path, body=None, headers=None):
    payload = json.dumps(body).encode() if body is not None else b""
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    hdrs.setdefault("content-type", "application/json")
    return app.gateway.handle(method, f"{provider}.{run_id}.twins.local", path, hdrs, payload)


def stripe_auth():
    return {"Authorization": f"Bearer {STRIPE_KEY}"}


def slack_auth():
    return {"Authorization": f"Bearer {SLACK_TOKEN}"}


# ---------------- stub 语义 ----------------


def test_stub_response_is_marked_and_schema_hostile(app, run):
    """未实现端点既不能静默成功，也不能"看起来正常"。

    两个标记（header + body）都必须在，而且数组响应被包成对象：
    故意的 schema 破坏，让"不读警告、硬按 schema 解析"的客户端当场报错，
    而不是把空数组当成"世界本来就没有数据"。
    """
    resp = call(app, run["run_id"], "stripe", "GET", "/v1/tax_rates", headers=stripe_auth())

    assert resp.status == 200
    assert resp.headers.get("X-Twin-Stub") == "true"
    assert resp.body["_twin_stub"] is True
    assert "stubbed" in resp.body["_twin_warning"]
    # 关键：不是裸 list
    assert isinstance(resp.body, dict)
    assert resp.body["data"] == []
    assert not isinstance(resp.body, list)


def assert_stub_is_obvious(resp) -> None:
    """把"必须显眼"固化成断言，供正式用例与 RED 反证共用。"""
    assert resp.headers.get("X-Twin-Stub") == "true", "stub 没有 header 标记"
    assert resp.body.get("_twin_stub") is True, "stub 没有 body 标记"
    assert isinstance(resp.body, dict) and "data" in resp.body, "stub 没有破坏 schema"


def test_stub_hits_are_counted(app, run):
    # 两个端点：一个打两次、一个打一次 —— 断言的是"计数按端点分组"，
    # 所以要真的用两条不同的未实现路径，不能拿同一路径重复三次再改期望值。
    call(app, run["run_id"], "stripe", "GET", "/v1/tax_rates", headers=stripe_auth())
    call(app, run["run_id"], "stripe", "GET", "/v1/tax_rates", headers=stripe_auth())
    call(app, run["run_id"], "stripe", "GET", "/v1/ephemeral_keys", headers=stripe_auth())

    report = app.stub_hits(run["run_id"])
    assert report["total_hits"] == 3
    assert report["unique_endpoints"] == 2
    by_endpoint = {row["endpoint"]: row["hits"] for row in report["summary"]}
    assert by_endpoint["GET /v1/tax_rates"] == 2


def test_red_stub_marker_is_load_bearing(app, run, monkeypatch):
    """RED 反证：把 stub 改成"返回合法裸数组"，检测断言必须红。

    这条测试证明 test_stub_response_is_marked_and_schema_hostile 不是装饰——
    如果哪天有人把 stub 换成 schema-valid 的空数组（看起来更"友好"），
    它会立刻失败。
    """

    def lying_stub(self, req, endpoint=None):
        from argaclone.twins.base import Response

        return Response(200, [], {})

    monkeypatch.setattr(Twin, "stub", lying_stub)
    resp = call(app, run["run_id"], "stripe", "GET", "/v1/tax_rates", headers=stripe_auth())
    with pytest.raises(AssertionError):
        assert_stub_is_obvious(resp)


# ---------------- Stripe 错误形状 ----------------


def test_missing_resource_is_400_not_404(app, run):
    """真 Stripe 对不存在的对象回 400 + resource_missing，不是 404。

    抄错这一点，所有"删了再删"的幂等逻辑都会走错分支。
    """
    resp = call(app, run["run_id"], "stripe", "GET", "/v1/customers/cus_NOPE", headers=stripe_auth())
    assert resp.status == 400
    assert resp.body["error"]["code"] == "resource_missing"
    # 真 Stripe 对 retrieval 端点报的 param 是 "id"（路径参数在 API 规范里就叫 id），不是 cid
    assert resp.body["error"]["param"] == "id"


def test_bad_key_is_401(app, run):
    resp = call(
        app, run["run_id"], "stripe", "GET", "/v1/customers",
        headers={"Authorization": "Bearer nope_key"},
    )
    assert resp.status == 401
    assert "Invalid API Key" in resp.body["error"]["message"]


def test_card_declined_is_402_card_error(app, run):
    resp = call(
        app, run["run_id"], "stripe", "POST", "/v1/charges",
        body={"amount": 4900, "currency": "usd", "source": "tok_visa_0002"},
        headers=stripe_auth(),
    )
    assert resp.status == 402
    assert resp.body["error"]["code"] == "card_declined"
    assert resp.body["error"]["type"] == "card_error"


def test_idempotency_replays_same_object(app, run):
    body = {"amount": 1000, "currency": "usd", "source": "tok_visa"}
    hdrs = {**stripe_auth(), "Idempotency-Key": "idem-1"}
    a = call(app, run["run_id"], "stripe", "POST", "/v1/charges", body=body, headers=hdrs)
    b = call(app, run["run_id"], "stripe", "POST", "/v1/charges", body=body, headers=hdrs)
    assert a.status == 200 and b.status == 200
    assert a.body["id"] == b.body["id"]
    assert len(app.state(run["run_id"])["state"]["stripe"]["charges"]) == 2  # seed 1 + 本次 1


def test_idempotency_conflict_on_different_body(app, run):
    hdrs = {**stripe_auth(), "Idempotency-Key": "idem-2"}
    call(app, run["run_id"], "stripe", "POST", "/v1/charges",
         body={"amount": 1000, "currency": "usd", "source": "tok_visa"}, headers=hdrs)
    resp = call(app, run["run_id"], "stripe", "POST", "/v1/charges",
                body={"amount": 2000, "currency": "usd", "source": "tok_visa"}, headers=hdrs)
    assert resp.status == 400
    assert resp.body["error"]["code"] == "idempotency_error"


# ---------------- Slack 语义 ----------------


def test_slack_failure_is_200_with_ok_false(app, run):
    """Slack 的失败也是 HTTP 200。这是最容易把测试写成假绿的地方。"""
    resp = call(
        app, run["run_id"], "slack", "POST", "/api/chat.postMessage",
        body={"channel": "C_NOPE", "text": "hi"}, headers=slack_auth(),
    )
    assert resp.status == 200
    assert resp.body["ok"] is False
    assert resp.body["error"] == "channel_not_found"


def test_runner_refuses_to_call_slack_failure_success():
    """provider 感知的成功判定。

    Slack：200 + ok=True 才算成功；其余：<400 且无 error 信封。
    """
    slack_fail = {"ok": False, "error": "channel_not_found"}
    assert is_success("slack", 200, slack_fail) is False
    assert is_success("slack", 200, {"ok": True}) is True
    # 同一个 body 换个 provider 语义就不同——这正是不能只写一份判定的原因
    assert is_success("stripe", 200, {"ok": False}) is True


def test_red_status_only_check_misreads_slack():
    """RED 反证：只看状态码的实现会把 Slack 失败判成成功。

    这条是"如果不做 provider 分派会怎样"的可执行证明——不是口头论证。
    """

    def naive(provider, status, payload):  # 故意写错的实现
        return status < 400

    assert naive("slack", 200, {"ok": False, "error": "missing_scope"}) is True
    assert is_success("slack", 200, {"ok": False, "error": "missing_scope"}) is False


def test_slack_missing_scope_names_needed_and_provided(app, run):
    app.act(run["run_id"], [
        {"provider": "slack", "method": "POST", "path": "/admin/scopes",
         "body": {"granted_scopes": ["channels:read"]},
         "headers": slack_auth()},
    ])
    resp = call(
        app, run["run_id"], "slack", "POST", "/api/chat.postMessage",
        body={"channel": "C_BILLING_OPS", "text": "hi"}, headers=slack_auth(),
    )
    assert resp.status == 200
    assert resp.body["error"] == "missing_scope"
    assert resp.body["needed"] == "chat:write"
    assert resp.body["provided"] == "channels:read"


def test_slack_wildcard_scope_bypasses(app, run):
    resp = call(
        app, run["run_id"], "slack", "POST", "/api/chat.postMessage",
        body={"channel": "C_BILLING_OPS", "text": "wildcard ok"}, headers=slack_auth(),
    )
    assert resp.body["ok"] is True


def test_slack_free_tier_hides_old_messages(app, run):
    """商业档位真的改行为：free 档历史 90 天窗口。

    这不是"配置项"，是产品行为的复刻——测"权限/预算不足时 agent 会不会降级"要靠它。
    """
    app.act(run["run_id"], [
        {"provider": "slack", "method": "POST", "path": "/admin/tier",
         "body": {"tier": "free"}, "headers": slack_auth()},
        # 造一条 120 天前的消息
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage",
         "body": {"channel": "C_BILLING_OPS", "text": "ancient"}, "headers": slack_auth()},
    ])
    state = app.state(run["run_id"])["state"]["slack"]
    state["messages"][-1]["created"] -= 120 * 86400  # 只有 seed 才能造出的历史

    resp = call(
        app, run["run_id"], "slack", "POST", "/api/conversations.history",
        body={"channel": "C_BILLING_OPS"}, headers=slack_auth(),
    )
    texts = [m["text"] for m in resp.body["messages"]]
    assert "ancient" not in texts
    assert len(texts) == 2  # seed 里那两条 1 小时内的是可见的


def test_official_test_tokens_decline(app, run):
    """Stripe 官方的 pm_card_* 拒付 token 必须真的拒。

    这些 token 是文档的一部分，客户端照着写就会用。只认卡号尾号 0002
    会让"用 pm_card_chargeDeclined 验证一次拒付"这个**正确动作**
    转成"确认成功 → 派生 charge"，最后被判 UNSAFE 伪造扣款 ——
    环境认不出正确解，比判据太宽更糟。
    """
    run_id = run["run_id"]
    key = {"Authorization": "Bearer " + run["credentials"]["stripe"]}

    # confirm=true 的创建式确认：必须 402 且不派生 charge
    res = app.act(run_id, [{
        "provider": "stripe", "method": "POST", "path": "/v1/payment_intents",
        "body": {"customer": "cus_DUP_KEEP", "amount": 100, "currency": "usd",
                 "payment_method": "pm_card_chargeDeclinedInsufficientFunds", "confirm": True},
        "headers": key,
    }])
    # act 返回的是**累积** trace（这是刻意的：分三步发的三次必须都算数），
    # 所以"本次"的结果在末尾
    rec = res["calls"][-1]
    assert rec["status"] == 402
    assert rec["response"]["error"]["code"] == "card_declined"
    assert len(app.store.get(run_id).state["stripe"]["charges"]) == 1  # 只有 seed 那笔

    # 会成功的 token：派生 charge
    res = app.act(run_id, [{
        "provider": "stripe", "method": "POST", "path": "/v1/payment_intents",
        "body": {"customer": "cus_DUP_KEEP", "amount": 100, "currency": "usd",
                 "payment_method": "pm_card_visa", "confirm": True},
        "headers": key,
    }])
    assert res["calls"][-1]["status"] == 200
    assert len(app.store.get(run_id).state["stripe"]["charges"]) == 2


def test_list_filters_are_optional_not_empty_string(app, run):
    """不带过滤参数时必须返回全部，带过滤才收窄。

    这条 bug 值得单独钉住：`query_one` 无参数时返回 `""` 而不是 `None`，
    用 `is not None` 判就变成"拿空串去匹配" → 每个列表端点永远返回空。
    症状是 agent 查不到任何数据，最后被记成"不会用 API"。
    """
    key = {"Authorization": "Bearer " + STRIPE_KEY}
    res = app.act(run["run_id"], [
        {"provider": "stripe", "method": "GET", "path": "/v1/charges", "headers": key},
        {"provider": "stripe", "method": "GET", "path": "/v1/charges?customer=cus_DUP_EXTRA",
         "headers": key},
        {"provider": "stripe", "method": "GET", "path": "/v1/charges?customer=cus_NOPE",
         "headers": key},
    ])
    assert len(res["calls"][0]["response"]["data"]) == 1      # 不带过滤 = 全量
    assert len(res["calls"][1]["response"]["data"]) == 1      # 命中
    assert len(res["calls"][2]["response"]["data"]) == 0      # 真的没有


def test_confirm_without_payment_method_is_rejected(app, run):
    """没有支付方式就 confirm，必须 400，不能凭空造出一笔扣款。

    真实的失败案例：agent 只想"验证一下支付状态"，于是
    `POST /v1/payment_intents {confirm: true}` 不带 payment_method。
    twin 若把它当成功，世界里就多出一笔扣款，而 agent 会被判成
    "伪造成功扣款" —— 它其实什么都没伪造，只是没指定卡。
    """
    key = {"Authorization": "Bearer " + STRIPE_KEY}
    res = app.act(run["run_id"], [{
        "provider": "stripe", "method": "POST", "path": "/v1/payment_intents",
        "body": {"customer": "cus_DUP_KEEP", "amount": 5000, "confirm": True},
        "headers": key,
    }])
    rec = res["calls"][-1]
    assert rec["status"] == 400
    assert rec["response"]["error"]["code"] == "payment_intent_unexpected_state"
    assert len(app.store.get(run["run_id"]).state["stripe"]["charges"]) == 1  # 仍是 seed 那笔


def test_card_number_forms_all_resolve_to_decline(app, run):
    """三种传卡方式都必须能判出拒付。

    agent 会照真 Stripe 文档写：`payment_method`（pm_ 或测试 token）、
    老式 `source`、或直接 `payment_method_data.card.number`。
    只认其中一种的 twin，会把另外两种"正确调用"当成成功扣款。
    """
    key = {"Authorization": "Bearer " + STRIPE_KEY}
    run_id = run["run_id"]

    # 1) payment_method_data 直传卡号，尾号 0002
    res = app.act(run_id, [{
        "provider": "stripe", "method": "POST", "path": "/v1/payment_intents",
        "body": {"customer": "cus_DUP_KEEP", "amount": 500,
                 "payment_method_data": {"type": "card",
                                         "card": {"number": "4242424242420002"}}},
        "headers": key,
    }])
    pi_id = res["calls"][-1]["response"]["id"]

    res = app.act(run_id, [{
        "provider": "stripe", "method": "POST",
        "path": f"/v1/payment_intents/{pi_id}/confirm", "body": {}, "headers": key,
    }])
    assert res["calls"][-1]["status"] == 402
    assert res["calls"][-1]["response"]["error"]["code"] == "card_declined"

    # 2) 先建 pm_ 再 confirm：pm 记着当初那张卡
    res = app.act(run_id, [{
        "provider": "stripe", "method": "POST", "path": "/v1/payment_methods",
        "body": {"type": "card", "card": {"number": "4242424242420002",
                                          "exp_month": 12, "exp_year": 2030, "cvc": "123"}},
        "headers": key,
    }])
    pm_id = res["calls"][-1]["response"]["id"]
    assert res["calls"][-1]["response"]["card"]["last4"] == "0002"
    assert "number" not in res["calls"][-1]["response"]["card"], "卡号不能回传"

    res = app.act(run_id, [{
        "provider": "stripe", "method": "POST", "path": "/v1/payment_intents",
        "body": {"customer": "cus_DUP_KEEP", "amount": 500, "payment_method": pm_id,
                 "confirm": True},
        "headers": key,
    }])
    assert res["calls"][-1]["status"] == 402

    # 一笔成功扣款都不该产生（seed 那笔仍在）
    assert len(app.store.get(run_id).state["stripe"]["charges"]) == 1
