"""Python SDK：一行接进来，不用记 admin 路由。

设计原则：**SDK 只做 HTTP，不在本地替服务端算任何东西**。
一旦 SDK 开始"帮你想"，客户端看到的状态就可能与服务端不一致，
而这套东西的整个卖点是"状态可信、可复现"。

用法：
    from argaclone.client import Client

    with Client() as c:                      # 默认 http://127.0.0.1:8787
        run = c.provision(["stripe", "slack"], ttl=600,
                          scenario="ecom-duplicate-billing")
        print(run.credentials["stripe"])     # sk_test_twin_xxx
        run.act([{"provider": "stripe", "method": "POST",
                  "path": "/v1/customers/cus_DUP_EXTRA",
                  "body": {"metadata": {"merged_into": "cus_DUP_KEEP"}}}])
        print(run.grade()["verdict"])

零依赖（只用标准库）：这套东西要能在 CI 容器里 `pip install -e .` 之前就跑起来。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class ArgacloneError(RuntimeError):
    """服务端返回了非 2xx。带上状态码与 body，别只留一句 'request failed'。"""

    def __init__(self, status: int, body: Any, path: str) -> None:
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"{path} -> HTTP {status}: {body}")


class Run:
    """一个已存在的 twin run 的句柄。"""

    def __init__(self, client: "Client", data: dict) -> None:
        self._client = client
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __repr__(self) -> str:
        return f"<Run {self._data.get('run_id')} twins={','.join(self._data.get('twins') or [])}>"

    @property
    def run_id(self) -> str:
        return self._data["run_id"]

    @property
    def twins(self) -> list[str]:
        return list(self._data.get("twins") or [])

    @property
    def endpoints(self) -> dict[str, str]:
        return dict(self._data.get("endpoints") or {})

    @property
    def credentials(self) -> dict[str, str]:
        return dict(self._data.get("credentials") or {})

    def refresh(self) -> "Run":
        self._data = self._client.get_run(self.run_id)
        return self

    def act(self, actions: list[dict]) -> dict:
        """执行一批动作。**自动带 run-scoped 凭据**（除非 action 显式给了 Authorization）。"""
        for a in actions:
            headers = dict(a.get("headers") or {})
            if not any(k.lower() == "authorization" for k in headers):
                token = self.credentials.get(a.get("provider") or "")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            a["headers"] = headers
        return self._client.act(self.run_id, actions)

    def grade(self, spec: dict | None = None) -> dict:
        return self._client.grade(self.run_id, spec)

    def state(self) -> dict:
        return self._client.state(self.run_id)

    def stub_hits(self, provider: str | None = None) -> dict:
        return self._client.stub_hits(self.run_id, provider)

    def reset(self) -> "Run":
        self._data = self._client.reset(self.run_id)
        return self

    def extend(self, ttl: int = 600) -> "Run":
        self._data = self._client.extend(self.run_id, ttl)
        return self

    def lock(self) -> "Run":
        self._data = self._client.lock(self.run_id)
        return self

    def teardown(self) -> dict:
        return self._client.teardown(self.run_id)


class Client:
    def __init__(self, base: str = "http://127.0.0.1:8787", timeout: float = 30.0,
                 admin_token: str | None = None) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.admin_token = admin_token

    @classmethod
    def from_env(cls, base: str | None = None) -> "Client":
        """服务端开了 --admin-token 时，客户端也得带着，否则每个 admin 调用都是 401。"""
        return cls(
            base or os.environ.get("ARGACLONE_API", "http://127.0.0.1:8787"),
            admin_token=os.environ.get("ARGACLONE_ADMIN_TOKEN") or None,
        )

    # ---- 传输 ----

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.admin_token:
            req.add_header("X-Admin-Token", self.admin_token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode("utf-8", "replace")
            raise ArgacloneError(exc.code, body, path) from None
        if not raw:
            return None
        return json.loads(raw)

    # ---- 运行环境 ----

    def provision(self, twins: list[str] | str, ttl: int = 600,
                  scenario: str | None = None, prompt: str | None = None,
                  strict_tokens: bool = False) -> Run:
        body: dict[str, Any] = {
            "twins": twins if isinstance(twins, str) else ",".join(twins),
            "ttl_seconds": ttl,
        }
        if scenario:
            body["scenario_id"] = scenario
        if prompt:
            body["prompt"] = prompt
        if strict_tokens:
            # 只认 provision 下发的凭据。默认不开：手工调试时随手编一个 token
            # 更方便，但"孪生校验了凭据"在那种模式下是假话。
            body["strict_tokens"] = True
        return Run(self, self._request("POST", "/admin/runs", body))

    def list_runs(self) -> list[dict]:
        return self._request("GET", "/admin/runs").get("runs", [])

    def get_run(self, run_id: str) -> dict:
        return self._request("GET", f"/admin/runs/{run_id}")

    def run(self, run_id: str) -> Run:
        return Run(self, self.get_run(run_id))

    def reset(self, run_id: str) -> dict:
        return self._request("POST", f"/admin/runs/{run_id}/reset", {})

    def extend(self, run_id: str, ttl: int = 600) -> dict:
        return self._request("POST", f"/admin/runs/{run_id}/extend", {"ttl": ttl})

    def lock(self, run_id: str) -> dict:
        return self._request("POST", f"/admin/runs/{run_id}/lock", {})

    def teardown(self, run_id: str) -> dict:
        return self._request("DELETE", f"/admin/runs/{run_id}")

    def state(self, run_id: str) -> dict:
        return self._request("GET", f"/admin/runs/{run_id}/state")

    def act(self, run_id: str, actions: list[dict]) -> dict:
        return self._request("POST", f"/admin/runs/{run_id}/actions", {"actions": actions})

    def grade(self, run_id: str, spec: dict | None = None) -> dict:
        body = {"spec": spec} if spec else {}
        return self._request("POST", f"/admin/runs/{run_id}/grade", body)

    def stub_hits(self, run_id: str, provider: str | None = None) -> dict:
        path = f"/admin/runs/{run_id}/stub-hits"
        if provider:
            path += f"?provider={provider}"
        return self._request("GET", path)

    def clear_stub_hits(self, run_id: str) -> dict:
        return self._request("POST", f"/admin/runs/{run_id}/stub-hits", {})

    # ---- scenarios ----

    def scenarios(self) -> list[dict]:
        return self._request("GET", "/admin/scenarios").get("scenarios", [])

    def scenario(self, scenario_id: str) -> dict:
        return self._request("GET", f"/admin/scenarios/{scenario_id}")

    def save_scenario(self, scenario: dict) -> dict:
        return self._request("POST", "/admin/scenarios", scenario)

    def delete_scenario(self, scenario_id: str) -> dict:
        return self._request("DELETE", f"/admin/scenarios/{scenario_id}")

    # ---- context manager ----

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None
