"""Drive twin 测试：重点在"误公开"与"以为拿到了内容"。

这两件事都不是"功能有没有实现"，而是"客户端会不会以为自己成功了"：
  · files.get 不返回内容，必须 alt=media —— 少了这条，agent 会读到 0 字节以为文件是空的
  · q 里不认识子句必须报错 —— 少了这条，agent 会以为自己筛出了 3 个文件，其实是全部
  · DELETE 是进回收站 —— 少了这条，审计会以为文件真没了
"""

from __future__ import annotations

import json

import pytest

from argaclone.store import Store
from argaclone.twins.base import Request
from argaclone.twins.drive_twin import DriveTwin

TOKEN = "ya29.drive-token"


@pytest.fixture()
def ctx():
    store = Store()
    run = store.create(["drive"], 600)
    run.state["drive"] = DriveTwin().initial_state()
    return run, store


def call(twin, ctx, method, path, body=None, query=None, token=TOKEN):
    run, store = ctx
    headers = {"authorization": f"Bearer {token}"} if token else {}
    req = Request(method=method, path=path, query=query or {}, headers=headers,
                  body=b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode()),
                  run=run, run_id=run.run_id, store=store)
    return twin.dispatch(req)


def seed(ctx, files):
    twin = DriveTwin()
    call(twin, ctx, "POST", "/admin/seed", {"files": files})
    return twin


def test_upload_then_get_does_not_return_content(ctx):
    twin = DriveTwin()
    up = call(twin, ctx, "POST", "/upload/drive/v3/files",
              b"quarterly numbers", query={"name": ["Q4.txt"], "mimeType": ["text/plain"]})
    fid = up.body["id"]
    assert up.body["size"] == str(len("quarterly numbers"))

    meta = call(twin, ctx, "GET", f"/drive/v3/files/{fid}")
    assert meta.status == 200
    assert "content" not in meta.body           # metadata 里没有正文，这是真 Drive 行为
    media = call(twin, ctx, "GET", f"/drive/v3/files/{fid}", query={"alt": ["media"]})
    assert media.body == b"quarterly numbers"


def test_unsupported_q_raises_instead_of_returning_everything(ctx):
    twin = seed(ctx, [{"name": "a.txt"}, {"name": "b.txt"}])
    bad = call(twin, ctx, "GET", "/drive/v3/files",
               query={"q": ["name = 'a.txt'"]})
    assert bad.status == 400
    assert "unsupported q clause" in bad.body["error"]["message"]


def test_supported_q_filters(ctx):
    twin = seed(ctx, [
        {"name": "Q4 report.pdf", "mimeType": "application/pdf"},
        {"name": "notes.txt"},
    ])
    by_name = call(twin, ctx, "GET", "/drive/v3/files", query={"q": ["name contains 'q4'"]})
    assert [f["name"] for f in by_name.body["files"]] == ["Q4 report.pdf"]

    by_mime = call(twin, ctx, "GET", "/drive/v3/files",
                   query={"q": ["mimeType = 'application/pdf'"]})
    assert len(by_mime.body["files"]) == 1

    both = call(twin, ctx, "GET", "/drive/v3/files",
                query={"q": ["name contains 'report' and mimeType = 'application/pdf'"]})
    assert len(both.body["files"]) == 1


def test_delete_is_trash_not_removal(ctx):
    twin = seed(ctx, [{"name": "keep.txt"}, {"name": "gone.txt"}])
    fid = [f for f in call(twin, ctx, "GET", "/drive/v3/files").body["files"]
           if f["name"] == "gone.txt"][0]["id"]
    assert call(twin, ctx, "DELETE", f"/drive/v3/files/{fid}").status == 204

    visible = call(twin, ctx, "GET", "/drive/v3/files")
    assert [f["name"] for f in visible.body["files"]] == ["keep.txt"]
    # 但文件还在，能按 includeTrashed 找回来
    all_files = call(twin, ctx, "GET", "/drive/v3/files", query={"includeTrashed": ["true"]})
    assert {f["name"] for f in all_files.body["files"]} == {"keep.txt", "gone.txt"}


def test_anyone_permission_is_flagged_as_public(ctx):
    twin = seed(ctx, [{"name": "plan.pdf"}])
    fid = call(twin, ctx, "GET", "/drive/v3/files").body["files"][0]["id"]
    perm = call(twin, ctx, "POST", f"/drive/v3/files/{fid}/permissions",
                {"type": "anyone", "role": "reader"})
    assert perm.status == 200
    assert perm.body["_twin_public_link"] is True
    again = call(twin, ctx, "GET", f"/drive/v3/files/{fid}/permissions")
    assert any(p["type"] == "anyone" for p in again.body["permissions"])


def test_user_permission_requires_email(ctx):
    twin = seed(ctx, [{"name": "plan.pdf"}])
    fid = call(twin, ctx, "GET", "/drive/v3/files").body["files"][0]["id"]
    bad = call(twin, ctx, "POST", f"/drive/v3/files/{fid}/permissions",
               {"type": "user", "role": "reader"})
    assert bad.status == 400
    ok = call(twin, ctx, "POST", f"/drive/v3/files/{fid}/permissions",
              {"type": "user", "role": "reader", "emailAddress": "morgan@retail.example"})
    assert ok.body["emailAddress"] == "morgan@retail.example"


def test_write_needs_write_scope(ctx):
    twin = seed(ctx, [{"name": "x.txt"}])
    run, _ = ctx
    run.state["drive"]["granted_scopes"] = ["https://www.googleapis.com/auth/drive.readonly"]
    fid = call(twin, ctx, "GET", "/drive/v3/files").body["files"][0]["id"]
    assert call(twin, ctx, "GET", f"/drive/v3/files/{fid}").status == 200
    denied = call(twin, ctx, "PATCH", f"/drive/v3/files/{fid}", {"name": "y.txt"})
    assert denied.status == 403


def test_parent_scope_grants_children(ctx):
    twin = seed(ctx, [{"name": "x.txt"}])
    run, _ = ctx
    run.state["drive"]["granted_scopes"] = ["https://www.googleapis.com/auth/drive"]
    fid = call(twin, ctx, "GET", "/drive/v3/files").body["files"][0]["id"]
    assert call(twin, ctx, "PATCH", f"/drive/v3/files/{fid}", {"name": "y.txt"}).status == 200


def test_copy_duplicates_content(ctx):
    twin = seed(ctx, [{"name": "orig.txt", "content": "hello"}])
    fid = call(twin, ctx, "GET", "/drive/v3/files").body["files"][0]["id"]
    copied = call(twin, ctx, "POST", f"/drive/v3/files/{fid}/copy", {})
    assert copied.body["name"] == "Copy of orig.txt"
    assert call(twin, ctx, "GET", f"/drive/v3/files/{copied.body['id']}",
                query={"alt": ["media"]}).body == b"hello"


def test_missing_token_is_401(ctx):
    twin = DriveTwin()
    assert call(twin, ctx, "GET", "/drive/v3/files", token=None).status == 401
    assert call(twin, ctx, "GET", "/drive/v3/files", token="ya29.").status == 401
