"""Salesforce twin：错误体是数组、PATCH 回 204 空 body、SOQL 只认一个子集。

判据纪律同其它 twin：断言必须命中 seed 里的真实字段，而不是只看状态码 ——
stub 也回 200，只判状态码的断言对假数据同样成立。
"""

from __future__ import annotations

import json

import pytest

from argaclone.runner import is_success
from argaclone.server import App
from argaclone.twins.base import Request, Response
from argaclone.twins.salesforce_twin import API, SalesforceTwin

TOKEN = "00Dtw1n000000AAA!AQ4AQtest"


@pytest.fixture
def sf(tmp_path):
    app = App(tmp_path)
    run = app.store.create(["salesforce"], ttl_seconds=600)
    return app, SalesforceTwin(), run


def call(twin, run, method, path, body=None, token=TOKEN, query=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    raw = json.dumps(body).encode() if body is not None else b""
    q = {k: [v] for k, v in (query or {}).items()}
    return twin.dispatch(Request(method, path, q, headers, raw, run, run.run_id))


def test_missing_token_is_rejected(sf):
    _, twin, run = sf
    resp = call(twin, run, "GET", f"{API}/sobjects/Account", token=None)
    assert not is_success("salesforce", resp.status, resp.body)
    # 关键：错误体是**数组**，不是对象。回对象是所有保真缺陷里最便宜的一种假绿。
    assert isinstance(resp.body, list)
    assert resp.body[0]["errorCode"] == "INVALID_SESSION_ID"


def test_token_prefix_is_not_required_but_value_is(sf):
    _, twin, run = sf
    ok = call(twin, run, "GET", f"{API}/sobjects/Account", token="anything")
    assert ok.status == 200
    # 空 Bearer 必须拒（`Authorization: Bearer ` 后面什么都没有）
    empty = call(twin, run, "GET", f"{API}/sobjects/Account", token="")
    assert empty.status == 401


def test_create_returns_id_and_success_envelope(sf):
    _, twin, run = sf
    resp = call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind", "Industry": "Retail"})
    assert resp.status == 201, resp.body
    assert resp.body["success"] is True
    assert resp.body["errors"] == []
    # 15 位、前 3 位是 Account 的 key prefix —— 客户端按前缀判断类型
    assert len(resp.body["id"]) == 15
    assert resp.body["id"].startswith("001")


def test_get_returns_record_with_attributes(sf):
    _, twin, run = sf
    ident = call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind"}).body["id"]
    resp = call(twin, run, "GET", f"{API}/sobjects/Account/{ident}")
    assert resp.status == 200
    # 字段在**顶层**（与 HubSpot 的 properties 壳正相反）
    assert resp.body["Name"] == "Northwind"
    assert resp.body["Id"] == ident
    assert resp.body["attributes"]["type"] == "Account"
    assert resp.body["attributes"]["url"].endswith(f"/sobjects/Account/{ident}")
    assert resp.body["CreatedDate"].endswith("+0000")


def test_get_unknown_id_returns_array_404(sf):
    _, twin, run = sf
    resp = call(twin, run, "GET", f"{API}/sobjects/Account/001000000000000")
    assert resp.status == 404
    assert isinstance(resp.body, list)
    assert resp.body[0]["errorCode"] == "NOT_FOUND"


def test_patch_returns_204_with_empty_body(sf):
    _, twin, run = sf
    ident = call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind"}).body["id"]
    resp = call(twin, run, "PATCH", f"{API}/sobjects/Account/{ident}", {"Industry": "Energy"})
    # 真 Salesforce 回 204 空 body。回对象会让"从响应取 LastModifiedDate"的客户端
    # 在 twin 上跑得通、上线拿到 None —— 那正是 twin 最该防的假绿。
    assert resp.status == 204
    assert resp.body is None
    assert resp.to_bytes() == b""
    after = call(twin, run, "GET", f"{API}/sobjects/Account/{ident}")
    assert after.body["Industry"] == "Energy"
    assert after.body["Name"] == "Northwind"


def test_delete_then_get_is_404(sf):
    _, twin, run = sf
    ident = call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Gone"}).body["id"]
    assert call(twin, run, "DELETE", f"{API}/sobjects/Account/{ident}").status == 204
    assert call(twin, run, "GET", f"{API}/sobjects/Account/{ident}").status == 404


def test_required_field_missing(sf):
    _, twin, run = sf
    resp = call(twin, run, "POST", f"{API}/sobjects/Opportunity", {"StageName": "Prospecting"})
    assert resp.status == 400
    assert resp.body[0]["errorCode"] == "REQUIRED_FIELD_MISSING"
    assert "Name" in resp.body[0]["message"]


def test_unknown_field_is_rejected_but_custom_field_is_allowed(sf):
    _, twin, run = sf
    bad = call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "X", "Nope": 1})
    assert bad.status == 400
    assert bad.body[0]["errorCode"] == "INVALID_FIELD"
    assert "Nope" in bad.body[0]["message"]
    # `__c` 是租户自定义字段，twin 无从枚举 —— 放行而不是报错（拒绝合法写入会教错事实）
    ok = call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Y", "Segment__c": "ent"})
    assert ok.status == 201
    rec = call(twin, run, "GET", f"{API}/sobjects/Account/{ok.body['id']}")
    assert rec.body["Segment__c"] == "ent"


def test_list_envelope_is_totalsize_done_records(sf):
    _, twin, run = sf
    for name in ("Northwind", "Contoso"):
        call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": name})
    resp = call(twin, run, "GET", f"{API}/sobjects/Account")
    assert resp.status == 200
    # 不是 results / values —— 这是第五种信封形状，缺 done 会让 jsforce 崩在 undefined 上
    assert resp.body["totalSize"] == 2
    assert resp.body["done"] is True
    assert [r["Name"] for r in resp.body["records"]] == ["Northwind", "Contoso"]


def test_describe_global_and_describe(sf):
    _, twin, run = sf
    g = call(twin, run, "GET", f"{API}/sobjects")
    names = {o["name"] for o in g.body["sobjects"]}
    assert {"Account", "Contact", "Opportunity", "Lead", "Case"} <= names
    assert g.body["sobjects"][0]["keyPrefix"] in {"001", "003", "006", "00Q", "500"}

    d = call(twin, run, "GET", f"{API}/sobjects/Opportunity/describe")
    assert d.status == 200
    assert "Amount" in {f["name"] for f in d.body["fields"]}


def test_describe_route_is_not_swallowed_by_id_route(sf):
    """`describe` 与「一个 id」都匹配 `[^/]+` —— 注册顺序错了就回 404。"""
    _, twin, run = sf
    resp = call(twin, run, "GET", f"{API}/sobjects/Contact/describe")
    assert resp.status == 200
    assert resp.body["name"] == "Contact"


def test_soql_select_with_where_and_limit(sf):
    _, twin, run = sf
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind", "Industry": "Retail"})
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Contoso", "Industry": "Energy"})
    resp = call(
        twin,
        run,
        "GET",
        f"{API}/query",
        query={"q": "SELECT Id, Name FROM Account WHERE Industry = 'Retail' LIMIT 5"},
    )
    assert resp.status == 200, resp.body
    assert resp.body["totalSize"] == 1
    assert resp.body["records"][0]["Name"] == "Northwind"
    # 投影生效：没要 Industry 就不该出现（真 SOQL 就是这样）
    assert "Industry" not in resp.body["records"][0]
    assert resp.body["records"][0]["Id"]


def test_soql_like_and_and_clauses(sf):
    _, twin, run = sf
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind", "Industry": "Retail"})
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northern Lights", "Industry": "Retail"})
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northern Gas", "Industry": "Energy"})
    q = "SELECT Name FROM Account WHERE Name LIKE 'Northern%' AND Industry = 'Retail'"
    resp = call(twin, run, "GET", f"{API}/query", query={"q": q})
    assert resp.status == 200, resp.body
    assert [r["Name"] for r in resp.body["records"]] == ["Northern Lights"]


def test_soql_unsupported_clause_is_explicit(sf):
    _, twin, run = sf
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind"})
    # ORDER BY 没实现就必须报错。静默忽略会返回"看起来排好了"的顺序，
    # agent 会基于错误顺序取第一条，然后做整套决策。
    resp = call(twin, run, "GET", f"{API}/query", query={"q": "SELECT Id FROM Account ORDER BY Name"})
    assert resp.status == 400
    assert resp.body[0]["errorCode"] == "MALFORMED_QUERY"


def test_soql_or_is_rejected_not_ignored(sf):
    _, twin, run = sf
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "A", "Industry": "Retail"})
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "B", "Industry": "Energy"})
    resp = call(
        twin,
        run,
        "GET",
        f"{API}/query",
        query={"q": "SELECT Id FROM Account WHERE Industry = 'Retail' OR Industry = 'Energy'"},
    )
    assert resp.status == 400
    assert resp.body[0]["errorCode"] == "MALFORMED_QUERY"


def test_soql_unknown_field_and_type(sf):
    _, twin, run = sf
    bad_field = call(twin, run, "GET", f"{API}/query", query={"q": "SELECT Nope FROM Account"})
    assert bad_field.status == 400
    assert bad_field.body[0]["errorCode"] == "INVALID_FIELD"
    bad_type = call(twin, run, "GET", f"{API}/query", query={"q": "SELECT Id FROM Invoice__x"})
    assert bad_type.status == 400
    assert bad_type.body[0]["errorCode"] == "INVALID_TYPE"


def test_version_segment_is_tolerated(sf):
    """客户端常从环境变量读版本号；只实现 v59.0 的 twin 不该让别的版本 404。"""
    _, twin, run = sf
    call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind"})
    resp = call(twin, run, "GET", "/services/data/v60.0/sobjects/Account")
    assert resp.status == 200
    assert resp.body["records"][0]["Name"] == "Northwind"
    versions = call(twin, run, "GET", "/services/data")
    assert [v["version"] for v in versions.body] == ["59.0", "60.0"]


def test_unimplemented_endpoint_is_stubbed_and_marked(sf):
    _, twin, run = sf
    resp = call(twin, run, "GET", f"{API}/sobjects/Account/describe/compactLayouts")
    assert resp.status == 200
    assert resp.headers["X-Twin-Stub"] == "true"
    assert resp.body["_twin_stub"] is True
    assert isinstance(resp.body["data"], list)


def test_seed_admin_roundtrip(sf):
    _, twin, run = sf
    twin.handle_admin(
        Request(
            "POST",
            "/admin/seed",
            {},
            {},
            json.dumps(
                {
                    "Account": [
                        {"Id": "001SEED000000001", "Name": "Seeded Co", "Industry": "Retail"},
                    ]
                }
            ).encode(),
            run,
            run.run_id,
        )
    )
    resp = call(twin, run, "GET", f"{API}/sobjects/Account/001SEED000000001")
    assert resp.status == 200
    assert resp.body["Name"] == "Seeded Co"


def test_coverage_reports_routes(sf):
    _, twin, _ = sf
    cov = twin.coverage().body
    assert cov["unique_endpoints"] >= 12
    assert f"GET {API}/query" in cov["endpoints"]


def test_red_object_error_envelope_would_still_pass_a_status_check(sf, monkeypatch):
    """RED：证明"错误体必须是数组"这条断言是承重的。

    把错误信封临时换成对象（几乎所有 provider 的形状），**状态码断言依然绿**——
    只有 `isinstance(body, list)` 会红。这正是"只判状态码 = 装饰"的定义：
    如果哪天有人把 `_err` 从数组改成对象，这个测试必须当场失败。
    """
    import argaclone.twins.salesforce_twin as sfmod

    _, twin, run = sf
    real = call(twin, run, "GET", f"{API}/sobjects/Account", token=None)
    assert isinstance(real.body, list)
    assert real.body[0]["errorCode"] == "INVALID_SESSION_ID"

    monkeypatch.setattr(
        sfmod, "_err", lambda status, message, code: Response(status, {"message": message, "errorCode": code})
    )
    wrong = call(twin, run, "GET", f"{API}/sobjects/Account", token=None)
    assert wrong.status == 401  # ← 只看状态码的话，错形状照样"通过"
    with pytest.raises(AssertionError):
        assert isinstance(wrong.body, list)


def test_red_patch_body_would_break_the_204_assertion(sf, monkeypatch):
    """RED：证明 PATCH 的 204 断言不是顺手写的。

    工厂默认的 update 回 200 + 实体。把它接回来，`status == 204` 必须红 ——
    否则这条断言就只是复述了实现，而不是在守真实行为。
    """
    _, twin, run = sf

    # 不能用 monkeypatch 换实例属性：路由在 `_build_routes()` 里就把**绑定方法**存进了
    # route 表，事后改实例属性对已注册的 handler 无效（这一点本身值得记住，
    # 换成子类才能改到 __init__ 期间绑定的那个方法）。
    class BodyReturningTwin(type(twin)):
        def update_record(self, req, params):
            return Response(200, {"Id": params["id"], "Name": "Northwind"})

    body_twin = BodyReturningTwin()
    ident = call(twin, run, "POST", f"{API}/sobjects/Account", {"Name": "Northwind"}).body["id"]
    resp = call(body_twin, run, "PATCH", f"{API}/sobjects/Account/{ident}", {"Name": "Northwind"})
    with pytest.raises(AssertionError):
        assert resp.status == 204
