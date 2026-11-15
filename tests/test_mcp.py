"""MCP 端点测试。

重点不是"JSON-RPC 格式对不对"，而是**agent 能不能只靠 MCP 走完一整道题**：
provision → act → grade。协议测试只测形状的话，端点可以全绿而实际不可用
（例如 act 的返回里没有 trace，agent 就永远不知道上一步成没成）。
"""

from __future__ import annotations

import json

import pytest

from argaclone.mcp_server import McpEndpoint, PROTOCOL_VERSION, tool_specs
from argaclone.server import App, make_server


@pytest.fixture()
def endpoint(tmp_path, scenario_dir):
    return McpEndpoint(App(str(scenario_dir)))


def call(endpoint: McpEndpoint, name: str, arguments: dict) -> dict:
    out = endpoint.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return out["result"]


def text_of(result: dict):
    return result["content"][0]["text"]


def test_initialize_reports_tools_capability(endpoint):
    out = endpoint.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert out["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in out["result"]["capabilities"]
    assert out["result"]["serverInfo"]["name"] == "argaclone"


def test_tools_list_has_schemas_not_just_names():
    tools = {t["name"]: t for t in tool_specs()}
    assert set(tools) == {"list_twins", "provision", "act", "grade", "stub_hits", "coverage"}
    for name, t in tools.items():
        assert isinstance(t["inputSchema"], dict), name
        assert t["inputSchema"].get("type") == "object", name
        assert t.get("description"), name


def test_notification_returns_no_body(endpoint):
    # 没有 id 的消息是通知，协议要求不回 body
    assert endpoint.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_jsonrpc_error(endpoint):
    out = endpoint.handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
    assert out["error"]["code"] == -32601
    assert out["id"] == 7


def test_unknown_tool_is_tool_error_not_protocol_error(endpoint):
    res = call(endpoint, "no_such_tool", {})
    assert res["isError"] is True
    assert "unknown tool" in text_of(res)


def test_full_loop_provision_act_grade(endpoint):
    """整条链：MCP 上能把一道题从起环境跑到判分。"""
    prov = json.loads(text_of(call(endpoint, "provision", {
        "twins": "stripe,slack", "ttl_seconds": 300,
        "scenario_id": "ecom-duplicate-billing",
    })))
    run_id = prov["run_id"]
    assert prov["credentials"]["stripe"] == f"sk_test_twin_{run_id}"
    assert any("stripe" in u for u in prov["endpoints"])

    empty = call(endpoint, "grade", {"run_id": run_id})
    assert json.loads(text_of(empty))["verdict"] == "FAIL"

    acted = call(endpoint, "act", {"run_id": run_id, "actions": [
        {"provider": "stripe", "method": "POST",
         "path": "/v1/customers/cus_DUP_EXTRA",
         "body": {"metadata": {"merged_into": "cus_DUP_KEEP"}}},
        {"provider": "slack", "method": "POST",
         "path": "/api/chat.postMessage",
         "body": {"channel": "C_BILLING_OPS", "text": "merged the duplicate billing profile"}},
    ]})
    trace = json.loads(text_of(acted))
    assert trace["count"] == 2
    assert len(trace["writes"]) == 2

    graded = json.loads(text_of(call(endpoint, "grade", {"run_id": run_id})))
    assert graded["verdict"] == "PASS", graded


def test_stub_hits_tool_counts(endpoint):
    prov = json.loads(text_of(call(endpoint, "provision", {"twins": "stripe", "ttl_seconds": 300})))
    run_id = prov["run_id"]
    call(endpoint, "act", {"run_id": run_id, "actions": [
        {"provider": "stripe", "method": "GET", "path": "/v1/terminal/readers"},
    ]})
    hits = json.loads(text_of(call(endpoint, "stub_hits", {"run_id": run_id})))
    assert hits["total_hits"] >= 1


def test_coverage_tool_lists_endpoints(endpoint):
    cov = json.loads(text_of(call(endpoint, "coverage", {"provider": "jira"})))
    assert cov["provider"] == "jira"
    assert cov["unique_endpoints"] > 0


def test_bad_jsonrpc_version_is_invalid_request(endpoint):
    out = endpoint.handle({"jsonrpc": "1.0", "id": 1, "method": "tools/list"})
    assert out["error"]["code"] == -32600


def test_http_mcp_endpoint_roundtrip(scenario_dir):
    """HTTP 层也要通：真实客户端是打 POST /mcp 的。"""
    import threading
    from urllib.request import Request, urlopen

    app = App(str(scenario_dir))
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        req = Request(
            f"http://{host}:{port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        assert len(body["result"]["tools"]) == len(tool_specs())
    finally:
        server.shutdown()
        server.server_close()


def test_http_mcp_rejects_get(scenario_dir):
    import threading
    from urllib.error import HTTPError
    from urllib.request import urlopen

    app = App(str(scenario_dir))
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        with pytest.raises(HTTPError) as exc:
            urlopen(f"http://{host}:{port}/mcp", timeout=10)
        assert exc.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
