"""HubSpot twin：证明工厂能覆盖"对象是壳、数据在 properties 里"这一类 SaaS。

判据不是"状态码 200"，而是**返回里含 seed 进去的真实字段** ——
stub 也回 200，只判状态码的测试对假数据同样成立，那是装饰。
"""

from __future__ import annotations

import json

import pytest

from argaclone.runner import is_success
from argaclone.server import App
from argaclone.twins.base import Request
from argaclone.twins.hubspot_twin import HubSpotTwin

TOKEN = "pat-na1-twin-0000"


@pytest.fixture
def hs(tmp_path):
    app = App(tmp_path)
    run = app.store.create(["hubspot"], ttl_seconds=600)
    twin = HubSpotTwin()
    return app, twin, run


def call(twin, run, method, path, body=None, token=TOKEN, query=None):
    # Request.body 是 bytes（HTTP 通道把原始报文塞进来），这里显式编码，
    # 别让 dict 直接流进去 —— 那会让 Request.json() 抛 TypeError，看起来像产品 bug。
    headers = {"authorization": f"Bearer {token}"} if token else {}
    raw = json.dumps(body).encode() if body is not None else b""
    q = {k: [v] for k, v in (query or {}).items()}
    req = Request(method, path, q, headers, raw, run, run.run_id)
    return twin.dispatch(req)


def test_missing_token_is_rejected(hs):
    _, twin, run = hs
    resp = call(twin, run, "GET", "/crm/v3/objects/contacts", token=None)
    assert not is_success("hubspot", resp.status, resp.body)
    assert resp.body["category"] == "INVALID_AUTHENTICATION"


def test_create_then_list_wraps_properties(hs):
    _, twin, run = hs
    created = call(twin, run, "POST", "/crm/v3/objects/contacts",
                   {"properties": {"email": "dana@acme.test", "firstname": "Dana"}})
    assert created.status == 201, created.body
    # 关键断言：字段在 properties 里，不在顶层
    assert created.body["properties"]["email"] == "dana@acme.test"
    assert "email" not in created.body

    listed = call(twin, run, "GET", "/crm/v3/objects/contacts")
    assert listed.status == 200
    assert listed.body["results"][0]["properties"]["email"] == "dana@acme.test"


def test_get_by_email_works_for_contacts(hs):
    _, twin, run = hs
    call(twin, run, "POST", "/crm/v3/objects/contacts",
         {"properties": {"email": "sam@acme.test", "lastname": "Lee"}})
    got = call(twin, run, "GET", "/crm/v3/objects/contacts/sam@acme.test")
    assert got.status == 200
    assert got.body["properties"]["lastname"] == "Lee"


def test_unknown_object_returns_object_not_found(hs):
    _, twin, run = hs
    resp = call(twin, run, "GET", "/crm/v3/objects/contacts/999")
    assert resp.status == 404
    assert resp.body["category"] == "OBJECT_NOT_FOUND"


def test_properties_projection_only_returns_requested(hs):
    _, twin, run = hs
    call(twin, run, "POST", "/crm/v3/objects/contacts",
         {"properties": {"email": "a@b.c", "firstname": "A", "lastname": "B"}})
    got = call(twin, run, "GET", "/crm/v3/objects/contacts/1", query={"properties": "email"})
    assert set(got.body["properties"]) == {"email"}


def test_paging_uses_after_cursor_not_next_cursor(hs):
    """HubSpot 的分页游标是 `paging.next.after`，没有 has_more。

    这条断言是工厂长出第四种分页形状的原因：用 cursor 那套回它，
    客户端永远停在第一页，而且不报错。
    """
    _, twin, run = hs
    for i in range(12):
        call(twin, run, "POST", "/crm/v3/objects/contacts",
             {"properties": {"email": f"u{i}@acme.test"}})
    first = call(twin, run, "GET", "/crm/v3/objects/contacts")
    assert len(first.body["results"]) == 10
    assert "has_more" not in first.body
    assert "next_cursor" not in first.body
    after = first.body["paging"]["next"]["after"]
    assert after == "10"

    second = call(twin, run, "GET", "/crm/v3/objects/contacts", query={"after": after})
    assert len(second.body["results"]) == 2
    assert "paging" not in second.body


def test_search_filters_by_property(hs):
    _, twin, run = hs
    call(twin, run, "POST", "/crm/v3/objects/companies", {"properties": {"name": "Acme Corp"}})
    call(twin, run, "POST", "/crm/v3/objects/companies", {"properties": {"name": "Globex"}})
    resp = call(twin, run, "POST", "/crm/v3/objects/companies/search",
                {"filterGroups": [{"filters": [
                    {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": "acme"}]}]})
    assert resp.status == 200
    assert resp.body["total"] == 1
    assert resp.body["results"][0]["properties"]["name"] == "Acme Corp"


def test_unsupported_search_operator_is_explicit_error(hs):
    """不认识的算子必须 400。静默回空结果会被读成"这家公司不存在"。"""
    _, twin, run = hs
    resp = call(twin, run, "POST", "/crm/v3/objects/companies/search",
                {"filterGroups": [{"filters": [
                    {"propertyName": "name", "operator": "BETWEEN", "value": "x"}]}]})
    assert resp.status == 400
    assert "Unsupported search operator" in resp.body["message"]


def test_deal_without_name_is_validation_error(hs):
    _, twin, run = hs
    resp = call(twin, run, "POST", "/crm/v3/objects/deals", {"properties": {"amount": "100"}})
    assert resp.status == 400
    assert resp.body["category"] == "VALIDATION_ERROR"


def test_patch_updates_and_keeps_other_fields(hs):
    _, twin, run = hs
    call(twin, run, "POST", "/crm/v3/objects/contacts",
         {"properties": {"email": "k@acme.test", "firstname": "K"}})
    resp = call(twin, run, "PATCH", "/crm/v3/objects/contacts/1",
                {"properties": {"firstname": "Kay"}})
    assert resp.body["properties"]["firstname"] == "Kay"
    assert resp.body["properties"]["email"] == "k@acme.test"


def test_delete_then_get_is_404(hs):
    _, twin, run = hs
    call(twin, run, "POST", "/crm/v3/objects/contacts", {"properties": {"email": "g@acme.test"}})
    assert call(twin, run, "DELETE", "/crm/v3/objects/contacts/1").status == 204
    assert call(twin, run, "GET", "/crm/v3/objects/contacts/1").status == 404


def test_stub_marking_on_unimplemented_endpoint(hs):
    _, twin, run = hs
    resp = call(twin, run, "GET", "/crm/v3/associations/contacts/companies")
    assert resp.headers.get("X-Twin-Stub") == "true"
    assert resp.body.get("_twin_stub") is True


def test_seed_admin_route(hs):
    _, twin, run = hs
    resp = call(twin, run, "POST", "/admin/seed",
                {"contacts": [{"id": "77", "email": "seeded@acme.test", "firstname": "S"}]})
    assert resp.status == 200
    got = call(twin, run, "GET", "/crm/v3/objects/contacts/77")
    assert got.body["properties"]["email"] == "seeded@acme.test"
