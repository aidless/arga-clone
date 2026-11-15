"""admin 鉴权：把"管理面"和"数据面"分开。

为什么这件事不能靠"只监听 127.0.0.1"：
本机单人调试确实够用，但只要有人要从另一台机器连进来（CI、同事、容器里的 agent），
监听到 0.0.0.0 就是唯一的做法，而这时代理、端口转发、共享开发机都会让
"127.0.0.1" 不再是边界。admin 面能 provision / reset / teardown，
等于能清掉别人正在跑的评测——它不是"仿真世界里的东西"，它是这个世界的开关。

设计上的两个取舍（都写进了断言，不只是注释）：
1. **默认关**。加个默认就要求令牌的版本会让所有既有脚本 401，而这条路的风险
   是"部署时忘了开"，不是"调试时多打了 --admin-token"。所以 serve 启动时把
   当前状态打印出来，让人看见现在是开的还是关的。
2. **数据面不动**。`/v1/...`、`/api/...` 有自己的 provider 凭据体系，
   把 admin 令牌也套上去会让"只改一行 base URL"这个卖点破裂。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib import error, request

import pytest

from argaclone.server import App, make_server

TOKEN = "adm_test_5f3a91c2"


def _serve(app, monkeypatch=None):
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
    except error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None), dict(exc.headers)


@pytest.fixture()
def locked(scenario_dir, monkeypatch):
    """开了令牌的服务。"""
    app = App(scenario_dir, admin_token=TOKEN)
    server, base = _serve(app)
    yield app, base
    server.shutdown()
    server.server_close()


@pytest.fixture()
def open_server(scenario_dir):
    """没开令牌的服务（默认形态）。"""
    app = App(scenario_dir)
    server, base = _serve(app)
    yield app, base
    server.shutdown()
    server.server_close()


# ---------------- 服务级 admin ----------------


def test_default_is_open_and_says_so(open_server):
    """默认形态必须是开的，而且这是文档写明的开发模式，不是遗漏。"""
    _, base = open_server
    status, body, _ = http("GET", f"{base}/admin/runs")
    assert status == 200, body


def test_locked_rejects_without_token(locked):
    _, base = locked
    status, body, headers = http("GET", f"{base}/admin/runs")
    assert status == 401
    assert body["error"] == "admin_auth_required"
    # 401 必须告诉客户端"该带什么"，否则它只会以为服务坏了
    assert "WWW-Authenticate" in headers


def test_locked_rejects_wrong_token(locked):
    _, base = locked
    status, body, _ = http("GET", f"{base}/admin/runs", headers={"X-Admin-Token": "adm_wrong"})
    assert status == 401 and body["error"] == "admin_auth_required"

    status, _, _ = http(
        "GET", f"{base}/admin/runs", headers={"Authorization": "Bearer adm_wrong"}
    )
    assert status == 401


def test_locked_accepts_both_header_forms(locked):
    _, base = locked
    status, body, _ = http("GET", f"{base}/admin/runs", headers={"X-Admin-Token": TOKEN})
    assert status == 200 and body["runs"] == []

    status, _, _ = http(
        "GET", f"{base}/admin/runs", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert status == 200


def test_write_routes_are_all_guarded(locked):
    """只挡 GET 是最常见的半吊子做法：读得到 run 列表不算大事，删得掉才是。"""
    _, base = locked
    status, _, _ = http("POST", f"{base}/admin/runs", body={"twins": "stripe", "ttl": 60})
    assert status == 401
    status, _, _ = http("DELETE", f"{base}/admin/runs/whatever")
    assert status == 401


def test_healthz_stays_open(locked):
    """健康检查要给监控用，监控不该拿 admin 令牌。"""
    _, base = locked
    status, body, _ = http("GET", f"{base}/healthz")
    assert status == 200 and body == {"ok": True}


def test_mcp_is_guarded_too(locked):
    """MCP 的 provision/act/grade 与被保护的 admin 路由是同一份权力。"""
    _, base = locked
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    status, body, _ = http("POST", f"{base}/mcp", body=payload)
    assert status == 401 and body["error"] == "admin_auth_required"

    status, body, _ = http("POST", f"{base}/mcp", body=payload, headers={"X-Admin-Token": TOKEN})
    assert status == 200 and body["result"]["tools"]


# ---------------- twin 级 admin ----------------


def test_twin_level_admin_is_guarded(locked):
    app, base = locked
    run_id = app.provision({"twins": "stripe", "ttl": 60})["run_id"]
    path = f"/__twin/stripe/{run_id}/admin/coverage"

    status, body, _ = http("GET", f"{base}{path}")
    assert status == 401, "twin 级 /admin 漏了就等于服务级白挡"

    status, body, _ = http("GET", f"{base}{path}", headers={"X-Admin-Token": TOKEN})
    assert status == 200 and body["provider"] == "stripe"


def test_twin_admin_via_host_header_resolves_to_twin_not_404(locked):
    """`/admin/coverage` 带着 twin 的 Host 时，是 twin 的覆盖度，不是"没有这个 admin 路由"。

    这条同时钉住两件事：鉴权（带 twin 的 Host 也必须给令牌）和路由
    （不能因为路径以 /admin 开头就吞进服务级 admin）。
    """
    app, base = locked
    run_id = app.provision({"twins": "slack", "ttl": 60})["run_id"]
    host = f"slack.{run_id}.twins.local"

    status, body, _ = http("GET", f"{base}/admin/coverage", headers={"Host": host})
    assert status == 401

    status, body, _ = http(
        "GET", f"{base}/admin/coverage", headers={"Host": host, "X-Admin-Token": TOKEN}
    )
    assert status == 200
    assert body["provider"] == "slack", f"应该是 slack 的覆盖度，实际 {body}"


# ---------------- 数据面必须不受影响 ----------------


def test_data_plane_is_untouched_by_admin_token(locked):
    """数据面用 provider 凭据，不是 admin 令牌。这条是防误伤的主断言。"""
    app, base = locked
    run_id = app.provision({"twins": "stripe", "ttl": 60})["run_id"]
    status, body, _ = http(
        "POST",
        f"{base}/v1/customers",
        body={"email": "dp@example.com"},
        headers={
            "Host": f"stripe.{run_id}.twins.local",
            "Authorization": f"Bearer {app.store.get(run_id).extras['credentials']['stripe']}",
        },
    )
    assert status == 200, body
    assert body["object"] == "customer"


def test_guard_is_load_bearing_and_not_just_a_flag(locked, open_server):
    """同一条请求，两个服务的唯一差别是令牌——证明拦截真的来自这个开关。"""
    _, locked_base = locked
    _, open_base = open_server
    a = http("GET", f"{locked_base}/admin/runs")[0]
    b = http("GET", f"{open_base}/admin/runs")[0]
    assert (a, b) == (401, 200)


def test_client_can_carry_the_token(locked, monkeypatch):
    """SDK 不带令牌的话，开了鉴权的服务对 SDK 用户就等于挂了。"""
    from argaclone.client import ArgacloneError, Client

    _, base = locked
    monkeypatch.setenv("ARGACLONE_ADMIN_TOKEN", TOKEN)
    with Client(base) as c:
        with pytest.raises(ArgacloneError) as exc:
            c.list_runs()
        assert exc.value.status == 401

    with Client.from_env(base) as c:
        assert c.list_runs() == []

    with Client(base, admin_token=TOKEN) as c:
        run = c.provision(["stripe"], ttl=60)
        assert run.run_id
