"""动作执行器：把一串动作打进 twin，并记录一份可判分的 trace。

这里最容易被写错的一点：**不同 provider 的"成功"语义不一样**。
Slack 失败也回 HTTP 200（看 body 的 ok 字段），Stripe 用状态码。
如果 runner 只看状态码，Slack 的所有失败都会在 trace 里记成成功，
下游 grader 就永远看不到真正的错误——这是最典型的一种"假绿"。

所以成功判定必须按 provider 分派，且这件事要写在这里而不是交给调用方。
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .gateway import Gateway


@dataclass
class Action:
    provider: str
    method: str
    path: str
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    query: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Action":
        return Action(
            provider=d["provider"],
            method=d.get("method", "GET").upper(),
            path=d["path"],
            body=d.get("body") or {},
            headers=d.get("headers") or {},
            query=d.get("query") or "",
        )


def is_success(provider: str, status: int, payload: Any) -> bool:
    """provider 感知的成功判定。"""
    if provider == "slack":
        # Slack：永远 200，成功看 body.ok
        return status == 200 and isinstance(payload, dict) and payload.get("ok") is True
    # Stripe 一类：状态码 + 没有 error 信封
    if status >= 400:
        return False
    return not (isinstance(payload, dict) and "error" in payload and len(payload) == 1)


def empty_trace() -> dict[str, Any]:
    """没跑过任何动作时的 trace。

    为什么需要显式的空 trace 而不是 None：判据里大量使用 `trace.bodies`、
    `trace.path_sequence` 这类路径，如果把它们解析成 PATH_ERROR，
    "什么都没做"就会被当成"判据坏了"，两种缺陷混在一起分不开。
    空 trace 表达的是真实语义——没有发生过调用，所以没有写请求、没有路径。
    """
    return {
        "calls": [],
        "count": 0,
        "providers": [],
        "stub_calls": 0,
        "absent_calls": 0,
        "error_calls": 0,
        "client_retries": 0,
        "path_sequence": [],
        "bodies": [],
        "writes": [],
    }


# 可重试的传输级失败。429 必须在内：限流是"稍后再来"，不是"你错了"。
# 402/400/404 这些是语义拒绝，重发只会拿到同一个答案 —— 排除它们是这条判据的价值所在。
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


# Slack 把**所有**方法都做成 POST —— 读聊天记录也是 POST。
# 所以"method != GET 就是写"这条判据在 Slack 上会把每一次读取都记成写，
# 于是"只发一次公告"被判成刷屏、"回帖确认"被判成多余写。
# 这是真实踩到的假阴性：正确解被判 UNSAFE。
SLACK_READ_METHODS = (
    "conversations.list",
    "conversations.history",
    "conversations.members",
    "conversations.info",
    "conversations.replies",
    "users.list",
    "users.info",
    "auth.test",
    "team.info",
    "files.list",
    "files.info",
    "search.messages",
)


def _is_write(provider: str, method: str, path: str) -> bool:
    """这次调用是否真的改变了状态。

    为什么不用 `method != GET`：Slack 的读接口也是 POST（见上）。
    Stripe 与 GitHub 的读接口是 GET，所以那条判据对它们成立，对 Slack 不成立。
    """
    if method.upper() == "GET":
        return False
    if provider == "slack":
        return not any(m in path for m in SLACK_READ_METHODS)
    return True


class Runner:
    """直接在进程内打 gateway。

    不走真实 socket 是刻意的：动作序列会被重放几千次，socket 只会拖慢而不会
    让语义更真——真不真取决于 twin 的实现，不取决于有没有过网线。
    """

    def __init__(
        self,
        gateway: Gateway,
        run_id: str,
        idempotent_writes: bool = False,
        client_retries: int = 0,
        retry_backoff: float = 0.0,
        retry_backoff_cap: float = 2.0,
        jitter: float = 0.5,
        sleep=time.sleep,
    ) -> None:
        self.gateway = gateway
        self.run_id = run_id
        self.calls: list[dict[str, Any]] = []
        # 模拟"会正确使用幂等键的 SDK"。注意这是**内容寻址**的近似：
        # 真实 SDK 在一次逻辑操作内部复用同一把钥匙，而这里拿不到"逻辑操作"
        # 这个概念，只能用 (provider, method, path, body) 派生。所以它证明的是
        # "服务端支持幂等键之后重复写入会消失"，不是"模型知道该用幂等键"。
        self.idempotent_writes = idempotent_writes
        # 客户端重试层：模拟真实 SDK 在"一次逻辑操作"内部的传输级重试。
        #
        # 为什么需要它：`commit_then_fail` 之后，模型看到 500 会重试 —— 这是对的行为。
        # 但模型的重试是**重新组织一次请求**（措辞、字段都可能变），而真实 SDK 重试的是
        # 同一个已经序列化好的请求，钥匙和 body 都不变。两者在服务端看起来完全不同：
        # 前者是一笔新操作（Stripe 也会写两次），后者才是幂等键能挡的那一种。
        #
        # 所以这个参数量的不是"模型聪不聪明"，而是"**客户端建对了之后，这个环境能不能
        # 给出恰好一次**"。0 = 现造现报（模型自己重试），>0 = 假装客户端有重试层。
        self.client_retries = max(0, int(client_retries))
        # 退避：真实 SDK 不会连着轰。`retry_backoff` 是指数基数（秒），第 n 次重试
        # 等 `base * 2**(n-1)`，封顶 `cap`；`jitter` 是"全抖动"（full jitter）的幅度，
        # 实际等待 = 上限内均匀随机 —— 固定退避会让一批客户端在同一个毫秒醒来，
        # 把一次 5xx 变成一次自造的惊群。
        # 0 表示不退避（老行为，测试里用它保持快）。
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.retry_backoff_cap = max(0.0, float(retry_backoff_cap))
        self.jitter = min(1.0, max(0.0, float(jitter)))
        # 注入 sleep 是为了让"等了多少"可测：真睡会让一排重试测试变成秒级
        self._sleep = sleep

    def _wait_before_retry(self, attempt: int, headers: dict[str, str]) -> float:
        """这一次重试前该等多久。

        两条规则，顺序有讲究：
        1. **服务端说等多久就等多久**（`Retry-After`，秒数；HTTP-date 形式不解析，
           直接忽略退回指数退避 —— 解析半个格式比不解析更危险）。
        2. 否则指数退避 + 全抖动。

        `Retry-After` 优先的原因：限流窗口是服务端定的，客户端自己猜一个更短的
        间隔只会把 429 变成更多 429。
        """
        # 退避关掉 = 完全没有节奏纪律（也不看 Retry-After）。这条分界要显式，
        # 否则"默认不退避"的评测会在 134 次重试里真睡几分钟，而读数一点没变。
        if self.retry_backoff <= 0:
            return 0.0
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw:
            try:
                secs = float(str(raw).strip())
            except ValueError:
                secs = None
            if secs is not None and secs >= 0:
                # **不拿自家 cap 去截服务端给的 `Retry-After`**。
                # cap 管的是我们自己猜的那条指数曲线；服务端说的是事实。
                # 实测代价：罚站 `Retry-After: 20` 被默认 cap=2.0 截成 2s，
                # 于是"尊重 Retry-After"的客户端 2 秒后就回来继续撞 —— 看起来像
                # 它没退避，其实是我把它的等待改小了。
                return secs
        ceiling = min(self.retry_backoff * (2 ** (attempt - 1)), self.retry_backoff_cap)
        if self.jitter <= 0:
            return ceiling
        # full jitter：在 [ceiling*(1-jitter), ceiling] 之间均匀取值。
        # 取上界为 ceiling 而不是"ceiling 到 2*ceiling"，是为了让封顶仍然是硬上界。
        low = ceiling * (1.0 - self.jitter)
        return round(random.uniform(low, ceiling), 4)

    def _host(self, provider: str) -> str:
        return f"{provider}.{self.run_id}.twins.local"

    def call(self, action: Action) -> dict[str, Any]:
        raw_path = action.path + (("?" + action.query) if action.query else "")
        body = json.dumps(action.body).encode() if action.body else b""
        headers = {k.lower(): v for k, v in action.headers.items()}
        headers.setdefault("content-type", "application/json")
        if self.idempotent_writes and action.method.upper() not in ("GET", "HEAD"):
            seed = f"{action.provider}\n{action.method.upper()}\n{action.path}\n{action.query}\n"
            headers.setdefault(
                "idempotency-key",
                hashlib.sha256((seed + body.decode("utf-8", "replace")).encode()).hexdigest()[:32],
            )

        # 客户端重试层：只重试**传输级失败**（5xx / 响应没落地），不重试 4xx。
        # 4xx 是语义拒绝，重发一遍只会得到同一个 4xx —— 那是日志噪声，不是韧性。
        attempts = 0
        retry_waits: list[float] = []
        is_write_call = _is_write(action.provider, action.method.upper(), action.path)
        while True:
            resp = self.gateway.handle(
                action.method, self._host(action.provider), raw_path, headers, body
            )
            payload = resp.body
            ok = is_success(action.provider, resp.status, payload)
            # 注意这里**不要求** idempotent_writes：不带钥匙的客户端也会重试，
            # 而那正是重复扣款的来源。把"重试"绑在"有钥匙"上，等于让对照组消失。
            # 读也要重试 429：限流不是"你问错了"，是"稍后再问"，而重发一次读
            # 不改变世界。5xx 的读则**不**重试 —— 那一路是要让模型自己决定
            # "是重试还是换个办法"，替它决定会把 §23 那批韧性读数抹平。
            read_throttled = (not is_write_call) and resp.status == 429
            if (
                (not is_write_call and not read_throttled)
                or ok
                or resp.status not in RETRYABLE_STATUS
                or attempts >= self.client_retries
            ):
                break
            attempts += 1
            waited = self._wait_before_retry(attempts, resp.headers)
            retry_waits.append(waited)
            if waited:
                self._sleep(waited)

        record = {
            "n": len(self.calls) + 1,
            "provider": action.provider,
            "method": action.method,
            "path": action.path,
            "status": resp.status,
            "ok": ok,
            "stub": resp.headers.get("X-Twin-Stub") == "true",
            # 声明的边界与欠账在结果文件里也要能分开（否则离线报表只能
            # 拿到总数，再想拆就得重跑）
            "declared": resp.headers.get("X-Twin-Declared") == "true",
            # 第三态：真实 provider 里不存在的端点。它既不是欠账（不该算进覆盖率
            # 分母），也不能算成功 —— agent 编出来的路径被 404 掉是正确行为。
            "absent": resp.headers.get("X-Twin-Absent") == "true",
            # 幂等重放：请求被网关挡回去了，世界没有第二次变动。判"重复写入"时
            # 必须排除它 —— 否则"重试但安全"会被算成"重试且写了两遍"，
            # 正好把这条能力买到的效果抹掉。
            "replayed": resp.headers.get("Idempotent-Replayed") == "true",
            # 客户端替它重试了几次。与 replayed 分开记：一次 500 之后重试成功，
            # 若中间那次是同一个钥匙，服务端只写了 1 遍；若钥匙变了（模型改写），
            # 就会写 2 遍 —— 这两件事在报表上必须能分开，否则"重试"这个词
            # 既可能是药的功劳，也可能是病的来源。
            "client_retries": attempts,
            # 每次重试前实际等了多久（秒）。记下来才判得开"它真的退避了"和
            # "它只是碰巧第二次成功" —— 后者在快环境里看起来一模一样。
            "retry_waits": retry_waits,
            "error": None if ok else self._error_of(payload),
            "request": action.body or None,
            # 响应体必须留在 record 里。没有它，agent 看到的是"状态码 200、
            # 内容空"，只能靠猜下一步 —— 那测出来的是猜运，不是能力。
            # 判据也可能要看响应（比如"返回里必须带 invoice id"）。
            # 裸字节响应（GitHub raw 文件）要转成 str：trace 会被 json.dumps
            # 落到结果文件里，bytes 会在那一刻抛 TypeError，而那时已经跑完
            # 所有 episode —— 一个序列化细节就能让整轮结果全废。
            "response": payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload,
        }
        self.calls.append(record)
        return record

    @staticmethod
    def _error_of(payload: Any) -> Any:
        if isinstance(payload, dict):
            if "error" in payload:
                return payload["error"]
            if payload.get("ok") is False:
                return {k: v for k, v in payload.items() if k != "ok"}
        return str(payload)[:200]

    def _normalize_write(self, provider: str, body: Any) -> Any:
        """把写请求里的 Slack channel 名归一成 channel id。

        为什么必须做：Slack 接受 `C_SALES_OPS` 与 `#sales-ops` 两种写法，
        而判据只能写一个字符串。不归一的话，"发对了地方但用了名字"会被判成
        没发 —— 判据在考格式而不是考事，pass 率就不可信了。
        归一放在 trace 层而不是 twin 层：twin 必须保留客户端的原始写法
        （那是保真的一部分），判据看到的才需要一个稳定形态。
        """
        if provider != "slack" or not isinstance(body, dict):
            return body
        channel = body.get("channel")
        if not isinstance(channel, str):
            return body
        try:
            state = self.gateway.store.get(self.run_id, allow_expired=True).state["slack"]
        except Exception:  # noqa: BLE001 - 取不到就原样返回，判据自己会表现为没命中
            return body
        channels = state.get("channels") or {}
        resolved = channel if channel in channels else None
        if resolved is None:
            name = channel[1:] if channel.startswith("#") else channel
            for cid, ch in channels.items():
                if ch.get("name") == name:
                    resolved = cid
                    break
        if resolved is None or resolved == channel:
            return body
        out = dict(body)
        out["channel"] = resolved
        return out

    def run(self, actions: list[Action | dict[str, Any]]) -> dict[str, Any]:
        for a in actions:
            self.call(a if isinstance(a, Action) else Action.from_dict(a))
        return self.trace()

    def trace(self) -> dict[str, Any]:
        writes = [c for c in self.calls if _is_write(c["provider"], c["method"], c["path"])]
        return {
            "calls": self.calls,
            "count": len(self.calls),
            "providers": sorted({c["provider"] for c in self.calls}),
            "stub_calls": sum(1 for c in self.calls if c["stub"]),
            "absent_calls": sum(1 for c in self.calls if c.get("absent")),
            "error_calls": sum(1 for c in self.calls if not c["ok"]),
            "client_retries": sum(c.get("client_retries", 0) for c in self.calls),
            "path_sequence": [f"{c['provider']}:{c['method']} {c['path']}" for c in self.calls],
            # 写请求的 body 被序列化成字符串列表。为什么需要这个：
            # 只看 path_sequence 无法区分"发到了 #marketing-ops"还是"发到了 #announcements"，
            # 而"发错频道"恰恰是最常见也最危险的 agent 失误。
            # 用字符串列表 + any_contains/none_contains 断言，既确定又不引入查询语言。
            "bodies": [
                json.dumps(self._normalize_write(c["provider"], c["request"]), ensure_ascii=False)
                for c in writes
                if c["request"]
            ],
            "writes": [
                {
                    "provider": c["provider"],
                    "method": c["method"],
                    "path": c["path"],
                    "body": self._normalize_write(c["provider"], c["request"]),
                    # 重放标记要跟着写记录走。否则 trace 里两个 200 长得一样，
                    # 而一个是"世界变了两次"、一个"世界只变了一次"。
                    "replayed": c.get("replayed", False),
                    # 客户端在这个动作内部替调用方重试了几次。它和 replayed 一起
                    # 才说明"恰好一次"是怎么来的：重试提供结果，钥匙提供唯一性。
                    "client_retries": c.get("client_retries", 0),
                }
                for c in writes
            ],
        }
