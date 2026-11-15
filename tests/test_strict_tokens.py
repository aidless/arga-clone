"""严格 run-scoped 凭据：让"孪生校验了凭据"这句话变成真的。

默认模式下 twin 只校验 token 的**形状**（前缀、非空）。这是有意的取舍：
手工调试与单元测试里随手编一个 token 更方便。但它意味着
`Authorization: Bearer definitely-not-valid` 在 jira/linear/notion 上能读全部数据 ——
"我配了鉴权"是假话。`strict_tokens=True` 把这句话变真：值必须逐字等于 provision 下发的那一个。
"""

from __future__ import annotations

import pytest

from argaclone.server import App

JIRA_MYSELF = "/rest/api/3/myself"


def _call(app, run_id, token, provider="jira", path=JIRA_MYSELF):
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    return app.gateway.handle("GET", f"{provider}.{run_id}.twins.local", path, headers, b"")


@pytest.fixture()
def rig(tmp_path):
    app = App(tmp_path)
    loose = app.provision({"twins": "jira", "ttl_seconds": 300})
    strict = app.provision({"twins": "jira", "ttl_seconds": 300, "strict_tokens": True})
    return app, loose, strict


def test_default_mode_accepts_shape_valid_foreign_token(rig):
    """默认模式的已知局限，写成断言而不是写成免责声明。

    这条 GREEN 是在记录"我们现在确实不校验值"。哪天它变红，说明默认行为收紧了，
    需要同步 README 里那条局限 —— 不是坏事，但必须有人知道。
    """
    app, loose, _ = rig
    resp = _call(app, loose["run_id"], "definitely-not-valid")
    assert resp.status == 200
    assert resp.body["accountId"]


def test_strict_mode_rejects_shape_valid_foreign_token(rig):
    app, _, strict = rig
    resp = _call(app, strict["run_id"], "definitely-not-valid")
    assert resp.status == 401
    assert resp.body["error"] == "invalid_credential"
    assert resp.body["provider"] == "jira"


def test_strict_mode_accepts_the_issued_credential(rig):
    app, _, strict = rig
    token = strict["credentials"]["jira"]
    resp = _call(app, strict["run_id"], token)
    assert resp.status == 200
    assert resp.body["accountId"]


def test_strict_mode_leaves_missing_token_to_the_provider(rig):
    """没带 token 时不用网关的统一错误体，让 provider 自己回 —— 401 的形状也是保真的一部分。"""
    app, _, strict = rig
    resp = _call(app, strict["run_id"], None)
    assert resp.status == 401
    assert "errorMessages" in resp.body, "jira 自己的错误信封不能被网关覆盖"
    assert "error" not in resp.body


def test_run_view_reports_the_mode(rig):
    app, loose, strict = rig
    assert app.get_run(loose["run_id"])["strict_tokens"] is False
    assert app.get_run(strict["run_id"])["strict_tokens"] is True


def test_red_flipping_the_flag_lets_the_foreign_token_back_in(rig):
    """RED：证明拦住它的是这个开关，不是 token 形状。

    把 strict 关掉，同一个假 token 必须又能读到数据。如果它仍然 401，
    说明拦截来自别处（比如形状校验），这条能力就没被验证到。
    """
    app, _, strict = rig
    run_id = strict["run_id"]
    assert _call(app, run_id, "definitely-not-valid").status == 401

    app.store.get(run_id).extras["strict_tokens"] = False
    assert _call(app, run_id, "definitely-not-valid").status == 200


def test_strict_mode_shares_one_google_token_across_three_apis(tmp_path):
    """Google 三件套共用一个 token（真 Google 也这样），严格模式下不该互斥。"""
    app = App(tmp_path)
    view = app.provision({"twins": "gmail,drive,calendar", "ttl_seconds": 300, "strict_tokens": True})
    token = view["credentials"]["gmail"]

    for provider, path in (
        ("gmail", "/gmail/v1/users/me/profile"),
        ("drive", "/drive/v3/files"),
        ("calendar", "/calendar/v3/users/me/calendarList"),
    ):
        resp = app.gateway.handle(
            "GET", f"{provider}.{view['run_id']}.twins.local", path,
            {"authorization": f"Bearer {token}"}, b"",
        )
        assert resp.status == 200, (provider, resp.body)
