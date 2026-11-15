"""端点缺口回归测试。

存在的理由：480 episodes 跑完，`stub_calls=508`、`observed coverage 29.3%` ——
**一半以上的请求打在了假数据上**。而 stub 是"schema 合法但内容是空的"，
agent 拿到它之后会基于空数据做判断，最后被记成 FAIL。

也就是说：这部分失分不是模型不会干活，是环境没给数据。
补端点前先写这张表，是为了让"补了"这件事可被证伪 —— 每条断言在补之前必须红。

每条测试的判据不是"状态码 200"，而是**返回里含某个来自 seed 的真实字段**。
只用 200 判过的测试对 stub 也成立（stub 也回 200），那就是装饰。
"""

from __future__ import annotations

import json

import pytest

from argaclone.server import App

TOKENS = {
    "stripe": "sk_test_cov",
    "slack": "xoxb-cov-zddkz",
    "github": "ghp_cov1234567890",
    "jira": "jira-cov-token",
    "linear": "lin_api_cov",
    "notion": "ntn_cov",
    "gmail": "ya29.cov",
    "drive": "ya29.cov",
    "calendar": "ya29.cov",
}


def _world(tmp_path):
    """一个三方都 seed 过的 run：github + slack + stripe，字段固定可断言。"""
    app = App(tmp_path)
    view = app.provision({"twins": "github,slack,stripe", "ttl": 600})
    run = app.store.get(view["run_id"])
    st = run.state

    gh = st["github"]
    gh["repos"]["acme/svc"] = {
        "full_name": "acme/svc", "name": "svc", "private": False,
        "default_branch": "main", "stargazers_count": 11,
    }
    gh["repos"]["acme/docs"] = {
        "full_name": "acme/docs", "name": "docs", "private": False,
        "default_branch": "main", "stargazers_count": 3,
    }
    gh["branches"]["acme/svc"] = {
        "main": "a" * 40, "feature": "b" * 40, "feature/deep/nest": "c" * 40,
    }
    gh["contents"]["acme/svc"] = {
        "README.md": {"raw": "svc readme", "sha": "1" * 40},
        "src/main.py": {"raw": "print('hi')", "sha": "2" * 40},
        "src/util/io.py": {"raw": "def load(): pass", "sha": "3" * 40},
    }
    gh["issues"]["acme/svc"] = {}
    gh["pulls"]["acme/svc"] = {}
    gh["commits"]["acme/svc"] = [
        {"sha": "d" * 40, "commit": {"message": "fix: checkout retry"}, "author": {"login": "dev1"}},
    ]
    gh["issues"]["acme/svc"]["56"] = {
        "number": 56, "title": "Refactor checkout handler", "body": "",
        "state": "open", "labels": ["regression"], "user": {"login": "dev1"},
    }
    gh["pulls"]["acme/svc"]["56"] = {
        "number": 56, "title": "Refactor checkout handler", "body": "",
        "state": "open", "merged": False,
        "head": {"ref": "feature/deep/nest"}, "base": {"ref": "main"},
    }
    gh["collaborators"]["acme/svc"] = ["dana"]

    sl = st["slack"]
    sl["channels"]["C_OPS"] = {"id": "C_OPS", "name": "ops", "topic": {"value": "ops"}}
    sl["users"]["U_DANA"] = {"id": "U_DANA", "name": "dana", "profile": {"email": "dana@acme.test"}}
    sl["messages"] = [
        {"type": "message", "ts": "1700000000.000001", "text": "deploy failed", "user": "U_DANA",
         "channel": "C_OPS", "thread_ts": None, "created": 1700000000},
        {"type": "message", "ts": "1700000001.000002", "text": "on it", "user": "U_DANA",
         "channel": "C_OPS", "thread_ts": "1700000000.000001", "created": 1700000001},
    ]

    sp = st["stripe"]
    sp["customers"]["cus_COV"] = {
        "id": "cus_COV", "object": "customer", "created": 1700000000, "livemode": False,
        "email": "cov@acme.test", "name": "Cov Ltd", "metadata": {},
    }
    sp["prices"]["price_COV"] = {
        "id": "price_COV", "object": "price", "created": 1700000000, "livemode": False,
        "product": "prod_COV", "unit_amount": 4900, "currency": "usd",
        "recurring": {"interval": "month"},
    }
    sp["subscriptions"]["sub_COV"] = {
        "id": "sub_COV", "object": "subscription", "created": 1700000000, "livemode": False,
        "customer": "cus_COV", "status": "active", "cancel_at_period_end": False,
        "items": {"object": "list", "data": [{"price": "price_COV"}]}, "metadata": {},
    }
    return app, view["run_id"]


def _get(tmp_path, provider, path, token=None):
    app, run_id = _world(tmp_path)
    gw = app.gateway
    resp = gw.handle(
        "GET", f"{provider}.{run_id}.twins.local", path,
        {"Authorization": f"Bearer {token or TOKENS[provider]}"}, b"",
    )
    return resp


# ---------------- GitHub：读仓库 ----------------
# 这些是 agent 找东西的入口。缺任何一个，它就只能猜路径 —— 而猜错的代价
# 是步数，最后测到的是运气不是判断力。

@pytest.mark.parametrize(
    "path,needle",
    [
        # 88 次 stub 的第一名：列仓库根目录
        ("/repos/acme/svc/contents", "README.md"),
        # 60 次：我这个 token 能看到哪些仓库
        ("/user/repos", "acme/svc"),
        ("/repos", "acme/docs"),
        ("/users/dana", "dana"),
        ("/users/dana/repos", "acme/svc"),
        # 按 sha 取 blob：trees 给出 sha，下一步必然要拿内容
        # blob 回 base64（真 GitHub 就是这样），所以断言的是 path 而不是明文
        ("/repos/acme/svc/git/blobs/" + "2" * 40, "src/main.py"),
        ("/repos/acme/svc/labels", "regression"),
        # PR 改了哪些文件、带了哪些 commit —— 评审类任务的必经两步
        ("/repos/acme/svc/pulls/56/files", "README.md"),
        ("/repos/acme/svc/pulls/56/commits", "checkout"),
        # 带斜杠的分支名：通用路由的 [^/]+ 一定匹配不到
        ("/repos/acme/svc/branches/feature/deep/nest", "feature/deep/nest"),
        ("/repos/acme/svc/compare/main...feature/deep/nest", "files"),
        ("/repos/acme/svc/git/trees/feature/deep/nest", "src/util/io.py"),
    ],
)
def test_github_read_endpoints_return_seeded_data(tmp_path, path, needle):
    resp = _get(tmp_path, "github", path)
    assert resp.status == 200, f"{path} -> {resp.status}"
    # stub 也回 200，所以"状态码对"不是判据；要看到 seed 里的真实字段才算补上了。
    assert resp.headers.get("X-Twin-Stub") is None, f"{path} 掉进了 stub"
    assert needle in json.dumps(resp.body, ensure_ascii=False), f"{path} 没有返回 {needle}: {resp.body}"
    assert resp.headers.get("X-Twin-Stub") is None, f"{path} 掉进了 stub"
    body = resp.body if isinstance(resp.body, (dict, list)) else {}
    assert needle in str(body), f"{path} 返回里没有 {needle!r}：{str(body)[:200]}"


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/search/issues?q=checkout", "56"),
        # 搜的是文件内容：seed 里 src/main.py 含 print('hi')
        ("/search/code?q=print", "src/main.py"),
        ("/search/repositories?q=svc", "acme/svc"),
    ],
)
def test_github_search_endpoints(tmp_path, path, needle):
    resp = _get(tmp_path, "github", path)
    assert resp.status == 200
    assert resp.headers.get("X-Twin-Stub") is None
    assert needle in str(resp.body)


# ---------------- Slack：读会话 ----------------

@pytest.mark.parametrize(
    "method,path,body,needle",
    [
        # 16 次 stub：读 thread。agent 看到一条消息想知道"接着说了什么"必然用它
        ("POST", "/api/conversations.replies",
         {"channel": "C_OPS", "ts": "1700000000.000001"}, "on it"),
        ("POST", "/api/conversations.members", {"channel": "C_OPS"}, "U_DANA"),
        ("POST", "/api/conversations.info", {"channel": "C_OPS"}, "ops"),
        ("POST", "/api/users.info", {"user": "U_DANA"}, "dana@acme.test"),
        ("POST", "/api/users.lookupByEmail", {"email": "dana@acme.test"}, "U_DANA"),
        ("POST", "/api/search.messages", {"query": "deploy"}, "deploy failed"),
    ],
)
def test_slack_read_methods(tmp_path, method, path, body, needle):
    app, run_id = _world(tmp_path)
    resp = app.gateway.handle(
        method, f"slack.{run_id}.twins.local", path,
        {"Authorization": "Bearer " + TOKENS["slack"], "Content-Type": "application/json"},
        __import__("json").dumps(body).encode(),
    )
    assert resp.headers.get("X-Twin-Stub") is None, f"{path} 掉进了 stub"
    # Slack 失败也是 200，所以必须查 ok，不能查状态码
    assert resp.body.get("ok") is True, f"{path} -> {resp.body}"
    assert needle in str(resp.body), f"{path} 返回里没有 {needle!r}"


# ---------------- Stripe：改订阅 / 读列表 ----------------

@pytest.mark.parametrize(
    "method,path,body,needle",
    [
        # 11 次 stub：改订阅（升级、改期、改 metadata 都走这条）
        ("POST", "/v1/subscriptions/sub_COV", {"metadata": {"plan": "scale"}}, "scale"),
        ("PATCH", "/v1/subscriptions/sub_COV", {"cancel_at_period_end": True}, "True"),
        ("GET", "/v1/payment_intents", None, "data"),
        ("GET", "/v1/payment_methods", None, "data"),
        ("GET", "/v1/prices/price_COV", None, "4900"),
        ("GET", "/v1/customers/cus_COV/subscriptions", None, "sub_COV"),
        ("GET", "/v1/customers/cus_COV/payment_methods", None, "data"),
    ],
)
def test_stripe_update_and_list(tmp_path, method, path, body, needle):
    import json

    app, run_id = _world(tmp_path)
    resp = app.gateway.handle(
        method, f"stripe.{run_id}.twins.local", path,
        {"Authorization": "Bearer " + TOKENS["stripe"], "Content-Type": "application/json"},
        json.dumps(body).encode() if body is not None else b"",
    )
    assert resp.headers.get("X-Twin-Stub") is None, f"{path} 掉进了 stub"
    assert resp.status == 200, f"{path} -> {resp.status} {resp.body}"
    assert needle in str(resp.body)


def test_subscription_update_persists_to_state(tmp_path):
    """改订阅必须真写进 state，否则判据读到的还是旧值 —— 那是假绿。"""
    import json

    app, run_id = _world(tmp_path)
    run = app.store.get(run_id)
    app.gateway.handle(
        "POST", f"stripe.{run_id}.twins.local", "/v1/subscriptions/sub_COV",
        {"Authorization": "Bearer " + TOKENS["stripe"], "Content-Type": "application/json"},
        json.dumps({"cancel_at_period_end": True, "metadata": {"plan": "scale"}}).encode(),
    )
    sub = run.state["stripe"]["subscriptions"]["sub_COV"]
    assert sub["cancel_at_period_end"] is True
    assert sub["metadata"]["plan"] == "scale"


def test_stripe_unknown_subscription_is_resource_missing(tmp_path):
    """改不存在的订阅要回 Stripe 的错误形状，不是 404 —— 客户端按 code 分支。"""
    app, run_id = _world(tmp_path)
    resp = app.gateway.handle(
        "POST", f"stripe.{run_id}.twins.local", "/v1/subscriptions/sub_NOPE",
        {"Authorization": "Bearer " + TOKENS["stripe"], "Content-Type": "application/json"},
        b"{}",
    )
    assert resp.status == 400
    assert resp.body["error"]["code"] == "resource_missing"


# ---------------- 覆盖率必须因此上升 ----------------

def test_coverage_reflects_new_routes(tmp_path):
    """补端点这件事本身要被量出来：路由数只许增不许减。"""
    from argaclone.server import build_registry

    reg = build_registry()
    counts = {n: reg.get(n).coverage().body["unique_endpoints"] for n in reg.names()}
    # 这是补完之后的实测值。写死是为了让"哪天有人删了端点"当场红。
    assert counts["github"] >= 40, counts
    assert counts["slack"] >= 18, counts
    assert counts["stripe"] >= 34, counts

def test_trailing_slash_is_tolerated(tmp_path):
    """尾斜杠必须被吃掉，不能掉进 stub。

    实测 120 episodes 里有 8 次 stub 是这条造成的：agent 请求
    `GET /repos/acme/website/contents/`（带斜杠），twin 严格匹配失败 → stub →
    agent 把"假数据"读成"仓库是空的"。真 GitHub 对此是 301，Slack 两种都认。
    """
    resp = _get(tmp_path, "github", "/repos/acme/svc/contents/")
    assert resp.headers.get("X-Twin-Stub") is None, "带尾斜杠的路径掉进了 stub"
    assert isinstance(resp.body, list) and any(i["name"] == "README.md" for i in resp.body), resp.body


# ---------------- GitHub：refs / 部署 / 缓存 / 发布 ----------------
# 这一批是 120 episodes 里剩下的大头。它们的共同点：**只读都能回 200 也没用**，
# 必须能"写进世界再读回来" —— agent 的动作是 create deployment、打 tag、发 release，
# 判据则去读那之后的状态。只实现 GET 会让这些题在判分时永远差一步。

def _call(tmp_path, provider, method, path, body=None, token=None):
    app, run_id = _world(tmp_path)
    resp = app.gateway.handle(
        method, f"{provider}.{run_id}.twins.local", path,
        {"Authorization": f"Bearer {token or TOKENS[provider]}",
         "Content-Type": "application/json"},
        json.dumps(body).encode() if body is not None else b"",
    )
    return app, resp


def test_git_ref_points_at_the_branch_sha(tmp_path):
    _, resp = _call(tmp_path, "github", "GET", "/repos/acme/svc/git/ref/heads/main")
    assert resp.status == 200, resp.body
    assert resp.headers.get("X-Twin-Stub") is None
    assert resp.body["ref"] == "refs/heads/main"
    assert resp.body["object"]["sha"] == "a" * 40


def test_git_ref_supports_slashed_branch_names(tmp_path):
    """`heads/feature/deep/nest` 有两段斜杠，通用路由必然匹配不到。"""
    _, resp = _call(tmp_path, "github", "GET", "/repos/acme/svc/git/ref/heads/feature/deep/nest")
    assert resp.status == 200, resp.body
    assert resp.body["object"]["sha"] == "c" * 40


def test_git_refs_lists_heads(tmp_path):
    _, resp = _call(tmp_path, "github", "GET", "/repos/acme/svc/git/refs")
    assert resp.status == 200
    refs = [x["ref"] for x in resp.body]
    assert "refs/heads/main" in refs and "refs/heads/feature" in refs, refs


def test_creating_a_tag_makes_it_visible_to_tags_and_refs(tmp_path):
    """打 tag 是发布类动作的收尾。只写进 refs 不写进 tags 会让后一步读不到。"""
    app, resp = _call(tmp_path, "github", "POST", "/repos/acme/svc/git/refs",
                      {"ref": "refs/tags/v1.0.0", "sha": "e" * 40})
    assert resp.status == 201, resp.body
    assert resp.body["ref"] == "refs/tags/v1.0.0"

    run_id = app.store.all_runs()[0].run_id
    out = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                             "/repos/acme/svc/git/refs/tags",
                             {"Authorization": f"Bearer {TOKENS['github']}"}, b"")
    assert out.status == 200
    assert [x["ref"] for x in out.body] == ["refs/tags/v1.0.0"]

    tags = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                              "/repos/acme/svc/tags",
                              {"Authorization": f"Bearer {TOKENS['github']}"}, b"")
    assert tags.status == 200
    assert tags.body[0]["name"] == "v1.0.0"
    assert tags.body[0]["commit"]["sha"] == "e" * 40


def test_updating_a_ref_moves_it(tmp_path):
    app, resp = _call(tmp_path, "github", "PATCH", "/repos/acme/svc/git/refs/heads/feature",
                      {"sha": "f" * 40, "force": True})
    assert resp.status == 200, resp.body
    assert resp.body["object"]["sha"] == "f" * 40

    run_id = app.store.all_runs()[0].run_id
    back = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                              "/repos/acme/svc/git/ref/heads/feature",
                              {"Authorization": f"Bearer {TOKENS['github']}"}, b"")
    assert back.body["object"]["sha"] == "f" * 40, "PATCH 之后 GET 还读旧值 = 没真改"


def test_git_commit_by_sha_carries_the_message(tmp_path):
    _, resp = _call(tmp_path, "github", "GET", "/repos/acme/svc/git/commits/" + "d" * 40)
    assert resp.status == 200, resp.body
    assert "checkout retry" in json.dumps(resp.body, ensure_ascii=False)
    assert resp.body["sha"] == "d" * 40


def test_deployment_roundtrip(tmp_path):
    app, resp = _call(tmp_path, "github", "POST", "/repos/acme/svc/deployments",
                      {"ref": "main", "environment": "production",
                       "description": "release 1.0"})
    assert resp.status == 201, resp.body
    assert resp.body["environment"] == "production"
    dep_id = resp.body["id"]

    run_id = app.store.all_runs()[0].run_id
    listing = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                                 "/repos/acme/svc/deployments",
                                 {"Authorization": f"Bearer {TOKENS['github']}"}, b"")
    assert listing.status == 200
    assert [d["environment"] for d in listing.body] == ["production"]

    status = app.gateway.handle("POST", f"github.{run_id}.twins.local",
                                f"/repos/acme/svc/deployments/{dep_id}/statuses",
                                {"Authorization": f"Bearer {TOKENS['github']}"},
                                json.dumps({"state": "success"}).encode())
    assert status.status == 201, status.body
    assert status.body["state"] == "success"

    statuses = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                                  f"/repos/acme/svc/deployments/{dep_id}/statuses",
                                  {"Authorization": f"Bearer {TOKENS['github']}"}, b"")
    assert [s["state"] for s in statuses.body] == ["success"], statuses.body


def test_actions_caches_list_and_delete(tmp_path):
    app, run_id = _world(tmp_path)
    st = app.store.get(run_id).state["github"]
    st.setdefault("actions_caches", {})["acme/svc"] = [
        {"id": 77, "key": "pip-Linux-py3.11", "ref": "refs/heads/main",
         "size_in_bytes": 1234, "created_at": "2026-01-01T00:00:00Z"},
    ]
    hdr = {"Authorization": f"Bearer {TOKENS['github']}"}
    got = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                             "/repos/acme/svc/actions/caches", hdr, b"")
    assert got.status == 200, got.body
    assert got.body["total_count"] == 1
    assert got.body["actions_caches"][0]["key"] == "pip-Linux-py3.11"

    deleted = app.gateway.handle("DELETE", f"github.{run_id}.twins.local",
                                 "/repos/acme/svc/actions/caches/77", hdr, b"")
    assert deleted.status == 204, deleted.body
    empty = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                               "/repos/acme/svc/actions/caches", hdr, b"")
    assert empty.body["total_count"] == 0, "删了还能读到 = 没真删"


def test_actions_runs_list(tmp_path):
    app, run_id = _world(tmp_path)
    st = app.store.get(run_id).state["github"]
    st.setdefault("actions_runs", {})["acme/svc"] = [
        {"id": 9, "name": "CI", "status": "completed", "conclusion": "failure",
         "head_branch": "main", "run_number": 12},
    ]
    resp = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                              "/repos/acme/svc/actions/runs",
                              {"Authorization": f"Bearer {TOKENS['github']}"}, b"")
    assert resp.status == 200, resp.body
    assert resp.body["total_count"] == 1
    assert resp.body["workflow_runs"][0]["conclusion"] == "failure"


def test_release_roundtrip(tmp_path):
    app, resp = _call(tmp_path, "github", "POST", "/repos/acme/svc/releases",
                      {"tag_name": "v1.0.0", "name": "1.0.0", "body": "notes"})
    assert resp.status == 201, resp.body
    assert resp.body["tag_name"] == "v1.0.0"
    assert resp.body["draft"] is False

    run_id = app.store.all_runs()[0].run_id
    hdr = {"Authorization": f"Bearer {TOKENS['github']}"}
    by_tag = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                                "/repos/acme/svc/releases/tags/v1.0.0", hdr, b"")
    assert by_tag.status == 200, by_tag.body
    assert by_tag.body["name"] == "1.0.0"
    listing = app.gateway.handle("GET", f"github.{run_id}.twins.local",
                                 "/repos/acme/svc/releases", hdr, b"")
    assert [r["tag_name"] for r in listing.body] == ["v1.0.0"]


# ---------------- 剩下的真端点（第二轮回填） ----------------
# 120 episodes 用 absent 桶把"agent 编的"分离出去之后，剩下的 stub 命中就只剩
# 这些**真实存在但我没实现**的端点了 —— 它们必须补，否则每一条都是一次假成功。

def _act(tmp_path, provider, method, path, body=None):
    app, run_id = _world(tmp_path)
    payload = json.dumps(body or {}).encode()
    return app.gateway.handle(
        method, f"{provider}.{run_id}.twins.local", path,
        {"Authorization": f"Bearer {TOKENS[provider]}"}, payload,
    )


def test_post_pull_review_lands_in_the_list(tmp_path):
    """评审类任务最后一步是提交 review，判据读的是"有没有 approve"。"""
    app, run_id = _world(tmp_path)
    created = app.gateway.handle(
        "POST", f"github.{run_id}.twins.local", "/repos/acme/svc/pulls/56/reviews",
        {"Authorization": f"Bearer {TOKENS['github']}"},
        json.dumps({"event": "APPROVE", "body": "looks good"}).encode(),
    )
    assert created.status in (200, 201), created.body
    assert created.body["state"] in ("APPROVED", "approved"), created.body

    resp = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/repos/acme/svc/pulls/56/reviews",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert any(r.get("state") == "APPROVED" for r in resp.body), resp.body


def test_create_repo_label_then_list_it(tmp_path):
    """建标签是分诊收尾动作；只给"给 issue 加标签"而没给"建标签"，判据会找不到它。"""
    app, run_id = _world(tmp_path)
    created = app.gateway.handle(
        "POST", f"github.{run_id}.twins.local", "/repos/acme/svc/labels",
        {"Authorization": f"Bearer {TOKENS['github']}"},
        json.dumps({"name": "triage", "color": "ededed"}).encode(),
    )
    assert created.status in (200, 201), created.body
    assert created.body["name"] == "triage"

    listed = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/repos/acme/svc/labels",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert "triage" in [lbl["name"] for lbl in listed.body], listed.body


def test_org_membership_lookup_and_removal(tmp_path):
    """离职回收权限：先查成员关系，再把人不带副作用地移出组织。"""
    app, run_id = _world(tmp_path)
    # 组织成员由 `collaborators["acme/svc"] = ["dana"]` 派生，不需要另建成员表

    def call(method, path):
        return app.gateway.handle(
            method, f"github.{run_id}.twins.local", path,
            {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
        )

    got = call("GET", "/orgs/acme/memberships/dana")
    assert got.status == 200, got.body
    assert got.body["state"] == "active"
    assert got.body["user"]["login"] == "dana"

    removed = call("DELETE", "/orgs/acme/members/dana")
    assert removed.status in (204, 200), removed.body
    assert call("GET", "/orgs/acme/memberships/dana").status == 404
    assert "dana" not in [m["login"] for m in call("GET", "/orgs/acme/members").body]


def test_org_invitations_and_user_orgs(tmp_path):
    app, run_id = _world(tmp_path)

    inv = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/orgs/acme/invitations",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert isinstance(inv.body, list)

    orgs = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/users/dana/orgs",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert "acme" in [o["login"] for o in orgs.body], orgs.body


def test_forks_and_repo_events(tmp_path):
    app, run_id = _world(tmp_path)
    forks = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/repos/acme/svc/forks",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert isinstance(forks.body, list)

    events = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/repos/acme/svc/events",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert isinstance(events.body, list)
    assert events.headers.get("X-Twin-Stub") is None


def test_stripe_subscription_items_are_listable(tmp_path):
    """改订阅前先读现有 item：`GET /v1/subscription_items?subscription=` 是官方做法。"""
    app, run_id = _world(tmp_path)
    resp = app.gateway.handle(
        "GET", f"stripe.{run_id}.twins.local", "/v1/subscription_items?subscription=sub_COV",
        {"Authorization": f"Bearer {TOKENS['stripe']}"}, b"",
    )
    assert resp.status == 200, resp.body
    assert resp.headers.get("X-Twin-Stub") is None
    assert resp.body["object"] == "list"
    assert any(i["price"]["id"] == "price_COV" or i["price"] == "price_COV"
               for i in resp.body["data"]), resp.body


# ---------------- 第三轮：cov2 里最后 5 条真欠账 ----------------
# 89.2% 之后剩下的 stub 命中不再是"跨 provider 打错 Host"，而是这 5 个
# 真实存在、我却没实现的端点。补它们的判据仍然一样：返回里必须含 seed 里的真值。

def test_actions_cache_usage_reports_real_numbers(tmp_path):
    """`cache/usage` 是"构建缓存占了多少配额"的那一问。空对象回 200 等于说"没用量"。"""
    app, run_id = _world(tmp_path)
    gh = app.store.get(run_id).state["github"]
    gh["actions_caches"]["acme/svc"] = [
        {"id": 1, "key": "pip-abc", "size_in_bytes": 1200, "ref": "refs/heads/main"},
        {"id": 2, "key": "pip-def", "size_in_bytes": 800, "ref": "refs/heads/main"},
    ]
    resp = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/repos/acme/svc/actions/cache/usage",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert resp.status == 200, resp.body
    assert resp.body["active_caches_count"] == 2
    assert resp.body["active_caches_size_in_bytes"] == 2000


def test_repo_invitations_are_listed(tmp_path):
    app, run_id = _world(tmp_path)
    gh = app.store.get(run_id).state["github"]
    gh["repo_invitations"]["acme/svc"] = [
        {"id": 501, "invitee": {"login": "erin"}, "permissions": "write"},
    ]
    resp = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/repos/acme/svc/invitations",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert resp.status == 200, resp.body
    assert resp.body[0]["invitee"]["login"] == "erin"


def test_collaborator_permission_is_readable(tmp_path):
    """离职清理的第一步是"这个人有什么权限"，不是"列出所有人"。"""
    resp = _get(tmp_path, "github", "/repos/acme/svc/collaborators/dana/permission")
    assert resp.status == 200, resp.body
    assert resp.body["user"]["login"] == "dana"
    assert resp.body["permission"] in ("push", "admin", "pull")


def test_outside_collaborators_exclude_org_members(tmp_path):
    app, run_id = _world(tmp_path)
    gh = app.store.get(run_id).state["github"]
    # 组织成员表不是 state 的必备键（从没有组织操作时它就不存在），
    # 所以这里用 setdefault 造出来，而不是直接下标赋值。
    gh.setdefault("org_members", {})["acme"] = ["dana"]
    gh["collaborators"]["acme/docs"] = ["dana", "frank"]
    resp = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/orgs/acme/outside_collaborators",
        {"Authorization": f"Bearer {TOKENS['github']}"}, b"",
    )
    assert resp.status == 200, resp.body
    logins = [u["login"] for u in resp.body]
    assert "frank" in logins
    assert "dana" not in logins, "组织成员不该出现在外部协作者列表里"


def test_delete_collaborator_removes_and_404s_after(tmp_path):
    app, run_id = _world(tmp_path)
    hdr = {"Authorization": f"Bearer {TOKENS['github']}"}
    gone = app.gateway.handle(
        "DELETE", f"github.{run_id}.twins.local", "/repos/acme/svc/collaborators/dana", hdr, b""
    )
    assert gone.status == 204, gone.body
    again = app.gateway.handle(
        "GET", f"github.{run_id}.twins.local", "/repos/acme/svc/collaborators/dana", hdr, b""
    )
    assert again.status == 404, "删掉之后再查必须是 404，否则判据读到的是没删成功"


# ---------------- 第四轮：80×3 那轮暴露的 13 条欠账 ----------------
# 判据同前：返回里要有 seed 里的真值；只判 200 对 stub 也成立。

def _on(app, run_id, provider, method, path, body=None):
    """同一个 app 实例上打请求。第四轮的测试要"先 seed 再查"，
    而 `_get`/`_act` 每次都会建一个新 run —— seed 会落在另一个 run 上，
    表现为"明明塞了数据却查不到"，这种假失败比真 bug 更浪费时间。"""
    payload = json.dumps(body).encode() if body is not None else b""
    return app.gateway.handle(
        method, f"{provider}.{run_id}.twins.local", path,
        {"Authorization": f"Bearer {TOKENS[provider]}", "Content-Type": "application/json"},
        payload,
    )


def test_environments_cover_deployments_and_explicit_ones(tmp_path):
    app, run_id = _world(tmp_path)
    gh = app.store.get(run_id).state["github"]
    gh.setdefault("environments", {}).setdefault("acme/svc", [{"id": 1, "name": "staging"}])
    gh["deployments"]["acme/svc"] = [{"id": 1001, "environment": "production", "ref": "main"}]
    resp = _on(app, run_id, "github", "GET", "/repos/acme/svc/environments")
    assert resp.status == 200, resp.body
    names = [e["name"] for e in resp.body["environments"]]
    assert "production" in names and "staging" in names


def test_single_deployment_is_addressable_by_id(tmp_path):
    app, run_id = _world(tmp_path)
    gh = app.store.get(run_id).state["github"]
    gh["deployments"]["acme/svc"] = [{"id": 1001, "environment": "production", "ref": "main"}]
    ok = _on(app, run_id, "github", "GET", "/repos/acme/svc/deployments/1001")
    assert ok.status == 200 and ok.body["ref"] == "main"
    assert _on(app, run_id, "github", "GET", "/repos/acme/svc/deployments/9999").status == 404


def test_matching_refs_filters_by_prefix(tmp_path):
    resp = _get(tmp_path, "github", "/repos/acme/svc/git/matching-refs/heads")
    assert resp.status == 200, resp.body
    refs = [r["ref"] for r in resp.body]
    assert "refs/heads/main" in refs


def test_commits_can_be_addressed_by_branch_name(tmp_path):
    """`/commits/<branch>` 是真实 GitHub 的行为；只认 sha 会让分诊类任务拿 404。"""
    resp = _get(tmp_path, "github", "/repos/acme/svc/commits/main")
    assert resp.status == 200, resp.body
    assert "sha" in resp.body


def test_blob_sha_is_derived_from_content(tmp_path):
    app, run_id = _world(tmp_path)
    hdr = {"Authorization": f"Bearer {TOKENS['github']}"}
    body = json.dumps({"content": "hello", "encoding": "utf-8"}).encode()
    made = app.gateway.handle("POST", f"github.{run_id}.twins.local",
                              "/repos/acme/svc/git/blobs", hdr, body)
    assert made.status == 201, made.body
    again = app.gateway.handle("POST", f"github.{run_id}.twins.local",
                               "/repos/acme/svc/git/blobs", hdr, body)
    assert made.body["sha"] == again.body["sha"], "同样内容必须算出同样的 sha"


def test_repo_side_tables_are_readable(tmp_path):
    app, run_id = _world(tmp_path)
    gh = app.store.get(run_id).state["github"]
    gh.setdefault("stargazers", {})["acme/svc"] = ["dana"]
    gh.setdefault("repo_teams", {})["acme/svc"] = ["platform"]
    gh.setdefault("workflows", {})["acme/svc"] = [{"id": 7, "name": "ci", "path": ".github/workflows/ci.yml"}]
    stars = _on(app, run_id, "github", "GET", "/repos/acme/svc/stargazers")
    assert stars.status == 200 and stars.body[0]["login"] == "dana"
    teams = _on(app, run_id, "github", "GET", "/repos/acme/svc/teams")
    assert teams.status == 200 and teams.body[0]["slug"] == "platform"
    flows = _on(app, run_id, "github", "GET", "/repos/acme/svc/actions/workflows")
    assert flows.status == 200 and flows.body["workflows"][0]["name"] == "ci"


def test_org_detail_reports_public_repo_count(tmp_path):
    resp = _get(tmp_path, "github", "/orgs/acme")
    assert resp.status == 200, resp.body
    assert resp.body["public_repos"] >= 2
    assert _get(tmp_path, "github", "/orgs/nope").status == 404


def test_stripe_cancel_endpoint_is_same_as_delete(tmp_path):
    """`POST /subscriptions/{id}/cancel` 与 `DELETE` 同义，两个都真实存在。"""
    app, run_id = _world(tmp_path)
    hdr = {"Authorization": f"Bearer {TOKENS['stripe']}"}
    resp = app.gateway.handle("POST", f"stripe.{run_id}.twins.local",
                              "/v1/subscriptions/sub_COV/cancel", hdr, b"{}")
    assert resp.status == 200, resp.body
    assert resp.body["status"] == "canceled"


def test_subscription_item_can_be_added(tmp_path):
    app, run_id = _world(tmp_path)
    hdr = {"Authorization": f"Bearer {TOKENS['stripe']}"}
    body = json.dumps({"subscription": "sub_COV", "price": "price_COV"}).encode()
    resp = app.gateway.handle("POST", f"stripe.{run_id}.twins.local",
                              "/v1/subscription_items", hdr, body)
    assert resp.status == 200, resp.body
    assert resp.body["subscription"] == "sub_COV"
    bad = app.gateway.handle("POST", f"stripe.{run_id}.twins.local",
                             "/v1/subscription_items", hdr,
                             json.dumps({"subscription": "sub_NOPE", "price": "price_COV"}).encode())
    assert bad.status == 400, "真 Stripe 对不存在的 subscription 回 400 resource_missing"


def test_balance_transactions_filter_by_source(tmp_path):
    app, run_id = _world(tmp_path)
    st = app.store.get(run_id).state["stripe"]
    st["charges"]["ch_COV"] = {"id": "ch_COV", "amount": 4900, "currency": "usd",
                               "customer": "cus_COV", "status": "succeeded"}
    resp = _on(app, run_id, "stripe", "GET", "/v1/balance_transactions?source=ch_COV")
    assert resp.status == 200, resp.body
    assert [r["source"] for r in resp.body["data"]] == ["ch_COV"]


def test_webhook_endpoints_are_listed(tmp_path):
    resp = _get(tmp_path, "stripe", "/v1/webhook_endpoints")
    assert resp.status == 200, resp.body
    assert isinstance(resp.body["data"], list)


def test_slack_pins_roundtrip_and_legacy_history(tmp_path):
    """`pins.list` 与 `channels.history` 都是真实 Slack 方法，只是名字老。"""
    app, run_id = _world(tmp_path)
    hdr = {"Authorization": f"Bearer {TOKENS['slack']}"}
    st = app.store.get(run_id).state["slack"]
    ts = st["messages"][0]["ts"]
    added = app.gateway.handle("POST", f"slack.{run_id}.twins.local", "/api/pins.add", hdr,
                               json.dumps({"channel": "C_OPS", "timestamp": ts}).encode())
    assert added.status == 200, added.body
    listed = app.gateway.handle("POST", f"slack.{run_id}.twins.local", "/api/pins.list", hdr,
                                json.dumps({"channel": "C_OPS"}).encode())
    assert listed.status == 200
    assert listed.body["items"][0]["message"]["ts"] == ts
    legacy = app.gateway.handle("POST", f"slack.{run_id}.twins.local", "/api/channels.history", hdr,
                                json.dumps({"channel": "C_OPS"}).encode())
    assert legacy.status == 200, legacy.body
    assert legacy.body["messages"]
