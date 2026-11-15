"""命令行：对着本地 server 说话，形状刻意贴近 arga-cli。

差异写在明处：`arga` 是远程服务 + device 登录，这里 `--api` 直接指向本地进程。
保留同构的部分是为了让"从这抄到那"的成本足够低。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib import error, request

DEFAULT_API = os.environ.get("ARGACLONE_API", "http://127.0.0.1:8787")


def _call(method: str, path: str, body: dict[str, Any] | None = None, api: str = DEFAULT_API) -> Any:
    url = api.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw.decode(errors="replace")}
        payload.setdefault("http_status", exc.code)
        return payload


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# ---------------- serve ----------------


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import App, serve

    token = args.admin_token or os.environ.get("ARGACLONE_ADMIN_TOKEN") or None
    data_dir = args.data_dir or os.environ.get("ARGACLONE_DATA_DIR") or None
    app = App(args.scenarios, admin_token=token, data_dir=data_dir)
    server = serve(app, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"argaclone listening on http://{host}:{port}")
    print(f"admin:     http://{host}:{port}/admin/runs")
    print(
        "admin auth: 已开启（Authorization: Bearer <token> 或 X-Admin-Token）"
        if token
        else "admin auth: 关闭（本机单人调试默认）——给别人连之前请加 --admin-token"
    )
    print(
        f"state:     {len(app.store.all_runs())} 个 run 从 {data_dir} 恢复"
        if data_dir
        else "state:     纯内存（重启即丢）——要跨重启请加 --data-dir"
    )
    print(f"twin:      curl -H 'Host: stripe.<run_id>.twins.local' http://{host}:{port}/v1/customers")
    try:
        import threading

        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


# ---------------- twin-runs ----------------


def cmd_twin_runs(args: argparse.Namespace) -> int:
    base = "/admin/runs"
    if args.op == "list":
        _print(_call("GET", base, api=args.api))
    elif args.op == "create":
        body: dict[str, Any] = {"twins": args.twins, "ttl": args.ttl}
        if args.scenario:
            body["scenario_id"] = args.scenario
        if args.prompt:
            body["prompt"] = args.prompt
        _print(_call("POST", base, body, api=args.api))
    elif args.op == "status":
        _print(_call("GET", f"{base}/{args.run_id}", api=args.api))
    elif args.op == "reset":
        _print(_call("POST", f"{base}/{args.run_id}/reset", {}, api=args.api))
    elif args.op == "extend":
        _print(_call("POST", f"{base}/{args.run_id}/extend", {"ttl": args.ttl}, api=args.api))
    elif args.op == "lock":
        _print(_call("POST", f"{base}/{args.run_id}/lock", {}, api=args.api))
    elif args.op == "teardown":
        _print(_call("DELETE", f"{base}/{args.run_id}", api=args.api))
    else:
        print(f"unknown op: {args.op}", file=sys.stderr)
        return 2
    return 0


# ---------------- scenarios ----------------


def cmd_scenarios(args: argparse.Namespace) -> int:
    if args.op == "list":
        _print(_call("GET", "/admin/scenarios", api=args.api))
    elif args.op == "get":
        _print(_call("GET", f"/admin/scenarios/{args.scenario_id}", api=args.api))
    elif args.op == "create":
        with open(args.file, encoding="utf-8") as fh:
            body = json.load(fh)
        _print(_call("POST", "/admin/scenarios", body, api=args.api))
    elif args.op == "delete":
        _print(_call("DELETE", f"/admin/scenarios/{args.scenario_id}", api=args.api))
    else:
        print(f"unknown op: {args.op}", file=sys.stderr)
        return 2
    return 0


# ---------------- act / grade / state ----------------


def cmd_act(args: argparse.Namespace) -> int:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        payload = json.loads(sys.stdin.read())
    actions = payload["actions"] if isinstance(payload, dict) else payload
    _print(_call("POST", f"/admin/runs/{args.run_id}/actions", {"actions": actions}, api=args.api))
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    spec = None
    if args.spec:
        with open(args.spec, encoding="utf-8") as fh:
            spec = json.load(fh)
    out = _call("POST", f"/admin/runs/{args.run_id}/grade", {"spec": spec}, api=args.api)
    _print(out)
    verdict = (out or {}).get("verdict")
    return {"PASS": 0, "FAIL": 1, "UNSAFE": 3, "ERROR": 4}.get(verdict, 5)


def cmd_state(args: argparse.Namespace) -> int:
    _print(_call("GET", f"/admin/runs/{args.run_id}/state", api=args.api))
    return 0


# ---------------- parser ----------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="argaclone", description="自建 SaaS 孪生沙箱")
    p.add_argument("--api", default=DEFAULT_API, help="admin API base URL")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="起服务（admin + 全部 twin 单端口）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--scenarios", default="scenarios")
    s.add_argument(
        "--admin-token",
        default=None,
        help="开启 admin 鉴权（/admin/* 与 /mcp 都需要该令牌）；也可用 ARGACLONE_ADMIN_TOKEN",
    )
    s.add_argument(
        "--data-dir",
        default=None,
        help="把每个 run 落盘到该目录，重启可恢复；也可用 ARGACLONE_DATA_DIR。默认纯内存",
    )
    s.set_defaults(func=cmd_serve)

    tr = sub.add_parser("twin-runs", help="run 生命周期")
    tr_sub = tr.add_subparsers(dest="op", required=True)
    tr_sub.add_parser("list")
    c = tr_sub.add_parser("create")
    c.add_argument("--twins", default="stripe")
    c.add_argument("--ttl", type=int, default=600)
    c.add_argument("--scenario")
    c.add_argument("--prompt")
    for op in ("status", "reset", "lock", "teardown"):
        sp = tr_sub.add_parser(op)
        sp.add_argument("--run-id", required=True)
    ext = tr_sub.add_parser("extend")
    ext.add_argument("--run-id", required=True)
    ext.add_argument("--ttl", type=int, default=600)
    tr.set_defaults(func=cmd_twin_runs)

    sc = sub.add_parser("scenarios", help="seed 场景")
    sc_sub = sc.add_subparsers(dest="op", required=True)
    sc_sub.add_parser("list")
    g = sc_sub.add_parser("get")
    g.add_argument("--scenario-id", required=True)
    cr = sc_sub.add_parser("create")
    cr.add_argument("--file", required=True)
    d = sc_sub.add_parser("delete")
    d.add_argument("--scenario-id", required=True)
    sc.set_defaults(func=cmd_scenarios)

    a = sub.add_parser("act", help="执行动作序列")
    a.add_argument("--run-id", required=True)
    a.add_argument("--file")
    a.set_defaults(func=cmd_act)

    gd = sub.add_parser("grade", help="按 spec 判分（退出码 PASS=0 FAIL=1 UNSAFE=3 ERROR=4）")
    gd.add_argument("--run-id", required=True)
    gd.add_argument("--spec")
    gd.set_defaults(func=cmd_grade)

    st = sub.add_parser("state", help="导出 run 全量状态（seed / 断言都用它）")
    st.add_argument("--run-id", required=True)
    st.set_defaults(func=cmd_state)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
