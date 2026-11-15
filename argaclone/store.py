"""Run 生命周期与状态的唯一持有者。

设计要点（对应 Arga 的三条硬语义）：
  1. per-run 隔离：每个 run_id 有自己的一份 provider 状态，互不可见。
  2. 快照式 reset：provision 完成时打一份 baseline，reset 就是回滚到它，
     不是"重建一个空环境"——所以 reset 之后看起来跟刚 provision 完一模一样。
  3. 显式 TTL：过期就 410，不静默存活。

共享实体层（shared.people）是复刻品的价值所在：
同一个自然人在 Slack 和 Stripe 里必须是同一个人，否则"多系统交叉核对"这类
任务根本无从判定。这里用 email 做弱身份键，两端写入时自动关联。
"""

from __future__ import annotations

import copy
import json
import fcntl
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RunExpired(Exception):
    """run 已过期。调用方应回 410 而不是 404——语义不同，别混。"""


class RunNotFound(Exception):
    """run_id 不存在。"""


class StaleRunVersion(Exception):
    """手里的 run 已经过期：盘上的版本号比这份新。

    这是**乐观并发**的失败信号，不是错误状态的崩溃。它存在的理由：
    两个进程各持一份同一 run 时，后写的那个会把对方的改动整段抹掉——
    而抹掉之后的读数是"世界看起来没问题、只是少了一次操作"，
    这种缺陷在评测里无法与"agent 没做"区分。
    """


def new_id(prefix: str, n: int = 24) -> str:
    """生成 provider 风格的长 id。Stripe 是 `cus_` + 14~24 位，Slack 是 `U` + 大写串。"""
    return prefix + uuid.uuid4().hex[:n]


@dataclass
class Run:
    run_id: str
    twins: list[str]
    ttl_seconds: int
    created_at: float
    # provider 名 → 该 provider 的全部状态（纯 dict，方便 deepcopy 与 JSON 化）
    state: dict[str, dict[str, Any]] = field(default_factory=dict)
    # provision 完成时打的基线快照；reset 的目标
    baseline: dict[str, dict[str, Any]] = field(default_factory=dict)
    # stub 命中日志：provider → {endpoint_template: hits}
    stub_hits: dict[str, dict[str, int]] = field(default_factory=dict)
    # 其中"twin 自己声明过不打算实现"的那部分。
    # 与 stub_hits 分开记而不是事后按名单过滤：声明与命中必须成对落盘，
    # 否则改一次声明就会让历史报表变样（过去的读数不可复算）。
    declared_stub_hits: dict[str, dict[str, int]] = field(default_factory=dict)
    # 第三桶：请求的端点**真实 API 里根本不存在**（agent 自己编的，或把 A 家的路径
    # 打到了 B 家的 Host 上）。它既不是"我们欠着的活"，也不该给假成功 ——
    # 真实 provider 会 404，twin 照做。混进 stub_hits 会让欠账看起来比实际大。
    absent_hits: dict[str, dict[str, int]] = field(default_factory=dict)
    # 命中流水（带时间与完整路径）。GitHub 那套 admin 报告要 recent 20 条，
    # 但流水本身对任何 provider 都有用：只保留计数就无法回答"是哪次调用撞的"。
    stub_recent: list[dict[str, Any]] = field(default_factory=list)
    locked: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    # 落盘版本号：每次成功写盘 +1。用来做**乐观并发**——两个进程各自拿着一份
    # run 时，后写的那个会发现"我手里的是第 N 版，盘上已经是 N+1 版"，
    # 于是报错而不是把对方的改动抹掉。
    version: int = 0
    # 读到这份 run 时盘上的版本号。测试与调试时改它没有意义，所以不序列化。
    loaded_version: int = field(default=0, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "twins": list(self.twins),
            "ttl_seconds": self.ttl_seconds,
            "created_at": self.created_at,
            "state": self.state,
            "baseline": self.baseline,
            "stub_hits": self.stub_hits,
            "declared_stub_hits": self.declared_stub_hits,
            "absent_hits": self.absent_hits,
            "stub_recent": self.stub_recent,
            "locked": self.locked,
            "extras": self.extras,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Run":
        return cls(
            run_id=payload["run_id"],
            twins=list(payload.get("twins") or []),
            ttl_seconds=int(payload.get("ttl_seconds") or 0),
            created_at=float(payload.get("created_at") or 0.0),
            state=payload.get("state") or {},
            baseline=payload.get("baseline") or {},
            stub_hits=payload.get("stub_hits") or {},
            declared_stub_hits=payload.get("declared_stub_hits") or {},
            absent_hits=payload.get("absent_hits") or {},
            stub_recent=payload.get("stub_recent") or [],
            locked=bool(payload.get("locked")),
            extras=payload.get("extras") or {},
            version=int(payload.get("version") or 0),
            # 刚读进来的这份与盘上同版本：第一次写盘不算冲突。
            loaded_version=int(payload.get("version") or 0),
        )

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_seconds

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def remaining(self, now: float | None = None) -> int:
        return max(0, int(self.expires_at - (now if now is not None else time.time())))


class Store:
    """状态库。单锁足够——这是仿真器，吞吐不是它的卖点。

    落盘是**可选的镜像**，不是事务日志：
    - 给了 `directory` 就每个 run 存一份 JSON，进程重启后能接着用；
    - 写盘走"临时文件 → 回读校验 → os.replace"，所以崩在中间只会留下上一份完好的快照，
      不会留下半个文件；
    - 没有 `directory` 就退回纯内存（默认），行为与以前完全一致。

    为什么不默认落盘：评测要跑几百个 episode，每个都留一份世界状态只会把磁盘塞满，
    而且那些 run 过期即废。**落盘是给"要跨重启的调试与演示"用的，不是给批量评测用的。**
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, Run] = {}
        self.directory = Path(directory) if directory is not None else None
        # 启动时读不动的文件记在这里，而不是静默跳过：一个坏文件说明
        # 上一轮写过一半或有人手改坏了，这件事本身要被看见。
        self.load_errors: list[str] = []
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._load_all()

    # ---------- 生命周期 ----------

    def create(
        self, twins: list[str], ttl_seconds: int, strict_tokens: bool = False
    ) -> Run:
        with self._lock:
            run_id = uuid.uuid4().hex[:12]
            run = Run(
                run_id=run_id,
                twins=list(twins),
                ttl_seconds=ttl_seconds,
                created_at=time.time(),
            )
            # 每个 run 初始就带一个 shared 命名空间
            run.state["shared"] = {"people": {}}
            # run-scoped 凭据：与 run_id 绑定，用来让"只替换 base URL 与密钥"
            # 这套接入方式成立。真实产品同样在 provision 时下发凭据，
            # 否则 agent 只能靠猜或者改双份配置。
            run.extras["credentials"] = {
                "stripe": f"sk_test_twin_{run_id}",
                "slack": f"xoxb-twin-{run_id}",
                "github": f"ghp_twin{run_id}",
                # 后加的 provider 也必须有 run-scoped 凭据，否则"只替换 base URL
                # 与密钥"这套接入承诺对它们不成立
                "jira": f"jira-twin-{run_id}",
                "linear": f"lin_api_twin_{run_id}",
                "notion": f"ntn_twin_{run_id}",
                "hubspot": f"pat-hi-twin-{run_id}",
                # Salesforce 的 session token 长这样：`00D...!AQ4AQ...`。
                # 前缀不是装饰——jsforce 与不少客户端会按 `!` 分段解析它。
                "salesforce": f"00Dtw1n000000AAA!AQ4AQtwin{run_id}",
                # Google OAuth access token 形状（ya29. 开头）。gmail/calendar/drive
                # 共用同一个 token —— 真 Google 也是这样，一个 token 覆盖多个 API。
                "gmail": f"ya29.twin-{run_id}",
                "calendar": f"ya29.twin-{run_id}",
                "drive": f"ya29.twin-{run_id}",
            }
            # 默认关闭。开着的含义见 Gateway._strict_credential_check：
            # token 必须逐字等于上面这一张表里的值。
            run.extras["strict_tokens"] = bool(strict_tokens)
            self._runs[run_id] = run
            self._save(run)
            return run

    def get(self, run_id: str, *, allow_expired: bool = False) -> Run:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise RunNotFound(run_id)
            if not allow_expired and run.expired():
                raise RunExpired(run_id)
            return run

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)
            path = self._path(run_id)
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def save(self, run: Run) -> None:
        """外部（网关）改完 state 后调它落盘。没配 directory 时是空操作。"""
        self._save(run)

    def all_runs(self) -> list[Run]:
        with self._lock:
            return list(self._runs.values())

    def gc(self) -> int:
        """清掉过期的 run。返回清掉的数量。故意不自动调用——过期要能被观测到(410)，
        立刻删掉会让过期变成 404，测不出 TTL 逻辑。"""
        with self._lock:
            dead = [rid for rid, r in self._runs.items() if r.expired()]
            for rid in dead:
                del self._runs[rid]
            return len(dead)

    # ---------- 快照 ----------

    def snapshot(self, run_id: str) -> None:
        """把当前状态冻结成 baseline。provision 结束时调用一次。"""
        with self._lock:
            run = self.get(run_id)
            run.baseline = copy.deepcopy(run.state)
            self._save(run)

    def reset(self, run_id: str) -> None:
        """回到 baseline。注意 stub_hits 不清——它是观测数据不是业务状态。"""
        with self._lock:
            run = self.get(run_id)
            run.state = copy.deepcopy(run.baseline)
            run.created_at = time.time()  # reset 重新计时，等同"再来一次"
            self._save(run)

    def extend(self, run_id: str, ttl_seconds: int) -> None:
        with self._lock:
            run = self.get(run_id)
            run.ttl_seconds = ttl_seconds
            run.created_at = time.time()
            self._save(run)

    def lock(self, run_id: str) -> None:
        with self._lock:
            run = self.get(run_id)
            run.locked = True
            self._save(run)

    # ---------- 共享实体层 ----------

    def link_person(self, run_id: str, email: str, system: str, entity_id: str) -> None:
        """把 (system, entity_id) 挂到 email 这个自然人身上。两端都调，就自动对齐。

        同一个 email 在同一个系统里可能有多个档案 —— 那正是"重复账单""重复线索"
        这类题要考的东西。所以这里 **first-wins + 记 duplicates**，不覆盖：
        覆盖会让共享实体层在 seed 阶段就把题目要考的不一致抹平，
        agent 再去查时两个系统都只看到一个档案，题目变得不可解而没人知道。
        """
        with self._lock:
            run = self.get(run_id)
            people = run.state["shared"].setdefault("people", {})
            person = people.setdefault(
                email.lower(), {"email": email.lower(), "links": {}, "duplicates": {}}
            )
            links = person["links"]
            dups = person.setdefault("duplicates", {})
            if system not in links:
                links[system] = entity_id
            elif links[system] != entity_id:
                bucket = dups.setdefault(system, [])
                if entity_id not in bucket:
                    bucket.append(entity_id)
            self._save(run)

    def person_for(self, run_id: str, email: str) -> dict[str, Any] | None:
        with self._lock:
            run = self.get(run_id)
            return run.state["shared"].get("people", {}).get(email.lower())

    # ---------- stub 观测 ----------

    def record_stub(
        self, run_id: str, provider: str, endpoint: str, planned: bool = False
    ) -> None:
        """记一次 stub 命中。

        `planned=True` 表示 twin 主动声明过"这个端点我不打算实现"（见
        `Twin.declared_stubs`）。两类命中都算 stub（因为返回的都是假数据），
        但报表必须能拆开：一类是"还欠着的活"，另一类是"画了线的边界"。
        混在一起看，就会把已经想清楚的取舍当成未完成的工程量。
        """
        with self._lock:
            run = self.get(run_id, allow_expired=True)
            hits = run.stub_hits.setdefault(provider, {})
            hits[endpoint] = hits.get(endpoint, 0) + 1
            if planned:
                dh = run.declared_stub_hits.setdefault(provider, {})
                dh[endpoint] = dh.get(endpoint, 0) + 1
            run.stub_recent.append(
                {
                    "at": time.time(),
                    "provider": provider,
                    "endpoint": endpoint,
                    "planned": planned,
                }
            )
            # 只留最近 200 条：这是观测流水不是审计日志，无限增长没有收益
            if len(run.stub_recent) > 200:
                del run.stub_recent[:-200]
            self._save(run)

    def record_absent(self, run_id: str, provider: str, endpoint: str) -> None:
        """记一次"真实 API 也没有这个端点"。

        与 stub 分开记的理由见 `Run.absent_hits` 的注释：这是 agent 的错，
        不是环境的债。合成一个数会让人得出"还欠 70 个端点"的错误结论。
        """
        with self._lock:
            run = self.get(run_id, allow_expired=True)
            hits = run.absent_hits.setdefault(provider, {})
            hits[endpoint] = hits.get(endpoint, 0) + 1
            self._save(run)

    def stub_report(self, run_id: str, provider: str | None = None) -> dict[str, Any]:
        with self._lock:
            run = self.get(run_id, allow_expired=True)
            source = (
                {provider: run.stub_hits.get(provider, {})}
                if provider
                else dict(run.stub_hits)
            )
            declared_source = (
                {provider: run.declared_stub_hits.get(provider, {})}
                if provider
                else dict(run.declared_stub_hits)
            )
            total = sum(sum(h.values()) for h in source.values())
            declared_total = sum(sum(h.values()) for h in declared_source.values())
            declared_eps = sorted({ep for h in declared_source.values() for ep in h})
            merged: dict[str, int] = {}
            for hits in source.values():
                for ep, n in hits.items():
                    merged[ep] = merged.get(ep, 0) + n
            recent = [
                r for r in run.stub_recent if provider is None or r["provider"] == provider
            ][-20:]
            absent_source = (
                {provider: run.absent_hits.get(provider, {})}
                if provider
                else dict(run.absent_hits)
            )
            absent_merged: dict[str, int] = {}
            for hits in absent_source.values():
                for ep, n in hits.items():
                    absent_merged[ep] = absent_merged.get(ep, 0) + n
            return {
                "total_hits": total,
                "absent_hits": sum(absent_merged.values()),
                "absent_endpoints": [
                    {"endpoint": ep, "hits": n}
                    for ep, n in sorted(absent_merged.items(), key=lambda kv: -kv[1])
                ],
                "unique_endpoints": len(merged),
                # 拆成"欠着的"与"说过不做的"。两列必须都在，否则报表会把
                # 声明的边界算成欠账，或反过来把欠账洗成边界。
                "unplanned_hits": total - declared_total,
                "declared_hits": declared_total,
                "declared_endpoints": declared_eps,
                "summary": [
                    {"endpoint": ep, "hits": n}
                    for ep, n in sorted(merged.items(), key=lambda kv: -kv[1])
                ],
                "recent": list(reversed(recent)),
            }

    def clear_stub_hits(self, run_id: str) -> None:
        with self._lock:
            run = self.get(run_id, allow_expired=True)
            run.stub_hits.clear()
            run.declared_stub_hits.clear()
            run.absent_hits.clear()
            run.stub_recent.clear()
            self._save(run)

    # ---------- 落盘 ----------

    def _path(self, run_id: str) -> Path | None:
        if self.directory is None:
            return None
        return self.directory / f"{run_id}.json"

    def _save(self, run: Run) -> None:
        """原子写 + 回读校验 + 跨进程互斥 + 乐观版本检查。

        为什么不是直接 write_text：写到一半崩掉会留下一个**语法上看起来没问题**
        的半个 JSON，下次启动时要么整个服务起不来，要么（更糟）读到一份缺了一半
        状态的世界，而 agent 会把"这些人怎么不见了"当成真实现象。

        为什么还要 flock：同一目录可能被两个进程打开（重放调试、两个 worker 打
        同一个 run）。没有锁时两个 os.replace 互相覆盖，最后一个赢，输的那次
        改动消失得无声无息。锁只保证"读盘校验 + 替换"这一小段是串行的。

        为什么还要比版本号：锁只解决"同时写"，解决不了"我先读、你写、我再写"。
        那种情况下我手里的 world 已经是旧的，直接覆盖等于把你那次操作删掉。
        """
        path = self._path(run.run_id)
        if path is None:
            return
        lock_path = path.with_suffix(".lock")
        with open(lock_path, "a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                disk_version = 0
                if path.exists():
                    try:
                        disk_version = int(
                            json.loads(path.read_text(encoding="utf-8")).get("version") or 0
                        )
                    except Exception:  # 坏文件：当成第 0 版，让它被这次写覆盖掉
                        disk_version = 0
                if disk_version != run.loaded_version:
                    raise StaleRunVersion(
                        f"run {run.run_id} 已被其它进程改到 v{disk_version}，"
                        f"本次改动基于 v{run.loaded_version}；请重新读取后再改"
                    )
                run.version = disk_version + 1
                run.loaded_version = run.version
                tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
                data = json.dumps(run.to_dict(), ensure_ascii=False)
                tmp.write_text(data, encoding="utf-8")
                json.loads(tmp.read_text(encoding="utf-8"))  # 回读校验：坏 JSON 不许过
                os.replace(tmp, path)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_all(self) -> None:
        assert self.directory is not None
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                run = Run.from_dict(payload)
            except Exception as exc:  # noqa: BLE001 - 一个坏文件不该让整个服务起不来
                self.load_errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            self._runs[run.run_id] = run
