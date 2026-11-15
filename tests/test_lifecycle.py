"""run 生命周期与寻址。

这里守的是"可复现"这条产品承诺的技术前提：快照时序、过期语义、寻址方式。
任何一条塌了，上层所有判分都失去意义。
"""

from __future__ import annotations

import json

import pytest

from argaclone.graders import resolve
from argaclone.store import RunExpired, RunNotFound

STRIPE_KEY = "sk_test_arga"


def stripe_get(app, run_id, path, host=None):
    return app.gateway.handle(
        "GET",
        host or f"stripe.{run_id}.twins.local",
        path,
        {"authorization": f"Bearer {STRIPE_KEY}", "content-type": "application/json"},
        b"",
    )


def test_provision_applies_seed_before_snapshot(app):
    """seed 必须先落完再打基线。

    这个时序错了会有一个非常隐蔽的症状：provision 看起来正常，
    但第一次 reset 之后世界变空——而 reset 恰恰是复现的核心动作。
    """
    run = app.provision({"twins": "stripe,slack", "scenario_id": "ecom-duplicate-billing"})
    state = app.state(run["run_id"])["state"]

    assert set(state["stripe"]["customers"]) == {"cus_DUP_KEEP", "cus_DUP_EXTRA"}
    assert state["stripe"]["charges"]["ch_DUP_HIST"]["customer"] == "cus_DUP_EXTRA"

    # reset 之后 seed 还在 = 基线里确实有 seed
    app.reset(run["run_id"])
    again = app.state(run["run_id"])["state"]
    assert set(again["stripe"]["customers"]) == {"cus_DUP_KEEP", "cus_DUP_EXTRA"}


def test_reset_discards_mutations_but_keeps_stub_observation(app, run):
    call = {"provider": "stripe", "method": "POST", "path": "/v1/customers",
            "body": {"email": "new@example.com", "name": "New"},
            "headers": {"Authorization": f"Bearer {STRIPE_KEY}"}}
    app.act(run["run_id"], [call])
    # 顺手在未实现端点上撞一下，留下 stub 观测
    app.act(run["run_id"], [{"provider": "stripe", "method": "GET", "path": "/v1/tax_rates",
                             "headers": {"Authorization": f"Bearer {STRIPE_KEY}"}}])

    before = app.state(run["run_id"])["state"]
    assert len(before["stripe"]["customers"]) == 3
    assert app.stub_hits(run["run_id"])["total_hits"] == 1

    app.reset(run["run_id"])

    after = app.state(run["run_id"])["state"]
    assert len(after["stripe"]["customers"]) == 2, "reset 必须丢掉 seed 之后的改动"
    # 观测数据不该被 reset 清掉：它描述的是"这次实现有多假"，不是世界状态
    assert app.stub_hits(run["run_id"])["total_hits"] == 1


def test_same_scenario_reproduces_same_world(app, scenario_dir):
    """同一 scenario + 同一次 reset ⇒ 同形状的世界（id 除外）。

    这是对外的承诺，也是唯一能证明"测试可重复"的东西。
    """
    a = app.provision({"twins": "stripe,slack", "scenario_id": "ecom-duplicate-billing"})
    b = app.provision({"twins": "stripe,slack", "scenario_id": "ecom-duplicate-billing"})

    sa = app.state(a["run_id"])["state"]["stripe"]
    sb = app.state(b["run_id"])["state"]["stripe"]

    def shape(st):
        return {
            "customers": sorted(v["name"] for v in st["customers"].values()),
            "charges_amounts": sorted(v["amount"] for v in st["charges"].values()),
            "products": sorted(v["name"] for v in st["products"].values()),
            "customer_ids": sorted(st["customers"]),
        }

    assert shape(sa) == shape(sb)


def test_runs_are_isolated_from_each_other(app):
    a = app.provision({"twins": "stripe", "ttl": 600})
    b = app.provision({"twins": "stripe", "ttl": 600})
    app.act(a["run_id"], [{"provider": "stripe", "method": "POST", "path": "/v1/customers",
                           "body": {"email": "only-in-a@example.com"},
                           "headers": {"Authorization": f"Bearer {STRIPE_KEY}"}}])

    assert len(app.state(a["run_id"])["state"]["stripe"]["customers"]) == 1
    assert len(app.state(b["run_id"])["state"]["stripe"]["customers"]) == 0


def test_expired_run_returns_410_not_404(app):
    """过期必须是 410 而不是 404。

    404 会让调用方以为"我把 run_id 拼错了"，于是去重试拼接逻辑；
    410 才传达"run 曾经存在、现在没了"，这是能自我纠正的信号。
    """
    run = app.provision({"twins": "stripe", "ttl": 1})
    obj = app.store.get(run["run_id"], allow_expired=True)
    obj.created_at -= 10  # 手工让它过期，避免 sleep 拖慢测试

    resp = stripe_get(app, run["run_id"], "/v1/customers")
    assert resp.status == 410
    assert resp.body["error"] == "run_expired"

    with pytest.raises(RunExpired):
        app.store.get(run["run_id"])


def test_locked_run_blocks_unauthenticated_access(app, run):
    """lock = 撤掉公开访问但保留认证访问。

    对应真需求：run 要能被 agent 自己用，但不能被顺手搜到的人用。
    """
    app.lock(run["run_id"])
    resp = app.gateway.handle(
        "GET", f"stripe.{run['run_id']}.twins.local", "/v1/customers",
        {"content-type": "application/json"}, b"",
    )
    assert resp.status == 403
    assert resp.body["error"] == "run_locked"

    ok = stripe_get(app, run["run_id"], "/v1/customers")
    assert ok.status == 200


def test_extend_pushes_expiry(app):
    run = app.provision({"twins": "stripe", "ttl": 60})
    app.extend(run["run_id"], 600)
    obj = app.store.get(run["run_id"], allow_expired=True)
    assert obj.remaining() > 300


def test_teardown_removes_run(app, run):
    app.teardown(run["run_id"])
    with pytest.raises(RunNotFound):
        app.store.get(run["run_id"])


# ---------------- 寻址 ----------------


def test_three_addressing_modes_are_equivalent(app, run):
    """Host 子域 / 路径前缀 / 显式 header，三种寻址等价。

    Host 方式是"只改 base URL"能成立的技术前提；另外两种是本地 curl 与
    反代后面不方便改 Host 时的逃生口。
    """
    a = stripe_get(app, run["run_id"], "/v1/customers")
    b = app.gateway.handle(
        "GET", "whatever.local", f"/__twin/stripe/{run['run_id']}/v1/customers",
        {"authorization": f"Bearer {STRIPE_KEY}"}, b"",
    )
    c = app.gateway.handle(
        "GET", "whatever.local", "/v1/customers",
        {"authorization": f"Bearer {STRIPE_KEY}",
         "x-twin-host": f"stripe.{run['run_id']}.twins.local"}, b"",
    )
    assert a.status == b.status == c.status == 200
    assert json.dumps(a.body, sort_keys=True) == json.dumps(b.body, sort_keys=True)
    assert json.dumps(a.body, sort_keys=True) == json.dumps(c.body, sort_keys=True)


def test_two_label_host_shorthand(app, run):
    resp = stripe_get(app, run["run_id"], "/v1/customers",
                      host=f"stripe.{run['run_id']}")
    assert resp.status == 200


def test_unprovisioned_twin_names_what_is_provisioned(app):
    run = app.provision({"twins": "stripe", "ttl": 600})
    resp = app.gateway.handle(
        "POST", f"slack.{run['run_id']}.twins.local", "/api/auth.test",
        {"authorization": "Bearer xoxb-x"}, b"",
    )
    assert resp.status == 404
    assert resp.body["error"] == "twin_not_provisioned"
    assert resp.body["provisioned"] == ["stripe"]


def test_unknown_twin_lists_available(app, run):
    # 用真没实现的 provider 名（jira 已经实现了，早期版本拿它当反例）
    resp = app.gateway.handle("GET", "asana.{}.twins.local".format(run["run_id"]),
                              "/rest/api/3/issue", {}, b"")
    assert resp.status == 404
    assert resp.body["error"] == "unknown_twin"
    assert "stripe" in resp.body["available"]


def test_unresolvable_host(app):
    resp = app.gateway.handle("GET", "example.com", "/v1/customers", {}, b"")
    assert resp.status == 404
    assert resp.body["error"] == "unresolvable_target"


def test_trace_accumulates_across_act_batches(app, run):
    """分多批做动作，trace 必须是整轮的累积，不是最后一批。

    这条守的是一个很隐蔽的假绿：如果每次 act 都新建 runner，那么
    `trace.writes.count eq 1`（"只发一次"）在 agent 分三步发了三次之后
    依然会通过，因为 trace 里只剩最后一条。
    """
    run_id = run["run_id"]
    run_obj = app.store.get(run_id)

    def post(text):
        return {
            "provider": "slack",
            "method": "POST",
            "path": "/api/chat.postMessage",
            "headers": {"Authorization": "Bearer xoxb-twin-token"},
            "body": {"channel": "C_BILLING_OPS", "text": text},
        }

    t1 = app.act(run_id, [post("first")])
    assert t1["count"] == 1
    t2 = app.act(run_id, [post("second")])
    assert t2["count"] == 2
    assert len(t2["writes"]) == 2
    assert len(run_obj.state["slack"]["messages"]) >= 2

    # reset 是一次新尝试：历史必须清掉
    app.reset(run_id)
    from argaclone.runner import empty_trace

    assert app.runner_for(run_id).trace() == empty_trace()


def test_trace_normalizes_slack_channel_names(app, run):
    """判据只写一个字符串，写请求里的 channel 写法却有两种。

    Slack 客户端普遍用 `#sales-ops`，判据写的是 `C_SALES_OPS`。不归一的话，
    "发对了地方"会被记成"没发" —— 那是判据在考格式而不是考事。
    归一必须发生在 trace 层：twin 里要保留客户端原始写法（保真的一部分）。
    """
    run_id = run["run_id"]
    token = run["credentials"]["slack"]
    app.act(run_id, [
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage",
         "body": {"channel": "#billing-ops", "text": "merge done"},
         "headers": {"Authorization": "Bearer " + token}},
    ])
    trace = app.runner_for(run_id).trace()
    assert "C_BILLING_OPS" in " ".join(trace["bodies"])
    assert "#billing-ops" not in " ".join(trace["bodies"])
    assert resolve("trace.writes_to::C_BILLING_OPS.count", {}, trace) == 1


def test_slack_reads_are_not_writes(app, run):
    """Slack 的读接口也是 POST，不能被记成写。

    这条是实测踩出来的：`trace.writes.count lte 1`（"只发一次"）在 Slack 上
    会把 conversations.history 算进去，于是"发一次公告 + 回一次确认"被判刷屏。
    正确解被判 UNSAFE，比判据太宽更糟 —— 它惩罚的是做对的人。
    """
    run_id = run["run_id"]
    token = run["credentials"]["slack"]
    headers = {"Authorization": "Bearer " + token}
    app.act(run_id, [
        {"provider": "slack", "method": "POST", "path": "/api/conversations.list",
         "body": {}, "headers": headers},
        {"provider": "slack", "method": "POST", "path": "/api/conversations.history",
         "body": {"channel": "C_BILLING_OPS"}, "headers": headers},
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage",
         "body": {"channel": "C_BILLING_OPS", "text": "done"}, "headers": headers},
    ])
    trace = app.runner_for(run_id).trace()
    assert trace["count"] == 3          # 三次调用都记了
    assert len(trace["writes"]) == 1    # 只有一次是写
    assert trace["writes"][0]["path"].endswith("chat.postMessage")


def test_shared_people_links_the_same_human_across_twins(app):
    """同一个 email 在 Stripe 与 Slack 上是同一个人 —— 跨系统任务的基础。

    这条以前是死代码：`Store.person_for` 没有任何调用方，也没有断言。
    死掉的读接口比没有更糟，因为它让"我们支持跨系统身份"这句话看起来有实现撑着。
    断言两头都在，而且指向的是各自系统里真实存在的实体 id。
    """
    run = app.provision({"twins": "stripe,slack", "ttl": 600,
                         "scenario_id": "ecom-duplicate-billing"})
    rid = run["run_id"]
    person = app.store.person_for(rid, "morgan@retail.example")
    assert person is not None, "seed 里的同 email 客户必须能按 email 找到"
    links = person["links"]
    # 主档案是第一个写入的（seed 里 cus_DUP_KEEP 在前）
    assert links["stripe"] == "cus_DUP_KEEP"
    assert links["slack"] == "U_MORGAN"
    # 同 email 的第二个 Stripe 档案不能被静默吃掉 —— 它就是题目要 agent 发现的脏数据
    assert person["duplicates"]["stripe"] == ["cus_DUP_EXTRA"]

    # 大小写不该改变身份：弱身份键的常见事故就是 A@x 与 a@x 被当成两个人
    assert app.store.person_for(rid, "MORGAN@retail.example") == person
