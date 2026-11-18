""" Scenario：一次可复现世界状态的配方。

三段结构，对应 Arga 的三个正交维度：
  seed          —— 世界里有什么（数据）
  capabilities  —— 世界允许什么（权限、档位、配额的收窄）
  events        —— 世界里已经发生过什么（历史消息、既有工单）

capabilities 是这套东西最有价值的部分：真实的 agent 失败大多不是因为数据不对，
而是因为**它以为自己的权限比实际大**。能把 scope 收窄成 scenario 的一个字段，
才测得出"权限不足时 agent 会不会降级"。
"""

from __future__ import annotations

import copy
import json
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import Run, Store, new_id
from .twins.base import TwinRegistry


@dataclass
class Scenario:
    id: str
    name: str
    seed: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    events: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    generation_mode: str = "fast"
    # 域名（IT support / CRM & outreach / ...）。分域统计要靠它，而分域统计是
    # "这套 benchmark 在哪个方向最弱"的唯一来源 —— 所以它必须过 API，不能只活在 JSON 文件里。
    domain: str = ""
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "seed": self.seed,
            "capabilities": self.capabilities,
            "events": self.events,
            "prompt": self.prompt,
            "generation_mode": self.generation_mode,
            "domain": self.domain,
            "created": self.created,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Scenario":
        return Scenario(
            id=d.get("id") or uuid.uuid4().hex[:12],
            name=d.get("name") or "untitled",
            seed=d.get("seed") or {},
            capabilities=d.get("capabilities") or {},
            events=d.get("events") or {},
            prompt=d.get("prompt") or "",
            generation_mode=d.get("generation_mode") or "fast",
            domain=d.get("domain") or "",
            created=d.get("created") or time.time(),
        )


class ScenarioStore:
    """场景存 JSON 文件，跟着仓库走——seed 要能进 code review，不能只活在数据库里。"""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.dir / f"{sid}.json"

    def save(self, scenario: Scenario) -> Scenario:
        if not scenario.id:
            scenario.id = uuid.uuid4().hex[:12]
        self._path(scenario.id).write_text(
            json.dumps(scenario.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return scenario

    def get(self, sid: str) -> Scenario | None:
        p = self._path(sid)
        if not p.exists():
            return None
        return Scenario.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def list(self) -> list[Scenario]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(Scenario.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except json.JSONDecodeError:
                continue
        return out

    def delete(self, sid: str) -> bool:
        p = self._path(sid)
        if p.exists():
            p.unlink()
            return True
        return False


# ---------- 从自然语言 prompt 生成 seed（无 LLM 的规则版） ----------

def generate_from_prompt(prompt: str, twins: list[str]) -> dict[str, Any]:
    """把一句话变成一个粗糙但可用的 seed。

    这里刻意不接 LLM：一个不确定的生成器会让"可复现"变成空话。
    需要更强的生成能力时，让 agent 自己写 seed JSON 再 import——
    生成是 agent 的事，复现是环境的事，两者不该混在同一个组件里。
    """
    text = prompt.lower()
    seed: dict[str, Any] = {}

    if "stripe" in twins:
        stripe_seed: dict[str, Any] = {
            "customers": [],
            "products": [],
        }
        if "subscription" in text or "订阅" in prompt:
            stripe_seed["products"].append(
                {"name": "Pro Plan", "prices": [{"unit_amount": 4900, "recurring": {"interval": "month"}}]}
            )
        if "duplicate" in text or "重复" in prompt:
            stripe_seed["customers"] = [
                {"email": "morgan@retail.example", "name": "Morgan Retail"},
                {"email": "morgan@retail.example", "name": "Morgan Retail Store"},
            ]
        elif "customer" in text:
            stripe_seed["customers"].append({"email": "customer@example.com", "name": "Example Customer"})
        seed["stripe"] = stripe_seed

    if "slack" in twins:
        channels = []
        for token in prompt.replace(",", " ").split():
            if token.startswith("#") and len(token) > 1:
                channels.append({"name": token[1:]})
        if not channels:
            channels = [{"name": "general"}]
        seed["slack"] = {
            "channels": channels,
            "users": [],
            "messages": [],
        }

    return seed


# ---------- 把 scenario 落到 run 上 ----------

def apply_scenario(
    scenario: Scenario,
    run: Run,
    store: Store,
    registry: TwinRegistry,
) -> None:
    """直接写 run.state，不走 HTTP。

    为什么不走 HTTP：seed 要能造出"你没法通过 API 造出来的状态"
    （比如 90 天前的消息、被锁定的账号、跨系统不一致的 email）。
    通过 API 造的话，能力边界会被 API 卡住，而我们要的恰恰是越过它。
    """
    base = scenario.seed or {}
    events = scenario.events or {}

    for provider in run.twins:
        twin = registry.get(provider)
        run.state.setdefault(provider, twin.initial_state())

    # ---- stripe ----
    if "stripe" in run.twins and "stripe" in base:
        st = run.state["stripe"]
        spec = base["stripe"]
        for c in spec.get("customers", []):
            # 允许 seed 显式给 id：判据要能在 scenario 里写死 id，随机 id 会让断言无法预先表达
            cid = c.get("id") or new_id("cus_")
            st["customers"][cid] = {
                "id": cid,
                "object": "customer",
                "created": int(time.time()),
                "livemode": False,
                "email": c.get("email"),
                "name": c.get("name"),
                "description": c.get("description"),
                "metadata": c.get("metadata") or {},
                "delinquent": False,
                "currency": c.get("currency", "usd"),
                "balance": 0,
            }
            if c.get("email"):
                store.link_person(run.run_id, c["email"], "stripe", cid)

        for p in spec.get("products", []):
            pid = p.get("id") or new_id("prod_")
            st["products"][pid] = {
                "id": pid,
                "object": "product",
                "created": int(time.time()),
                "livemode": False,
                "name": p.get("name"),
                "description": p.get("description"),
                "active": True,
                "metadata": p.get("metadata") or {},
            }
            for pr in p.get("prices", []):
                price_id = pr.get("id") or new_id("price_")
                st["prices"][price_id] = {
                    "id": price_id,
                    "object": "price",
                    "created": int(time.time()),
                    "livemode": False,
                    "product": pid,
                    "currency": pr.get("currency", "usd"),
                    "unit_amount": pr.get("unit_amount"),
                    "recurring": pr.get("recurring"),
                    "active": True,
                }

        for ch in spec.get("charges", []):
            ch_id = ch.get("id") or new_id("ch_")
            st["charges"][ch_id] = {
                "id": ch_id,
                "object": "charge",
                "created": int(time.time()),
                "livemode": False,
                "amount": ch.get("amount", 0),
                "currency": ch.get("currency", "usd"),
                "customer": ch.get("customer"),
                "paid": True,
                "refunded": False,
                "amount_refunded": 0,
                "status": "succeeded",
                # description 必须出现在 GET 响应里：crm-17 这类题要求模型
                # "读扣款记录区分哪笔该退"，不给 description 等于让它猜。
                # （2026-11-16 spare 扫描发现：crm-17 十二轮全 UNSAFE，模型
                # 只能按 customer id 猜，把 trial 转化首扣退掉。）
                "description": ch.get("description"),
            }

        for sb in spec.get("subscriptions", []):
            sid = sb.get("id") or new_id("sub_")
            st["subscriptions"][sid] = {
                "id": sid,
                "object": "subscription",
                "created": int(time.time()),
                "livemode": False,
                "customer": sb.get("customer"),
                "items": {"object": "list", "data": sb.get("items") or []},
                "status": sb.get("status", "active"),
                "cancel_at_period_end": bool(sb.get("cancel_at_period_end")),
                "current_period_end": int(time.time()) + int(sb.get("period_end_in", 30 * 86400)),
                "metadata": sb.get("metadata") or {},
            }

    # ---- slack ----
    if "slack" in run.twins and "slack" in base:
        sl = run.state["slack"]
        spec = base["slack"]
        name_to_cid: dict[str, str] = {}
        for ch in spec.get("channels", []):
            cid = ch.get("id") or new_id("C", 10).upper()
            sl["channels"][cid] = {
                "id": cid,
                "name": ch.get("name") or "general",
                "is_channel": True,
                "is_im": False,
                "is_private": bool(ch.get("is_private")),
                "created": int(time.time()),
                "num_members": ch.get("num_members", 2),
                "topic": {"value": ch.get("topic") or ""},
            }
            name_to_cid[sl["channels"][cid]["name"]] = cid

        email_to_uid: dict[str, str] = {}
        for u in spec.get("users", []):
            uid = u.get("id") or new_id("U", 10).upper()
            email = u.get("email")
            sl["users"][uid] = {
                "id": uid,
                "team_id": "T0TWIN0001",
                "name": u.get("name") or (email or "user").split("@")[0],
                "real_name": u.get("real_name") or (email or "user").split("@")[0],
                "profile": {"email": email, "display_name": (email or "user").split("@")[0]},
                "deleted": False,
                "is_bot": False,
            }
            if email:
                email_to_uid[email.lower()] = uid
                store.link_person(run.run_id, email, "slack", uid)

        for m in spec.get("messages", []):
            ch_name = m.get("channel") or "general"
            cid = name_to_cid.get(ch_name) or next(iter(sl["channels"]), None)
            if not cid:
                continue
            uid = m.get("user_id") or email_to_uid.get((m.get("user_email") or "").lower(), "U0TWINBOT")
            # age_seconds 让 seed 能造假历史：free 档的 90 天窗口要靠它才测得出
            age = int(m.get("age_seconds") or 0)
            sl["next_ts"] += 1
            micro = sl["next_ts"]
            sl["messages"].append(
                {
                    "type": "message",
                    "ts": f"{micro // 1_000_000}.{micro % 1_000_000:06d}",
                    "text": m.get("text") or "",
                    "user": uid,
                    "bot_id": None,
                    "channel": cid,
                    "thread_ts": m.get("thread_ts"),
                    "blocks": m.get("blocks"),
                    "created": int(time.time()) - age,
                }
            )

    # ---- github ----
    if "github" in run.twins and "github" in base:
        gh = run.state["github"]
        spec = base["github"]

        for r in spec.get("repos", []):
            full = r.get("name") or r.get("full_name")
            if not full:
                continue
            gh["repos"][full] = {
                "full_name": full,
                "name": full.split("/")[-1],
                "private": bool(r.get("private")),
                "default_branch": r.get("default_branch") or "main",
                "stargazers_count": r.get("stargazers_count", 0),
                "description": r.get("description"),
            }
            gh.setdefault("branches", {}).setdefault(full, {})
            gh.setdefault("issues", {}).setdefault(full, {})
            gh.setdefault("pulls", {}).setdefault(full, {})
            gh.setdefault("contents", {}).setdefault(full, {})
            gh.setdefault("commits", {}).setdefault(full, [])
            gh.setdefault("comments", {}).setdefault(full, {})
            # 每个仓库都要先有这几个空容器，否则"没有 release / 没有部署"在状态里
            # 表现为**键不存在**（解析成 None），判据写成 count eq 0 会拿到 None 判 False
            # —— 于是"正确地什么都没做"被记成失败。真 GitHub 对空集合也是回 []。
            for bucket in ("deployments", "deployment_statuses", "actions_caches",
                           "actions_runs", "releases"):
                gh.setdefault(bucket, {}).setdefault(full, [])
            gh.setdefault("git_refs", {}).setdefault(full, {})

        for full, branches in (spec.get("branches") or {}).items():
            gh["branches"].setdefault(full, {}).update(branches)

        for c in spec.get("contents", []):
            full = c.get("repo")
            if not full or not c.get("path"):
                continue
            raw = c.get("content") or ""
            gh["contents"].setdefault(full, {})[c["path"]] = {
                "raw": raw,
                "sha": hashlib.sha1(raw.encode()).hexdigest(),
            }

        for i in spec.get("issues", []):
            full = i.get("repo")
            if not full:
                continue
            number = i.get("number") or 1
            gh["issues"].setdefault(full, {})[str(number)] = {
                "id": number * 1000,
                "number": number,
                "title": i.get("title") or "",
                "body": i.get("body") or "",
                "state": i.get("state") or "open",
                "labels": i.get("labels") or [],
                "assignee": i.get("assignee"),
                "user": {"login": i.get("author") or "reporter"},
                "created_at": int(time.time()) - int(i.get("age_seconds") or 0),
            }
            gh["next_number"] = max(gh.get("next_number") or 1, number + 1)

        for pr in spec.get("pulls", []):
            full = pr.get("repo")
            if not full:
                continue
            number = pr.get("number") or 1
            gh["pulls"].setdefault(full, {})[str(number)] = {
                "number": number,
                "title": pr.get("title") or "",
                "body": pr.get("body") or "",
                "state": pr.get("state") or "open",
                "head": {"ref": pr.get("head") or "feature"},
                "base": {"ref": pr.get("base") or "main"},
                "merged": bool(pr.get("merged")),
                "user": {"login": pr.get("author") or "contributor"},
                "created_at": int(time.time()) - int(pr.get("age_seconds") or 0),
            }
            gh["next_number"] = max(gh.get("next_number") or 1, number + 1)

        if spec.get("granted_scopes") is not None:
            gh["granted_scopes"] = list(spec["granted_scopes"])

        # 预置部署：不装载的话 deployments 永远是空 list，于是"给已有部署补状态"
        # 这类题唯一能过的路径变成"先自己建一个部署"——而那往往正是题面禁止的动作。
        for d in spec.get("deployments", []):
            full = d.get("repo")
            if not full:
                continue
            dep_id = int(d.get("id") or 0) or (1000 + len(gh["deployments"].get(full) or []) + 1)
            ref = d.get("ref") or "main"
            gh["deployments"].setdefault(full, []).append({
                "id": dep_id,
                "ref": ref,
                "sha": (gh.get("branches", {}).get(full) or {}).get(ref, "0" * 40),
                "environment": d.get("environment") or "production",
                "description": d.get("description") or "",
                "task": d.get("task") or "deploy",
                "payload": d.get("payload") or {},
                "created_at": int(time.time()) - int(d.get("age_seconds") or 0),
                "creator": {"login": "arga-bot"},
            })

    # ---- 能力收窄 ----
    caps = scenario.capabilities or {}
    if "slack" in run.twins and "slack" in caps:
        sl = run.state["slack"]
        sc = caps["slack"]
        if "granted_scopes" in sc:
            sl["granted_scopes"] = list(sc["granted_scopes"])
        if "tier" in sc:
            sl["tier"] = sc["tier"]
        if "disabled_scopes" in sc:
            granted = [s for s in sl["granted_scopes"] if s not in sc["disabled_scopes"]]
            sl["granted_scopes"] = granted

    # ---- 额外事件（例如已存在的工单） ----
    if events:
        run.state.setdefault("shared", {}).setdefault("events", []).extend(
            copy.deepcopy(events.get("items", []))
        )
