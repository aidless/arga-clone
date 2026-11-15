"""工厂（twin_spec）的自测。

P2 的验收标准不是"工厂能跑"，而是**"新增 twin 的 diff 里不出现手写 CRUD 样板"**。
所以这里除了测工厂件的正确性，还要有一条结构断言：
拿 Jira/Linear/Notion 三个用工厂写的 twin 去 grep，看它们的源码里
有没有 `_create_/ _list_/ _update_/ _delete_` 这类逐资源手写的方法。

工厂失败的样子很好认：三个新 twin 各自复刻一遍分页、id 生成、错误形状，
每个 400 行 —— 那就等于什么都没省，只是多了一层没人用的抽象。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argaclone.store import Store
from argaclone.twin_spec import (
    ErrorShape,
    Field,
    Pager,
    Resource,
    ScopeFamily,
    SpecTwin,
    adf,
    adf_text,
    jira_errors,
    notion_errors,
)
from argaclone.twins.base import Request, Response

ROOT = Path(__file__).resolve().parent.parent


def simple_errors() -> ErrorShape:
    """最小错误形状：`{"error": "..."}`。用来测工厂，不代表任何真实 provider。"""

    def not_found(kind: str, ident: str) -> Response:
        return Response(404, {"error": f"{kind} {ident} not found"})

    def bad_request(detail: str, fld: str | None = None) -> Response:
        return Response(400, {"error": detail, "field": fld})

    def forbidden(detail: str) -> Response:
        return Response(403, {"error": detail})

    def unauthorized(detail: str) -> Response:
        return Response(401, {"error": detail})

    return ErrorShape(not_found, bad_request, forbidden, unauthorized)


# ---------------------------------------------------------------- ScopeFamily


def test_scope_family_expands_transitively():
    fam = ScopeFamily(
        {
            "write:jira-work": ["read:jira-work"],
            "manage:jira-project": ["read:jira-work"],
            "manage:jira-configuration": ["read:jira-work", "manage:jira-project"],
        }
    )
    got = fam.expand(["manage:jira-configuration"])
    assert got == {
        "manage:jira-configuration",
        "read:jira-work",
        "manage:jira-project",
    }
    assert fam.allows(["manage:jira-configuration"], "read:jira-work")
    assert not fam.allows(["read:jira-work"], "write:jira-work")


def test_scope_family_wildcard_and_unknown():
    fam = ScopeFamily({})
    assert fam.allows(["*"], "anything:at-all")
    assert not fam.allows(["read:jira-work"], "admin:everything")
    # 空 scope 列表不能因为"没有不匹配的"而放行
    assert not fam.allows([], "read:jira-work")


# --------------------------------------------------------------------- Pager


def _bare_req(query=None):
    store = Store()
    run = store.create(["x"], 60)
    return Request("GET", "/", query or {}, {}, b"", run, run.run_id, store)


def test_pager_cursor_shape():
    rows = [{"id": str(i)} for i in range(3)]
    body = Pager("cursor").apply(_bare_req({"limit": "2"}), rows)
    assert body["has_more"] is True
    assert body["results"] == rows[:2]
    assert body["next_cursor"] == "2"
    # 最后一页：next_cursor 必须是空串而不是 "3"（否则客户端会一直翻下去）
    last = Pager("cursor").apply(_bare_req({"cursor": "2", "limit": "2"}), rows)
    assert last["has_more"] is False
    assert last["next_cursor"] == ""


def test_pager_offset_shape():
    rows = [{"id": str(i)} for i in range(5)]
    body = Pager("offset").apply(_bare_req({"startAt": "1", "maxResults": "2"}), rows)
    assert body["startAt"] == 1
    assert body["maxResults"] == 2
    assert body["total"] == 5
    assert body["isLast"] is False
    assert body["results"] == rows[1:3]


def test_pager_none_returns_all_rows():
    out = Pager("none").apply(_bare_req(), [{"id": "1"}, {"id": "2"}])
    assert out["results"] == [{"id": "1"}, {"id": "2"}]
    assert "has_more" not in out


# ----------------------------------------------------------------- error 形状


def test_error_shapes_are_provider_specific():
    jira = jira_errors()
    body = jira.not_found("issue", "ABC-1").body
    assert body["errorMessages"] and isinstance(body["errorMessages"], list)
    assert body["errors"] == {}

    notion = notion_errors()
    body = notion.not_found("page", "p1").body
    assert body["object"] == "error"
    assert body["status"] == 404
    assert body["code"] == "object_not_found"

    plain = simple_errors()
    body = plain.forbidden("no").body
    assert body == {"error": "no"}


# ------------------------------------------------------------------ 工厂本体

class ToyTwin(SpecTwin):
    """最小用例：只声明一个资源，不写任何 handler。"""

    name = "toy"
    errors = simple_errors()

    def _build_routes(self) -> None:
        self.declare(
            Resource(
                name="widgets",
                collection="/v1/widgets",
                item="/v1/widgets/{id}",
                fields=[Field("name", required=True), Field("size", default=1)],
                id_prefix="wid_",
            )
        )
        self.mount()

    def initial_state(self) -> dict:
        return {"toy": {"widgets": {}}}


def _toy_twin() -> SpecTwin:
    return ToyTwin()


def _ctx(twin):
    store = Store()
    run = store.create(["toy"], 60)
    run.state[twin.name] = twin.initial_state()[twin.name]
    return store, run


def _req(twin, store, run, method, path, body=None, query=None):
    return Request(
        method,
        path,
        query or {},
        {"content-type": "application/json"},
        json.dumps(body).encode() if body is not None else b"",
        run,
        run.run_id,
        store,
    )


def test_factory_registers_crud_routes_from_declaration():
    twin = _toy_twin()
    methods = {(r.method, r.pattern.pattern if hasattr(r.pattern, "pattern") else r.pattern)
               for r in twin.routes}
    # 一份 Resource 声明换来五条路由，且没有任何手写 handler
    assert ("GET", r"^/v1/widgets$") in methods
    assert ("POST", r"^/v1/widgets$") in methods
    assert ("GET", r"^/v1/widgets/(?P<id>[^/]+)$") in methods
    assert ("PATCH", r"^/v1/widgets/(?P<id>[^/]+)$") in methods
    assert ("DELETE", r"^/v1/widgets/(?P<id>[^/]+)$") in methods


def test_factory_crud_roundtrip():
    twin = _toy_twin()
    store, run = _ctx(twin)

    created = twin.dispatch(_req(twin, store, run, "POST", "/v1/widgets", {"name": "a"}))
    assert created.status == 201
    wid = created.body["id"]
    assert wid.startswith("wid_")
    assert created.body["size"] == 1  # default 生效

    listed = twin.dispatch(_req(twin, store, run, "GET", "/v1/widgets"))
    assert listed.body["results"][0]["id"] == wid
    assert listed.body["has_more"] is False

    patched = twin.dispatch(_req(twin, store, run, "PATCH", f"/v1/widgets/{wid}", {"size": 9}))
    assert patched.body["size"] == 9

    deleted = twin.dispatch(_req(twin, store, run, "DELETE", f"/v1/widgets/{wid}"))
    assert deleted.status in (200, 204)
    missing = twin.dispatch(_req(twin, store, run, "GET", f"/v1/widgets/{wid}"))
    assert missing.status == 404


def test_factory_rejects_missing_required_field():
    twin = _toy_twin()
    store, run = _ctx(twin)
    resp = twin.dispatch(_req(twin, store, run, "POST", "/v1/widgets", {}))
    assert resp.status in (400, 422)
    assert "name" in json.dumps(resp.body)


def test_factory_paginates_with_cursor():
    twin = _toy_twin()
    store, run = _ctx(twin)
    for i in range(3):
        twin.dispatch(_req(twin, store, run, "POST", "/v1/widgets", {"name": f"w{i}"}))
    page = twin.dispatch(_req(twin, store, run, "GET", "/v1/widgets", query={"limit": "2"}))
    assert len(page.body["results"]) == 2
    assert page.body["has_more"] is True
    assert page.body["next_cursor"] == "2"


def test_factory_ids_do_not_collide_across_resources():
    class TwoRes(SpecTwin):
        name = "toy2"
        errors = simple_errors()

        def _build_routes(self) -> None:
            self.declare(Resource(name="as", collection="/v1/as", item="/v1/as/{id}",
                                  id_prefix="a_", fields=[Field("x")]))
            self.declare(Resource(name="bs", collection="/v1/bs", item="/v1/bs/{id}",
                                  id_prefix="b_", fields=[Field("x")]))
            self.mount()

        def initial_state(self) -> dict:
            return {"toy2": {"as": {}, "bs": {}}}

    twin = TwoRes()
    store, run = _ctx(twin)
    a = twin.dispatch(_req(twin, store, run, "POST", "/v1/as", {"x": "1"})).body["id"]
    b = twin.dispatch(_req(twin, store, run, "POST", "/v1/bs", {"x": "1"})).body["id"]
    assert a != b
    assert a.startswith("a_") and b.startswith("b_")


# ------------------------------------------------------------------ ADF 工具


def test_adf_wraps_plain_string_and_roundtrips():
    doc = adf("hello world")
    assert doc["type"] == "doc"
    assert doc["content"][0]["type"] == "paragraph"
    assert adf_text(doc) == "hello world"
    # 已经是 ADF 的输入必须原样返回，不能包两层
    assert adf(doc) is doc
    # None / 空串都当空文档，不能炸
    assert adf_text(adf(None)) == ""


# --------------------------------------------------- 结构断言：工厂真的省了代码

FACTORY_TWINS = ["jira_twin.py", "linear_twin.py", "notion_twin.py"]
# 分页断言要覆盖**所有**用 Pager 的 twin，而不只是最初试产那三个：
# 新增一个 twin 却漏登记，等于给自己留了个"下次可以偷偷手拼分页"的口子。
PAGER_TWINS = FACTORY_TWINS + ["hubspot_twin.py", "salesforce_twin.py"]
# 分页形状只该由 Pager 产生。如果某个 factory twin 自己写这些键，
# 说明它又把分页抄了一遍 —— 那工厂就是白建的。
PAGER_KEYS = ["has_more", "next_cursor", "startAt", "isLast", "maxResults"]


@pytest.mark.parametrize("fname", PAGER_TWINS)
def test_factory_twins_do_not_reimplement_pagination(fname):
    """用工厂写的 twin，源码里不该出现分页形状的字段名。

    这条比"不许有 _list_xxx 方法"更实在：方法名可以换，但一旦某个 twin
    自己拼 `{"has_more": ..., "next_cursor": ...}`，分页就被复刻了。
    """
    src = (ROOT / "argaclone" / "twins" / fname).read_text()
    hits = [k for k in PAGER_KEYS if f'"{k}"' in src]
    assert not hits, f"{fname} 自己拼了分页形状: {hits}（应由 Pager 统一产生）"


@pytest.mark.parametrize("fname", FACTORY_TWINS)
def test_factory_twins_are_shorter_than_handwritten_ones(fname):
    """工厂写的 twin 不该比手写的长。

    这不是审美要求：手写 twin 的长度就是维护成本。stripe/github 那种
    因为要复刻错误形状、签名、raw 内容而必须长——它们不是工厂的目标。
    """
    factory_lines = len((ROOT / "argaclone" / "twins" / fname).read_text().splitlines())
    handwritten = len((ROOT / "argaclone" / "twins" / "github_twin.py").read_text().splitlines())
    assert factory_lines < handwritten, (
        f"{fname} {factory_lines} 行 >= github_twin.py {handwritten} 行 —— 工厂没省下代码"
    )
