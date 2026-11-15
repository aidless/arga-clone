"""GitHub twin 的保真测试。

GitHub 是三个 twin 里唯一"权限模型本身就会改变业务结果"的一个：
同一个调用，token 的 scope 差一格，返回就是 403 而不是 404。
所以这里测的重点不是 CRUD，而是 **scope 继承、raw 免鉴权、以及"不支持"要被明说**。
"""

from __future__ import annotations

import base64
import json


TOKEN = "ghp_twin_fixture_token_not_real"


def call(app, run_id, method, path, body=None, headers=None):
    payload = json.dumps(body).encode() if body is not None else b""
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    hdrs.setdefault("content-type", "application/json")
    return app.gateway.handle(method, f"github.{run_id}.twins.local", path, hdrs, payload)


def auth(token: str = TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ---------------- 鉴权 ----------------


def test_missing_token_is_401(app, gh):
    resp = call(app, gh.run_id, "GET", "/user")
    assert resp.status == 401
    assert resp.body["message"] == "Requires authentication"


def test_wrong_token_prefix_is_401(app, gh):
    """token 前缀不是 gh* 的一律 401 —— 客户端常把 OAuth token 与 PAT 混用，
    真 GitHub 也按前缀区分，抄宽松了会让"配错凭据"变成静默可用。"""
    resp = call(app, gh.run_id, "GET", "/user", headers=auth("sk_test_arga"))
    assert resp.status == 401


def test_public_repo_readable_without_repo_scope(app, gh):
    st = gh.state["github"]
    st["granted_scopes"] = []
    resp = call(app, gh.run_id, "GET", "/repos/acme/api", headers=auth())
    assert resp.status == 200
    assert resp.body["full_name"] == "acme/api"


def test_private_repo_needs_repo_scope(app, gh):
    st = gh.state["github"]
    st["granted_scopes"] = []
    resp = call(app, gh.run_id, "GET", "/repos/acme/secret", headers=auth())
    assert resp.status == 403
    assert resp.body["message"] == "Resource not accessible by personal access token"


def test_parent_scope_grants_child(app, gh):
    """只给 `repo`，需要 `repo:status` 的调用必须放行 —— 父 scope 自动授子。"""
    st = gh.state["github"]
    st["granted_scopes"] = ["repo"]
    twin = app.registry.get("github")
    assert "repo:status" in twin.effective_scopes(st)

    from argaclone.twins.base import Request

    req = Request("GET", "/x", {}, {"authorization": f"Bearer {TOKEN}"}, b"", gh, gh.run_id, app.store)
    assert twin._check(req, "repo:status") is None
    resp = call(app, gh.run_id, "GET", "/repos/acme/api/issues", headers=auth())
    assert resp.status == 200


def test_missing_child_scope_is_403(app, gh):
    st = gh.state["github"]
    st["granted_scopes"] = ["repo:status"]  # 反向不成立：子 scope 不授父
    twin = app.registry.get("github")
    assert "repo" not in twin.effective_scopes(st)

    resp = call(app, gh.run_id, "GET", "/repos/acme/api/issues", headers=auth())
    assert resp.status == 403
    assert resp.body["message"] == "Resource not accessible by personal access token"


def test_admin_scopes_endpoint_reports_effective(app, gh):
    call(app, gh.run_id, "POST", "/admin/scopes", body={"granted_scopes": ["admin:org"]})
    resp = call(app, gh.run_id, "GET", "/admin/scopes")
    assert resp.status == 200
    assert "read:org" in resp.body["effective_scopes"]
    assert "write:org" in resp.body["effective_scopes"]


def test_red_parent_scope_inheritance_is_load_bearing(app, gh, monkeypatch):
    """RED 反证：把父 scope 继承关掉，行为必须翻转。

    只测"给了 repo 就能 `repo:status`"是描述实现；这里进一步证明
    **这个放行是由继承这条机制提供的**：摘掉它，同一个调用立刻 403。
    """
    from argaclone.twins.base import Request

    twin = app.registry.get("github")
    gh.state["github"]["granted_scopes"] = ["admin:repo_hook"]

    def need(needed: str):
        req = Request("POST", "/x", {}, {"authorization": f"Bearer {TOKEN}"}, b"", gh, gh.run_id, app.store)
        return twin._check(req, needed)

    assert need("write:repo_hook") is None, "admin:repo_hook 应自动授 write:repo_hook"

    monkeypatch.setattr(twin, "effective_scopes", lambda state: set(state["granted_scopes"]))
    flipped = need("write:repo_hook")
    assert flipped is not None and flipped.status == 403, "关掉继承后必须 403，仍是 None 说明这条测试没咬住机制"


# ---------------- 路径与前缀 ----------------


def test_api_v3_prefix_is_equivalent(app, gh):
    a = call(app, gh.run_id, "GET", "/repos/acme/api", headers=auth())
    b = call(app, gh.run_id, "GET", "/api/v3/repos/acme/api", headers=auth())
    assert a.status == b.status == 200
    assert a.body["full_name"] == b.body["full_name"]


def test_raw_content_needs_no_token(app, gh):
    resp = call(app, gh.run_id, "GET", "/raw/acme/api/main/README.md")
    assert resp.status == 200
    assert resp.to_bytes() == b"hello from main"


def test_raw_prefers_longest_branch_name(app, gh):
    """`feature` 与 `feature/x` 同时存在时，"feature/x/README.md" 里的 ref 是哪个？

    真 GitHub 按最长匹配走。抄成"取第一段"会读到 feature 分支上的、
    名字叫 x/README.md 的文件 —— 静默读错文件，比报错难查一百倍。
    """
    st = gh.state["github"]
    # 只放 feature 分支视角下才成立的那个路径：x/only-x.md
    st["contents"]["acme/api"]["x/only-x.md"] = {"raw": "wrong branch file", "sha": "9" * 40}

    resp = call(app, gh.run_id, "GET", "/raw/acme/api/feature/x/only-x.md")
    assert resp.status == 404, (
        "最长匹配把 ref 读成 feature/x、path 读成 only-x.md（不存在）；"
        "只有错读成 ref=feature 才会命中 x/only-x.md 并返回 200 —— 那是在静默读错文件"
    )


def test_raw_unknown_path_is_plain_text_404(app, gh):
    resp = call(app, gh.run_id, "GET", "/raw/acme/api/main/nope.md")
    assert resp.status == 404
    assert resp.to_bytes() == b"404: Not Found"


# ---------------- 业务语义 ----------------


def test_issue_lifecycle_and_labels(app, gh):
    created = call(app, gh.run_id, "POST", "/repos/acme/api/issues",
                   body={"title": "prod checkout 500s", "labels": ["sev1"]}, headers=auth())
    assert created.status == 201
    n = created.body["number"]

    patched = call(app, gh.run_id, "PATCH", f"/repos/acme/api/issues/{n}",
                   body={"state": "closed", "labels": ["sev1", "regression"]}, headers=auth())
    assert patched.body["state"] == "closed"
    assert patched.body["labels"] == ["sev1", "regression"]

    listed = call(app, gh.run_id, "GET", "/repos/acme/api/issues?state=all", headers=auth())
    assert [i["number"] for i in listed.body] == [n]


def test_issue_without_title_is_422_with_field_error(app, gh):
    resp = call(app, gh.run_id, "POST", "/repos/acme/api/issues", body={"body": "no title"}, headers=auth())
    assert resp.status == 422
    assert resp.body["errors"][0]["field"] == "title"


def test_merge_twice_is_405(app, gh):
    pr = call(app, gh.run_id, "POST", "/repos/acme/api/pulls",
              body={"title": "fix", "head": "feature/x", "base": "main"}, headers=auth())
    assert pr.status == 201
    n = pr.body["number"]

    first = call(app, gh.run_id, "PUT", f"/repos/acme/api/pulls/{n}/merge", body={}, headers=auth())
    assert first.status == 200 and first.body["merged"] is True

    second = call(app, gh.run_id, "PUT", f"/repos/acme/api/pulls/{n}/merge", body={}, headers=auth())
    assert second.status == 405
    assert second.body["message"] == "Pull Request is not mergeable"


def test_put_content_sha_conflict_is_409(app, gh):
    payload = {"message": "update", "content": base64.b64encode(b"new text").decode()}
    ok = call(app, gh.run_id, "PUT", "/repos/acme/api/contents/README.md", body=payload, headers=auth())
    assert ok.status == 200
    sha_after = ok.body["content"]["sha"]

    stale = dict(payload, sha="0" * 40)
    resp = call(app, gh.run_id, "PUT", "/repos/acme/api/contents/README.md", body=stale, headers=auth())
    assert resp.status == 409
    assert "does not match" in resp.body["message"]

    fresh = dict(payload, sha=sha_after)
    again = call(app, gh.run_id, "PUT", "/repos/acme/api/contents/README.md", body=fresh, headers=auth())
    assert again.status == 200


def test_workflow_dispatch_is_204(app, gh):
    resp = call(app, gh.run_id, "POST",
                "/repos/acme/api/actions/workflows/deploy.yml/dispatches",
                body={"ref": "main"}, headers=auth())
    assert resp.status == 204
    assert resp.to_bytes() == b""


# ---------------- GraphQL ----------------


def test_graphql_viewer(app, gh):
    resp = call(app, gh.run_id, "POST", "/graphql", body={"query": "{ viewer { login } }"}, headers=auth())
    assert resp.status == 200
    assert resp.body["data"]["viewer"]["login"] == "twin-bot"


def test_graphql_repository_by_name(app, gh):
    q = '{ repository(owner: "acme", name: "api") { nameWithOwner isPrivate } }'
    resp = call(app, gh.run_id, "POST", "/graphql", body={"query": q}, headers=auth())
    assert resp.body["data"]["repository"]["nameWithOwner"] == "acme/api"
    assert resp.body["data"]["repository"]["isPrivate"] is False


def test_graphql_unsupported_query_says_so(app, gh):
    """不支持的查询必须回 errors。

    回 `{"data": {}}` 是最坏的选择：调用方会以为"查到了但没字段"，
    于是把"twin 没实现"读成"这个仓库没有 issue"。
    """
    resp = call(app, gh.run_id, "POST", "/graphql",
                body={"query": "{ search(query: \"is:issue\") { issueCount } }"}, headers=auth())
    assert resp.status == 200
    assert resp.body["data"] is None
    assert resp.body["errors"][0]["type"] == "UNSUPPORTED"


# ---------------- stub 报表 ----------------


def test_stub_hits_report_is_rich(app, gh):
    """GitHub 是唯一带 recent 流水的 twin：它回答"哪几次调用撞到了假数据"。"""
    # 靶子必须真的"没实现"：`/releases` 补上之后就不再掉进 stub 了
    for _ in range(2):
        call(app, gh.run_id, "GET", "/repos/acme/api/actions/runners", headers=auth())
    call(app, gh.run_id, "GET", "/repos/acme/api/actions/secrets", headers=auth())

    resp = call(app, gh.run_id, "GET", "/admin/stub-hits")
    assert resp.status == 200
    assert resp.body["total_hits"] == 3
    assert resp.body["unique_endpoints"] == 2
    assert {s["endpoint"] for s in resp.body["summary"]} == {
        "GET /repos/acme/api/actions/runners",
        "GET /repos/acme/api/actions/secrets",
    }
    assert len(resp.body["recent"]) == 3
    assert resp.body["recent"][0]["provider"] == "github"
    assert "at" in resp.body["recent"][0]


def test_stub_hits_cleared_but_history_survives_reset(app, gh):
    call(app, gh.run_id, "GET", "/repos/acme/api/actions/runners", headers=auth())
    app.reset(gh.run_id)
    resp = call(app, gh.run_id, "GET", "/admin/stub-hits")
    assert resp.body["total_hits"] == 1, "reset 是恢复世界，不该抹掉'这个测试撞过假数据'的证据"

    call(app, gh.run_id, "POST", "/admin/stub-hits", body={})
    resp = call(app, gh.run_id, "GET", "/admin/stub-hits")
    assert resp.body["total_hits"] == 0
    assert resp.body["recent"] == []


# ---------------- webhook 签名 ----------------


def test_webhook_signature_roundtrip(app, gh):
    payload = '{"action":"opened"}'
    signed = call(app, gh.run_id, "POST", "/admin/sign", body={"payload": payload, "secret": "s3cr3t"})
    sig = signed.body["signature"]
    assert sig.startswith("sha256=")

    ok = call(app, gh.run_id, "POST", "/admin/verify-signature",
              body={"payload": payload, "secret": "s3cr3t", "signature": sig})
    assert ok.body["valid"] is True

    bad = call(app, gh.run_id, "POST", "/admin/verify-signature",
               body={"payload": payload + "x", "secret": "s3cr3t", "signature": sig})
    assert bad.body["valid"] is False


def test_unknown_admin_route_is_not_silent(app, gh):
    resp = call(app, gh.run_id, "GET", "/admin/nope")
    assert resp.status == 404
    assert resp.body["path"] == "/admin/nope"


def test_raw_file_and_tree_by_api_path(app, gh):
    """`/repos/{o}/{r}/raw/{ref}/{path}` 与 `/git/trees/{ref}` 必须真的回内容。

    这两个端点缺了，agent 只能靠猜路径找文件 —— 猜错的步数会算进它的
    pass 率里，测到的就不是判断力。而 `{path}` 含斜杠，通用路由的
    `[^/]+` 匹配不到，会静默掉进 stub，所以必须单独测。
    """
    resp = call(app, gh.run_id, "GET", "/repos/acme/api/raw/main/docs/guide.md", headers=auth())
    assert resp.status == 200
    assert resp.body == b"guide on main"

    resp = call(app, gh.run_id, "GET", "/repos/acme/api/git/trees/main", headers=auth())
    assert resp.status == 200
    paths = [item["path"] for item in resp.body["tree"]]
    assert "docs/guide.md" in paths and "README.md" in paths


def test_raw_response_is_recordable(app, gh):
    """trace 必须能 json.dumps —— 裸字节响应是唯一的破坏点。

    跑完一整轮之后在写结果文件时才抛 TypeError，代价是整轮作废。
    所以在最小单元就在这里钉住。
    """
    from argaclone.runner import Action, Runner

    runner = Runner(app.gateway, gh.run_id)
    runner.call(Action(provider="github", method="GET", path="/repos/acme/api/raw/main/README.md",
                       headers={"Authorization": "Bearer " + TOKEN}))
    json.dumps(runner.trace())  # 不抛就是通过


def test_issue_labels_roundtrip(app, gh):
    """给 issue 打标签是"分诊"类任务的标准收尾动作。

    缺这几个端点时，agent 会一直重试到步数耗尽，最后记成 FAIL ——
    看起来像它不会分诊，实际是环境没给它分诊的手。
    """
    resp = call(app, gh.run_id, "POST", "/repos/acme/api/issues", {"title": "t"}, headers=auth())
    number = resp.body["number"]

    resp = call(app, gh.run_id, "POST", f"/repos/acme/api/issues/{number}/labels",
                {"labels": ["triaged", "sev2"]}, headers=auth())
    assert resp.status == 200
    assert {lb["name"] for lb in resp.body} == {"triaged", "sev2"}

    # 重复添加不产生重复标签
    resp = call(app, gh.run_id, "POST", f"/repos/acme/api/issues/{number}/labels",
                {"labels": ["triaged"]}, headers=auth())
    assert [lb["name"] for lb in resp.body].count("triaged") == 1

    resp = call(app, gh.run_id, "GET", f"/repos/acme/api/issues/{number}/labels", headers=auth())
    assert {lb["name"] for lb in resp.body} == {"triaged", "sev2"}

    resp = call(app, gh.run_id, "DELETE", f"/repos/acme/api/issues/{number}/labels/sev2", headers=auth())
    assert [lb["name"] for lb in resp.body] == ["triaged"]

    # 删一个不存在的标签必须是 404，不能静默成功
    resp = call(app, gh.run_id, "DELETE", f"/repos/acme/api/issues/{number}/labels/nope", headers=auth())
    assert resp.status == 404
