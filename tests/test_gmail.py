"""Gmail twin 测试。

保真的关键点不是"能不能发信"，而是**它是否像真 Gmail 那样难以被骗**：
列表不返回正文、raw 是去 padding 的 base64url、scope 不够时是 403 而不是 404。
这些地方放松一点，twin 就会比真实世界更宽容，而宽容的 twin 训不出能上线的 agent。
"""

from __future__ import annotations

import base64

import pytest

from argaclone.store import Store
from argaclone.twins.gmail_twin import GmailTwin, parse_mime


def b64raw(to: str, subject: str, body: str) -> str:
    mime = f"To: {to}\r\nFrom: ops@acme.example\r\nSubject: {subject}\r\n\r\n{body}"
    return base64.urlsafe_b64encode(mime.encode()).decode().rstrip("=")


@pytest.fixture()
def ctx():
    store = Store()
    r = store.create(["gmail"], 600)
    r.state["gmail"] = GmailTwin().initial_state()
    return r, store


def call(twin: GmailTwin, ctx, method: str, path: str, body=None, token="ya29.demo-token", query=None):
    run, store = ctx if isinstance(ctx, tuple) else (ctx, None)
    from argaclone.twins.base import Request

    headers = {"authorization": f"Bearer {token}"} if token else {}
    req = Request(
        method=method, path=path, query=query or {}, headers=headers,
        body=b"" if body is None else __import__("json").dumps(body).encode(),
        run=run, run_id=run.run_id, store=store,
    )
    return twin.dispatch(req)


def test_base64url_without_padding_is_decoded():
    raw = b64raw("a@example.com", "Hi", "body text")
    assert "=" not in raw                       # 真 Gmail 的 raw 不带 padding
    assert parse_mime(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )["subject"] == "Hi"


def test_parse_mime_handles_lf_only():
    assert parse_mime(b"To: a@b.c\nSubject: S\n\nbody")["body"] == "body"


def test_send_then_list_returns_ids_only(ctx):
    twin = GmailTwin()
    sent = call(twin, ctx, "POST", "/gmail/v1/users/me/messages/send",
                {"raw": b64raw("morgan@retail.example", "Merged", "done")})
    assert sent.status == 200
    assert sent.body["labelIds"] == ["SENT"]

    listed = call(twin, ctx, "GET", "/gmail/v1/users/me/messages")
    assert listed.status == 200
    assert len(listed.body["messages"]) == 1
    # 列表只回 id + threadId —— 真 Gmail 就是这样，客户端必须再 get 一次
    assert set(listed.body["messages"][0]) == {"id", "threadId"}


def test_get_message_full_has_payload_and_raw(ctx):
    twin = GmailTwin()
    mid = call(twin, ctx, "POST", "/gmail/v1/users/me/messages/send",
               {"raw": b64raw("a@b.c", "Subject line", "hello")}).body["id"]
    got = call(twin, ctx, "GET", f"/gmail/v1/users/me/messages/{mid}")
    assert got.body["payload"]["headers"][0] == {"name": "Subject", "value": "Subject line"}
    assert got.body["raw"]
    minimal = call(twin, ctx, "GET", f"/gmail/v1/users/me/messages/{mid}", query={"format": ["minimal"]})
    assert "payload" not in minimal.body


def test_send_without_scope_is_403(ctx):
    twin = GmailTwin()
    ctx[0].state["gmail"]["granted_scopes"] = ["https://www.googleapis.com/auth/gmail.readonly"]
    out = call(twin, ctx, "POST", "/gmail/v1/users/me/messages/send",
               {"raw": b64raw("a@b.c", "S", "b")})
    assert out.status == 403
    assert out.body["error"]["errors"][0]["reason"] == "insufficientPermissions"


def test_parent_scope_grants_send(ctx):
    """mail.google.com 是父 scope，授了它 send 就该能用。"""
    twin = GmailTwin()
    ctx[0].state["gmail"]["granted_scopes"] = ["https://mail.google.com/"]
    out = call(twin, ctx, "POST", "/gmail/v1/users/me/messages/send",
               {"raw": b64raw("a@b.c", "S", "b")})
    assert out.status == 200


def test_bad_token_shapes_rejected(ctx):
    twin = GmailTwin()
    assert call(twin, ctx, "GET", "/gmail/v1/users/me/profile", token=None).status == 401
    assert call(twin, ctx, "GET", "/gmail/v1/users/me/profile", token="ghp_wrong").status == 401


def test_unsupported_q_is_400_not_empty(ctx):
    """不支持的查询必须报错。

    返回空列表会让 agent 得到"邮箱里没有这封信"的错误事实 ——
    这比报错危险得多，因为它会基于错误事实继续行动。
    """
    twin = GmailTwin()
    out = call(twin, ctx, "GET", "/gmail/v1/users/me/messages", query={"q": ["newer_than:2d"]})
    assert out.status == 400
    assert "unsupported q" in out.body["error"]["message"]


def test_supported_q_filters(ctx):
    twin = GmailTwin()
    call(twin, ctx, "POST", "/gmail/v1/users/me/messages/send", {"raw": b64raw("a@b.c", "S1", "x")})
    listed = call(twin, ctx, "GET", "/gmail/v1/users/me/messages", query={"q": ["label:SENT"]})
    assert len(listed.body["messages"]) == 1
    empty = call(twin, ctx, "GET", "/gmail/v1/users/me/messages", query={"q": ["label:INBOX"]})
    assert empty.body["messages"] == []


def test_modify_labels_and_unknown_label_rejected(ctx):
    twin = GmailTwin()
    mid = call(twin, ctx, "POST", "/gmail/v1/users/me/messages/send",
               {"raw": b64raw("a@b.c", "S", "b")}).body["id"]
    ok = call(twin, ctx, "POST", f"/gmail/v1/users/me/messages/{mid}/modify",
              {"addLabelIds": ["STARRED"], "removeLabelIds": ["SENT"]})
    assert "STARRED" in ok.body["labelIds"] and "SENT" not in ok.body["labelIds"]
    bad = call(twin, ctx, "POST", f"/gmail/v1/users/me/messages/{mid}/modify",
               {"addLabelIds": ["NOSUCH"]})
    assert bad.status == 400


def test_trash_moves_out_of_inbox(ctx):
    twin = GmailTwin()
    mid = call(twin, ctx, "POST", "/gmail/v1/users/me/messages/send",
               {"raw": b64raw("a@b.c", "S", "b")}).body["id"]
    out = call(twin, ctx, "POST", f"/gmail/v1/users/me/messages/{mid}/trash")
    assert "TRASH" in out.body["labelIds"]


def test_draft_is_not_sent(ctx):
    """草稿不能带 SENT —— 否则"发了吗"这个判据在草稿上会被判成已发。"""
    twin = GmailTwin()
    out = call(twin, ctx, "POST", "/gmail/v1/users/me/drafts",
               {"message": {"raw": b64raw("a@b.c", "Draft", "d")}})
    assert out.status == 200
    assert out.body["message"]["labelIds"] == ["DRAFT"]
    listed = call(twin, ctx, "GET", "/gmail/v1/users/me/messages", query={"q": ["label:SENT"]})
    assert listed.body["messages"] == []


def test_unknown_message_404(ctx):
    twin = GmailTwin()
    out = call(twin, ctx, "GET", "/gmail/v1/users/me/messages/msg_9999")
    assert out.status == 404
    assert out.body["error"]["status"] == "NOT_FOUND"


def test_unimplemented_endpoint_is_stubbed_with_marker(ctx):
    twin = GmailTwin()
    out = call(twin, ctx, "GET", "/gmail/v1/users/me/history")
    assert out.headers.get("X-Twin-Stub") == "true"
    assert out.body["_twin_stub"] is True


def test_seed_via_admin_creates_readable_messages(ctx):
    twin = GmailTwin()
    call(twin, ctx, "POST", "/admin/seed", {"messages": [
        {"to": "ops@acme.example", "from": "morgan@retail.example",
         "subject": "duplicate charge", "body": "We were billed twice", "labels": ["INBOX", "UNREAD"]},
    ]})
    listed = call(twin, ctx, "GET", "/gmail/v1/users/me/messages", query={"q": ["is:unread"]})
    assert len(listed.body["messages"]) == 1
