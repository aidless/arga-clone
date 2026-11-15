#!/usr/bin/env bash
# 一键验证 arga-clone 的每一层。任何一层红了就停。
#
# 为什么要有这个脚本：这套东西的失败模式几乎全是"静默通过"——
# 判据写错了返回 PASS、twin 缺端点但 agent 照样"跑完"、
# 场景 seed 顺序错了 reset 之后还在。
# 所以验证必须分层跑，且每层都要有能变红的证据。

set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/12 场景定义合法性 =="
python3 scenarios/build_suite.py --check

echo "== 2/12 单元 + 端到端测试（含跨 twin 契约层）=="
python3 -m pytest -q

echo "== 3/12 三态判分演示（PASS / FAIL / UNSAFE + 重放一致）=="
python3 demo/run_demo.py

echo "== 4/12 HTTP 面冒烟 =="
python3 - <<'PY'
import json
import threading
import urllib.request

from argaclone.server import App, make_server

app = App("scenarios")
srv = make_server(app, "127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


assert call("/healthz")["ok"] is True
view = call("/admin/runs", {"twins": "stripe,slack", "ttl": 300,
                            "scenario_id": "ecom-duplicate-billing"})
run_id = view["run_id"]
key = {"Authorization": "Bearer " + view["credentials"]["stripe"]}

# 只替换 base URL 就能打过去——这是整套设计的对外承诺，必须真验一次
req = urllib.request.Request(
    base + "/v1/customers", headers={**key, "Host": f"stripe.{run_id}.twins.local"})
with urllib.request.urlopen(req) as r:
    body = json.loads(r.read())
assert body["object"] == "list" and len(body["data"]) == 2

# 未实现端点必须显眼地假
req = urllib.request.Request(
    base + "/v1/test_clocks", headers={**key, "Host": f"stripe.{run_id}.twins.local"})
with urllib.request.urlopen(req) as r:
    stub = json.loads(r.read())
    assert r.headers.get("X-Twin-Stub") == "true"
assert stub["_twin_stub"] is True and "_twin_warning" in stub

print("   healthz / provision / host routing / stub marker 全通")
srv.shutdown()
PY

echo "== 5/12 可解性（79/79 gold 解法必须 PASS）=="
python3 scripts/verify_solvable.py

echo "== 6/12 覆盖度自述（每 twin 必须能报实现清单）=="
python3 - <<'PY'
from argaclone.server import build_registry

reg = build_registry()
assert reg.names(), "没有任何 twin 被注册"
for name in reg.names():
    body = reg.get(name).coverage().body
    assert body["provider"] == name
    assert body["unique_endpoints"] > 0, f"{name} 报不出任何已实现端点"
    assert body["endpoints"], f"{name} 的端点清单是空的"
print("   " + " / ".join(f"{n}:{reg.get(n).coverage().body['unique_endpoints']}端点"
                         for n in reg.names()))
PY

echo "== 7/12 MCP 端点（agent 只能靠工具调用的那条通道）=="
python3 - <<'PY'
import json

from argaclone.mcp_server import McpEndpoint
from argaclone.server import App

app = App("scenarios")
ep = McpEndpoint(app)
init = ep.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
assert init["result"]["capabilities"]["tools"]
tools = ep.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
names = {t["name"] for t in tools}
assert names == {"list_twins", "provision", "act", "grade", "stub_hits", "coverage"}, names

provisioned = ep.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "provision",
                                    "arguments": {"twins": "stripe,slack", "ttl_seconds": 60,
                                                  "scenario_id": "ecom-duplicate-billing"}}})
run = json.loads(provisioned["result"]["content"][0]["text"])
graded = ep.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "grade", "arguments": {"run_id": run["run_id"]}}})
graded_body = json.loads(graded["result"]["content"][0]["text"])
verdict = graded_body["verdict"]
# 无 scenario 的 run 没有判据，grade 只能回 ERROR。这里必须带 scenario，
# 否则这条断言测的是"有没有判据"而不是"空动作算不算失败"。
assert verdict == "FAIL", f"空动作在 MCP 通道上也必须是 FAIL，实际 {verdict}"
print(f"   {len(tools)} 个工具；provision→grade 链路判为 {verdict}")
PY

echo "== 8/12 SDK + PR 钩子（真实 HTTP 闭环）=="
python3 - <<'PY'
import threading

from argaclone import client as c
from argaclone.server import App, make_server

app = App("scenarios")
srv = make_server(app, "127.0.0.1", 0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
host, port = srv.server_address[:2]
cl = c.Client(f"http://{host}:{port}")
run = cl.provision(["stripe"], ttl=60, scenario="ecom-duplicate-billing")
assert run.grade()["verdict"] == "FAIL"
run.act([{"provider": "stripe", "method": "POST", "path": "/v1/customers/cus_DUP_EXTRA",
          "body": {"metadata": {"merged_into": "cus_DUP_KEEP"}}}])
run.teardown()
srv.shutdown()
print("   SDK provision/act/grade/teardown 全通")
PY
python3 demo/pr_hook.py | tail -6

echo "== 9/12 边界、凭据与第三态：声明式 stub / 严格令牌 / 不存在的端点 =="
python3 - <<'PYEOF'
from argaclone.server import App
import tempfile, pathlib

app = App(pathlib.Path("scenarios"))
loose = app.provision({"twins": "jira,github", "ttl_seconds": 60})
strict = app.provision({"twins": "jira", "ttl_seconds": 60, "strict_tokens": True})

def call(run_id, provider, method, path, token):
    h = {} if token is None else {"authorization": f"Bearer {token}"}
    return app.gateway.handle(method, f"{provider}.{run_id}.twins.local", path, h, b"")

# 1) 声明的边界要自报家门，且与欠账分开计数
dec = call(loose["run_id"], "github", "GET", "/repos/acme/svc/actions/secrets", loose["credentials"]["github"])
assert dec.headers.get("X-Twin-Declared") == "true", "声明的端点必须带 X-Twin-Declared"
assert dec.body["_twin_reason"], "声明必须带理由"
debt = call(loose["run_id"], "github", "GET", "/repos/acme/svc/hooks", loose["credentials"]["github"])
assert debt.headers.get("X-Twin-Stub") == "true"
assert "X-Twin-Declared" not in debt.headers, "没声明的端点不能被洗成边界"
rep = app.stub_hits(loose["run_id"])
assert (rep["declared_hits"], rep["unplanned_hits"]) == (1, 1), rep

# 2) 严格模式：形状合法的假 token 必须被拒，下发的那个必须能用
bad = call(strict["run_id"], "jira", "GET", "/rest/api/3/myself", "definitely-not-valid")
good = call(strict["run_id"], "jira", "GET", "/rest/api/3/myself", strict["credentials"]["jira"])
assert bad.status == 401 and bad.body["error"] == "invalid_credential", bad.body
assert good.status == 200, good.body
# 3) 反过来：默认模式下同一个假 token 仍被放行（已知局限，写死成观测而不是免责）
loose_jira = call(loose["run_id"], "jira", "GET", "/rest/api/3/myself", "definitely-not-valid")
assert loose_jira.status == 200, "默认模式不校验值，这条是记录而非期望"
# 4) 第三态：真实 provider 里不存在的端点必须 404，且不进覆盖率分母
absent = call(loose["run_id"], "github", "POST", "/repos/acme/svc/issues/56", loose["credentials"]["github"])
assert absent.status == 404, absent.body
assert absent.headers.get("X-Twin-Absent") == "true"
assert "X-Twin-Stub" not in absent.headers, "不存在的端点不能被记成'还没实现'"
rep2 = app.stub_hits(loose["run_id"])
assert (rep2["total_hits"], rep2["absent_hits"]) == (2, 1), rep2
print("   声明式 stub：欠账 1 / 边界 1；严格令牌：假 401 / 真 200；默认模式放行（已知局限）；"
      "第三态：编出来的端点 404 且不记账")
PYEOF

echo "== 10/12 韧性：故障注入 / 限流 / Link 分页 / 事件投递 / 并发写 =="
python3 - <<'PYEOF'
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from argaclone import chaos, webhooks
from argaclone.server import App
from argaclone.store import StaleRunVersion, Store

app = App("scenarios")
view = app.provision({"twins": "stripe,github", "ttl": 600})
rid, creds = view["run_id"], view["credentials"]
run = app.store.get(rid)


def call(provider, method, path, body=None, token=None):
    raw = json.dumps(body).encode() if body is not None else b""
    return app.gateway.handle(method, f"{provider}.{rid}.twins.local", path,
                              {"authorization": f"Bearer {token or creds[provider]}"}, raw)


# 1) 故障注入：坏一次，第二次必须真的执行
chaos.add_fault(run, {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                      "mode": "status", "status": 500, "times": 1})
first = call("stripe", "POST", "/v1/customers", {"email": "a@ver.test"})
second = call("stripe", "POST", "/v1/customers", {"email": "a@ver.test"})
assert first.status == 500 and first.headers.get("X-Twin-Fault") == "status", first.body
assert second.status == 200 and "X-Twin-Fault" not in second.headers

# 2) 限流：第 N+1 发 429，且带可退避的 Retry-After
chaos.add_rate_limit(run, {"provider": "github", "limit": 1, "window_seconds": 60})
assert call("github", "GET", "/user").status == 200
limited = call("github", "GET", "/user")
assert limited.status == 429 and limited.headers.get("Retry-After") == "1", limited.body
assert limited.headers.get("X-Twin-Rate-Limited") == "true"
# 关掉限流再做后面的检查：否则下面每发 github 调用都 429，
# 报出来的会是"分页坏了"——排查方向从一开始就错。
chaos.clear(run, "rate_limits")
chaos.clear(run, "rate_used")

# 3) Link 分页：翻页信号真的在，且最后一页不给 next
gh = run.state["github"]
gh["repos"]["acme/ver"] = {"id": 9, "name": "ver", "full_name": "acme/ver",
                           "private": False, "stargazers_count": 0,
                           "default_branch": "main"}
gh["issues"]["acme/ver"] = {str(n): {"number": n, "title": f"i{n}", "state": "open"}
                            for n in range(1, 41)}
page1 = call("github", "GET", "/repos/acme/ver/issues?state=all")
page2 = call("github", "GET", "/repos/acme/ver/issues?state=all&page=2")
assert len(page1.body) == 30 and 'rel="next"' in page1.headers["Link"]
assert len(page2.body) == 10 and 'rel="next"' not in page2.headers["Link"]
assert not ({i["number"] for i in page1.body} & {i["number"] for i in page2.body})

# 4) 事件投递：注册 → 变更 → 收到 → 签名能验
received = []


class Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        received.append((dict(self.headers), raw))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Sink)
threading.Thread(target=srv.serve_forever, daemon=True).start()
hook = webhooks.register(run, {"url": f"http://127.0.0.1:{srv.server_port}/h",
                               "provider": "stripe"})
call("stripe", "POST", "/v1/customers", {"email": "b@ver.test"})
srv.shutdown()
assert len(received) == 1, received
headers, raw = received[0]
assert headers["X-Argaclone-Event"] == "customer.created"
assert webhooks.verify(hook["secret"], headers["X-Argaclone-Signature"], raw)

# 5) commit_then_fail：写生效但响应失败，且事件照发
#    这条是重复扣款的唯一成因：客户端看到 500 会重试，而服务端其实已经做过了。
from argaclone import chaos as ch2

run2 = app.store.get(rid)
before = len(app.store.get(rid).state["stripe"]["customers"])
ch2.add_fault(run2, {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                     "mode": "commit_then_fail", "status": 500, "times": 1})
resp = app.gateway.handle("POST", f"stripe.{rid}.twins.local", "/v1/customers",
                          {"authorization": f"Bearer {creds['stripe']}"},
                          b'{"email": "ghost@verify.test"}')
assert resp.status == 500, resp.status
assert resp.headers.get("X-Twin-Fault") == "commit_then_fail"
after = len(app.store.get(rid).state["stripe"]["customers"])
assert after == before + 1, "状态没变 —— 那这条故障就不是在测幂等，只是在测 500"

# 5b) 幂等键：写生效 + 响应 500 + 同一个 key 重试 → 只有一份，且拿回第一次的 200
ch2.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                   "path": "/v1/customers", "mode": "commit_then_fail",
                                   "status": 500, "times": 1})
_hdr = {"authorization": f"Bearer {creds['stripe']}", "idempotency-key": "verify-idem-1"}
first = app.gateway.handle("POST", f"stripe.{rid}.twins.local", "/v1/customers", dict(_hdr),
                           b'{"email": "idem@verify.test"}')
assert first.status == 500, first.status
second = app.gateway.handle("POST", f"stripe.{rid}.twins.local", "/v1/customers", dict(_hdr),
                            b'{"email": "idem@verify.test"}')
assert second.status == 200, second.status
assert second.headers.get("Idempotent-Replayed") == "true", "没走重放 —— 那是又写了一遍"
_idem = [c for c in app.store.get(rid).state["stripe"]["customers"].values()
         if c.get("email") == "idem@verify.test"]
assert len(_idem) == 1, f"重试建了 {len(_idem)} 份"

# 5c) 客户端重试层：模型看到 500 会自己重发，但它重发时**会改写措辞**（钥匙跟着变），
#     于是又写一遍。真实 SDK 的重试发生在一次逻辑操作内部，重发的是同一份请求。
#     这一段证明"客户端建对之后环境能给出恰好一次"，并证明没有钥匙时它照样写两遍。
from argaclone.runner import Action
ch2.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                   "path": "/v1/customers", "mode": "commit_then_fail",
                                   "status": 500, "times": 1})
_runner = app.runner_for(rid)
_runner.idempotent_writes = True
_runner.client_retries = 2
_rec = _runner.call(Action("stripe", "POST", "/v1/customers", {"email": "cli@verify.test"},
                           headers={"authorization": f"Bearer {creds['stripe']}"}))
assert _rec["status"] == 200 and _rec["client_retries"] == 1, _rec
assert _rec["replayed"] is True, "重试没拿到第一次那份响应 —— 说明钥匙变了"
_cli = [c for c in app.store.get(rid).state["stripe"]["customers"].values()
        if c.get("email") == "cli@verify.test"]
assert len(_cli) == 1, f"客户端重试写成了 {len(_cli)} 份"

# 对照组：同一套重试、只是不带钥匙 —— 必须写两遍。去掉这一段，上面那条就分不清
# "恰好一次"是钥匙买的还是重试买的。
ch2.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                   "path": "/v1/customers", "mode": "commit_then_fail",
                                   "status": 500, "times": 1})
_bare = app.runner_for(rid)
_bare.idempotent_writes = False
_bare.client_retries = 2
_bare.call(Action("stripe", "POST", "/v1/customers", {"email": "nokey@verify.test"},
                  headers={"authorization": f"Bearer {creds['stripe']}"}))
_nokey = [c for c in app.store.get(rid).state["stripe"]["customers"].values()
          if c.get("email") == "nokey@verify.test"]
assert len(_nokey) == 2, f"没有钥匙的重试只写了 {len(_nokey)} 份 —— 对照组失效了"

# 5d) 退避：重试的**节奏**。次数对了但节奏错了，在限流环境里就是"越撞越多"。
_clock = []
_runner = app.runner_for(rid)
_runner.idempotent_writes = True
_runner.client_retries = 3
_runner.retry_backoff = 0.1
_runner.retry_backoff_cap = 0.3
_runner.jitter = 0.0
_runner._sleep = _clock.append
ch2.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                   "path": "/v1/customers", "mode": "status",
                                   "status": 500, "times": 4})
_brec = _runner.call(Action("stripe", "POST", "/v1/customers", {"email": "backoff@verify.test"},
                            headers={"authorization": f"Bearer {creds['stripe']}"}))
assert _brec["status"] == 500, _brec["status"]
assert _clock == [0.1, 0.2, 0.3], f"退避不是指数且没被 cap 截住：{_clock}"

# Retry-After 优先于指数退避：限流窗口是服务端定的，客户端猜短的只会更糟
ch2.add_rate_limit(app.store.get(rid), {"provider": "stripe", "limit": 1,
                                        "window_seconds": 30, "retry_after": 2})
_hit = app.runner_for(rid)
_hit.idempotent_writes = True
_hit.client_retries = 1
_hit.retry_backoff = 0.01
_hit.retry_backoff_cap = 30.0
_hit.jitter = 0.0
_clock2 = []
_hit._sleep = _clock2.append
_hit.call(Action("stripe", "POST", "/v1/customers", {"email": "ra1@verify.test"},
                 headers={"authorization": f"Bearer {creds['stripe']}"}))
_rrec = _hit.call(Action("stripe", "POST", "/v1/customers", {"email": "ra2@verify.test"},
                         headers={"authorization": f"Bearer {creds['stripe']}"}))
assert _rrec["status"] == 429, _rrec["status"]
assert _clock2 == [2.0], f"没按 Retry-After 等：{_clock2}"
ch2.clear(app.store.get(rid), "rate_limits")
ch2.clear(app.store.get(rid), "rate_used")

# 6) 并发写：基于旧世界的写必须被拒，而不是覆盖
app.store.directory = None  # 内存里做不了跨进程，起一个临时目录的 Store
import tempfile
from pathlib import Path

tmpdir = Path(tempfile.mkdtemp())
app.store.directory = tmpdir
app.store.save(run)
a, b = Store(tmpdir), Store(tmpdir)
ra, rb = a.get(rid), b.get(rid)
ra.extras["who"] = "a"
a.save(ra)
rb.extras["who"] = "b"
try:
    b.save(rb)
    raise AssertionError("旧世界把新世界覆盖了 —— 版本检查没生效")
except StaleRunVersion:
    pass

print("   故障注入 500/一次 / 429+Retry-After / Link 翻页不重叠 / 事件签名可验 / 并发写被拒"
      " / 写已生效但响应失败 / 幂等键重放不重复写 / 客户端重试只在有钥匙时安全")
PYEOF

echo "== 11/12 README 数字可复算（文档不漂移）=="
python3 scripts/check_readme.py --selftest
python3 scripts/check_readme.py

echo "== 12/12 真模型对比（可选，需 MAGPIE_BASE 可达）=="
if curl -sf "${MAGPIE_BASE:-http://127.0.0.1:3425}/v1/models" -o /dev/null --max-time 3; then
  python3 demo/agent_compare.py --models deepseek-v4.1-flash --limit 1 --workers 5 \
    --out /tmp/agent_compare_smoke.json
else
  echo "   跳过（LLM 网关不可达；离线验证不需要它）"
fi

echo
echo "全部通过。"
