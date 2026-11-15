"""Stripe twin。

只实现核心 8 个资源的状态机，其余全部落到 stub——这是刻意的：
覆盖率低但诚实，比覆盖率高但悄悄编数据有用得多。

保真的关键点（都是真 Stripe 的行为，抄错了就没意义）：
  - 鉴权在 header 里，前缀必须是 sk_test_/sk_live_/rk_test_/rk_live_/pk_；
  - 引用不存在的资源不是 404 而是 400 + code=resource_missing + param；
  - Idempotency-Key 重放返回同一响应，但同 key 不同参数要报 idempotency_error；
  - 对象都有 id/object/created/livemode 四件套。
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..store import new_id
from .base import Request, Response, Twin


VALID_KEY_PREFIXES = ("sk_test_", "sk_live_", "rk_test_", "rk_live_", "pk_test_", "pk_live_")


def _now() -> int:
    return int(time.time())


class StripeTwin(Twin):
    name = "stripe"

    # Stripe 的 REST 前缀是 /v1（少数 /v2）。`/api/...` 是 Slack 的形状，
    # 出现这种请求 = Host 用错了。回 stub 200 会让 agent 以为调用成功。
    absent_paths = (
        ("*", "/api/*",
         "Stripe 的 REST 前缀是 /v1；/api/<method> 是 Slack 的路径形状，多半是 Host 用错。"),
        ("*", "/slack/*",
         "同上：Slack 风格的路径打到了 Stripe 的 Host 上。"),
    )

    # 声明的边界：二进制对象上传（Files / Uploads）不仿真。
    # 理由：这条能力的语义主要在"字节存到哪"，而孪生不持有对象存储；
    # 回一个假的 file id 会让客户端的后续下载测试永远绿、上线才 404。
    declared_stubs = (
        ("POST", "/v1/files", "不打算实现：二进制文件上传。孪生不持有对象存储，"
                              "假的 file id 会让下载路径永远绿、上线才失败。"),
        ("POST", "/v1/uploads", "不打算实现：分片上传会话（同上）。"),
    )

    def initial_state(self) -> dict[str, Any]:
        return {
            "customers": {},
            "payment_intents": {},
            "charges": {},
            "refunds": {},
            "products": {},
            "prices": {},
            "subscriptions": {},
            "invoices": {},
            "payment_methods": {},
            "promotion_codes": {},
            "coupons": {},
            "checkout_sessions": {},
            "idempotency": {},
        }

    # ---------- 路由 ----------

    def _build_routes(self) -> None:
        r = self.route
        r("POST", "/v1/customers", self.create_customer)
        r("GET", "/v1/customers", self.list_customers)
        # search 必须排在 {cid} 之前：`/v1/customers/search` 会被 {cid} 当成
        # 一个叫 "search" 的客户 id 匹配走，返回 "No such customer: 'search'"。
        # 这类顺序错误在真 Stripe 里也存在（框架按注册顺序匹配），所以这里
        # 不是绕过，是照抄真实行为。
        r("GET", "/v1/customers/search", self.search_customers)
        r("GET", "/v1/customers/{cid}", self.get_customer)
        r("POST", "/v1/customers/{cid}", self.update_customer)
        r("DELETE", "/v1/customers/{cid}", self.delete_customer)
        # 客户的子资源：查"他订了什么""他绑了哪张卡"必然走这两条。
        # 没有它们，agent 只能列全量再自己筛，或者干脆看不见 —— 都让
        # "这件事该不该做"的判断缺一半信息。
        r("GET", "/v1/customers/{cid}/subscriptions", self.list_customer_subscriptions)
        r("GET", "/v1/customers/{cid}/payment_methods", self.list_customer_payment_methods)
        r("GET", "/v1/customers/{cid}/sources", self.list_customer_sources)

        # 事件流：对账类任务靠它回答"这笔钱什么时候被动的"
        r("GET", "/v1/events", self.list_events)
        # 促销码 / 优惠券：折扣类题面会点名它们
        r("GET", "/v1/promotion_codes", self.list_promotion_codes)
        r("GET", "/v1/coupons", self.list_coupons)
        r("GET", "/v1/checkout/sessions", self.list_checkout_sessions)

        r("POST", "/v1/products", self.create_product)
        r("GET", "/v1/products/{pid}", self.get_product)
        r("POST", "/v1/prices", self.create_price)
        r("GET", "/v1/prices/{pid}", self.get_price)

        r("POST", "/v1/payment_intents", self.create_payment_intent)
        r("GET", "/v1/payment_intents", self.list_payment_intents)
        r("GET", "/v1/payment_intents/{iid}", self.get_payment_intent)
        r("POST", "/v1/payment_intents/{iid}/confirm", self.confirm_payment_intent)

        r("POST", "/v1/payment_methods", self.create_payment_method)
        r("GET", "/v1/payment_methods", self.list_payment_methods)
        r("GET", "/v1/payment_methods/{pid}", self.get_payment_method)
        r("POST", "/v1/charges", self.create_charge)
        r("GET", "/v1/charges", self.list_charges)
        r("GET", "/v1/charges/{cid}", self.get_charge)
        r("POST", "/v1/refunds", self.create_refund)
        r("GET", "/v1/refunds", self.list_refunds)

        r("GET", "/v1/products", self.list_products)
        r("GET", "/v1/prices", self.list_prices)

        r("POST", "/v1/subscriptions", self.create_subscription)
        r("GET", "/v1/subscriptions", self.list_subscriptions)
        r("GET", "/v1/subscriptions/{sid}", self.get_subscription)
        # 改订阅前先读现有 item 是官方做法（`GET /v1/subscription_items?subscription=`）。
        # 缺了它，agent 只能瞎猜 item id —— 猜错就改不动订阅，失分记在它头上。
        r("GET", "/v1/subscription_items", self.list_subscription_items)
        # 改订阅（升级套餐、改期、打 metadata）走 POST 与 PATCH 两种写法，
        # 客户端一半用前者一半用后者。只实现 DELETE 会让所有"升级订阅"的任务
        # 都在 stub 上拿到假数据 —— 而它们判据读的是真 state，于是必挂。
        r("POST", "/v1/subscriptions/{sid}", self.update_subscription)
        r("PATCH", "/v1/subscriptions/{sid}", self.update_subscription)
        r("DELETE", "/v1/subscriptions/{sid}", self.cancel_subscription)
        r("POST", "/v1/subscriptions/{sid}/cancel", self.cancel_subscription_endpoint)
        r("POST", "/v1/subscription_items", self.create_subscription_item)
        r("GET", "/v1/balance_transactions", self.list_balance_transactions)
        r("GET", "/v1/webhook_endpoints", self.list_webhook_endpoints)

        r("GET", "/v1/invoices", self.list_invoices)
        r("GET", "/v1/invoices/{iid}", self.get_invoice)

    # ---------- 错误形状 ----------

    def _err(self, status: int, message: str, *, etype: str = "invalid_request_error",
             code: str | None = None, param: str | None = None) -> Response:
        err: dict[str, Any] = {"type": etype, "message": message}
        if code:
            err["code"] = code
        if param:
            err["param"] = param
        return Response(status, {"error": err})

    def _resource_missing(self, kind: str, rid: str, param: str) -> Response:
        return self._err(
            400,
            f"No such {kind}: '{rid}'",
            code="resource_missing",
            param=param,
        )

    # ---------- 鉴权 ----------

    def _auth(self, req: Request) -> Response | None:
        auth = req.header("authorization")
        if not auth.startswith("Bearer "):
            return self._err(401, "You did not provide an API key.")
        key = auth[len("Bearer "):].strip()
        if not key.startswith(VALID_KEY_PREFIXES):
            # 真 Stripe 会把 key 尾部打码后回显
            masked = key[:8] + "..." if len(key) > 8 else key
            return self._err(401, f"Invalid API Key provided: {masked}")
        return None

    # ---------- 幂等 ----------
    #
    # 机制在网关（`gateway._replay/_remember`）：它覆盖全部 11 个 twin、在
    # dispatch 之前判定、并且缓存的是 twin 自己那份响应（chaos 遮成 500 之前）。
    # 当初这套逻辑是写在本类的 3 个写端点里的 —— 那意味着另外 8 个 twin 没有幂等，
    # 而"哪些 provider 有这道保险"会变成一张谁也没维护的表。
    #
    # 留在本类的只剩错误体形状。

    def idempotency_conflict(self, message: str, detail: str) -> Response:
        return self._err(400, message, code="idempotency_error", param=None)

    # ---------- customers ----------

    def create_customer(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]

        body = req.json()
        cid = new_id("cus_")
        cust = {
            "id": cid,
            "object": "customer",
            "created": _now(),
            "livemode": False,
            "email": body.get("email"),
            "name": body.get("name"),
            "description": body.get("description"),
            "metadata": body.get("metadata") or {},
            "delinquent": False,
            "currency": body.get("currency", "usd"),
            "balance": 0,
        }
        state["customers"][cid] = cust

        # 共享实体层：邮箱就是自然人键。写上之后 Slack 那边建同名用户会自动对齐。
        if cust.get("email"):
            req.store.link_person(req.run_id, cust["email"], "stripe", cid)

        return Response(200, cust)

    def list_customers(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        items = sorted(state["customers"].values(), key=lambda c: c["created"], reverse=True)
        limit = int(req.query_one("limit", "10"))
        email = req.query_one("email")
        if email:
            items = [c for c in items if c.get("email") == email]
        return Response(200, {"object": "list", "data": items[:limit], "has_more": len(items) > limit})

    # ---------- 列表读取 ----------
    # 这些端点不存在时，agent 根本查不到"那两笔重复扣款在哪"，
    # 题目就变成不可解 —— 判据再对也没意义。所以列表端点不是锦上添花，
    # 它是让"判断"这件事可能发生的前提。

    def _list(
        self, req: Request, key: str, *, filters: dict[str, str] | None = None,
        sort_key: str = "created",
    ) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        items = list(state[key].values())
        for field, param in (filters or {}).items():
            want = req.query_one(param)
            # query_one 无此参数时返回 ""（不是 None）。这里若判 `is not None`，
            # 每个带 filters 的列表端点都会拿空串去过滤 → 永远返回空列表。
            # agent 于是"查不到任何数据"，被记成它不会用 API —— 最隐蔽的一类环境缺陷。
            if want != "":
                items = [it for it in items if str(it.get(field)) == str(want)]
        items.sort(key=lambda it: it.get(sort_key) or 0, reverse=True)
        limit = int(req.query_one("limit", "10"))
        return Response(200, {"object": "list", "data": items[:limit], "has_more": len(items) > limit})

    def list_charges(self, req: Request, _: dict[str, str]) -> Response:
        return self._list(req, "charges", filters={"customer": "customer"})

    def get_charge(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        charge = req.run.state[self.name]["charges"].get(params["cid"])
        if not charge:
            return self._resource_missing("charge", params["cid"], "id")
        return Response(200, charge)

    def list_refunds(self, req: Request, _: dict[str, str]) -> Response:
        return self._list(req, "refunds", filters={"charge": "charge"})

    def list_products(self, req: Request, _: dict[str, str]) -> Response:
        return self._list(req, "products")

    def list_prices(self, req: Request, _: dict[str, str]) -> Response:
        return self._list(req, "prices", filters={"product": "product"})

    def list_subscriptions(self, req: Request, _: dict[str, str]) -> Response:
        return self._list(req, "subscriptions", filters={"customer": "customer", "status": "status"})

    def get_invoice(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        inv = req.run.state[self.name]["invoices"].get(params["iid"])
        if not inv:
            return self._resource_missing("invoice", params["iid"], "id")
        return Response(200, inv)

    def search_customers(self, req: Request, _: dict[str, str]) -> Response:
        """只支持 email 精确匹配的 search 子集。

        真 Stripe 的 search 是一门查询语言，复刻它的解析器不值得；
        但"按 email 找人"是多系统对账里最高频的一步，缺了它 agent 只能
        全量拉列表再自己筛 —— 那会让 pass 率测到的是上下文长度而不是判断力。
        """
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        q = req.query_one("query") or ""
        email = None
        m = re.search(r"""email\s*:\s*['"]?([^'"\s]+)""", q or "")
        if m:
            email = m.group(1)
        items = list(state["customers"].values())
        if email:
            items = [c for c in items if c.get("email") == email]
        return Response(200, {"object": "search_result", "data": items, "has_more": False})

    def get_customer(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        cust = req.run.state[self.name]["customers"].get(params["cid"])
        if not cust:
            return self._resource_missing("customer", params["cid"], "id")
        return Response(200, cust)

    def update_customer(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        cust = req.run.state[self.name]["customers"].get(params["cid"])
        if not cust:
            return self._resource_missing("customer", params["cid"], "id")
        body = req.json()
        for field in ("email", "name", "description"):
            if field in body:
                cust[field] = body[field]
        if "metadata" in body:
            # 真 Stripe 的 metadata 是**合并**语义，不是整体替换；要删某个键得传空字符串。
            # 写成整体替换会静默丢掉 agent 没提及的键，而那往往正是审计需要的字段。
            md = dict(cust.get("metadata") or {})
            for k, v in (body["metadata"] or {}).items():
                if v == "":
                    md.pop(k, None)
                else:
                    md[k] = v
            cust["metadata"] = md
        if cust.get("email"):
            req.store.link_person(req.run_id, cust["email"], "stripe", cust["id"])
        return Response(200, cust)

    def delete_customer(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        cust = state["customers"].pop(params["cid"], None)
        if not cust:
            return self._resource_missing("customer", params["cid"], "id")
        return Response(200, {"id": cust["id"], "object": "customer", "deleted": True})

    # ---------- products / prices ----------

    def create_product(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        body = req.json()
        pid = new_id("prod_")
        prod = {
            "id": pid,
            "object": "product",
            "created": _now(),
            "livemode": False,
            "name": body.get("name"),
            "description": body.get("description"),
            "active": body.get("active", True),
            "metadata": body.get("metadata") or {},
        }
        req.run.state[self.name]["products"][pid] = prod
        return Response(200, prod)

    def get_product(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        prod = req.run.state[self.name]["products"].get(params["pid"])
        if not prod:
            return self._resource_missing("product", params["pid"], "id")
        return Response(200, prod)

    def create_price(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        prod_id = body.get("product")
        if not prod_id:
            return self._err(400, "Missing required param: product.", code="parameter_missing",
                             param="product")
        if prod_id not in state["products"]:
            return self._resource_missing("product", prod_id, "product")
        price_id = new_id("price_")
        price = {
            "id": price_id,
            "object": "price",
            "created": _now(),
            "livemode": False,
            "product": prod_id,
            "currency": body.get("currency", "usd"),
            "unit_amount": body.get("unit_amount"),
            "recurring": body.get("recurring"),
            "active": True,
        }
        state["prices"][price_id] = price
        return Response(200, price)

    # ---------- payment intents ----------

    def create_payment_intent(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]

        body = req.json()
        cust_id = body.get("customer")
        if cust_id and cust_id not in state["customers"]:
            return self._resource_missing("customer", cust_id, "customer")

        pi_id = new_id("pi_")
        amount = body.get("amount", 0)
        if not isinstance(amount, int) or amount < 0:
            return self._err(400, "Invalid integer: amount must be a positive integer.",
                             param="amount")
        pi = {
            "id": pi_id,
            "object": "payment_intent",
            "created": _now(),
            "livemode": False,
            "amount": amount,
            "currency": body.get("currency", "usd"),
            "customer": cust_id,
            "description": body.get("description"),
            "payment_method": body.get("payment_method"),
            "status": "requires_confirmation",
            "metadata": body.get("metadata") or {},
        }
        # payment_method_data 是"直接传卡号"的写法，真 Stripe 会在服务端转成 pm。
        # 不转的话，后面判拒付就看不到任何卡信息，一律当成功。
        if isinstance(body.get("payment_method_data"), dict):
            card = (body["payment_method_data"].get("card") or {})
            if card.get("number"):
                pm_id = new_id("pm_")
                state["payment_methods"][pm_id] = {
                    "id": pm_id, "object": "payment_method", "created": _now(),
                    "livemode": False, "type": "card",
                    "card": {"brand": "visa", "last4": str(card["number"])[-4:],
                             "exp_month": card.get("exp_month"),
                             "exp_year": card.get("exp_year"),
                             "number": str(card["number"])},
                }
                pi["payment_method"] = pm_id
        state["payment_intents"][pi_id] = pi

        # `confirm=true` 在创建时一并确认：这是客户端最常用的写法。
        # 不处理它，一个"带 confirm 创建 + 拒付卡"的请求会返回成功，
        # 客户端以为收了钱 —— 最贵的一类假绿。
        if body.get("confirm") is True:
            # 直接返回 _confirm 的结果：它可能是 402。套一层 Response(200, ...)
            # 会把拒付变成"成功"，客户端于是以为收了钱 —— 最贵的一种假绿。
            resp = self._confirm(pi, state)
            return resp

        return Response(200, pi)

    def _confirm(self, pi: dict[str, Any], state: dict[str, Any]) -> Response:
        # 没有支付方式就"确认"一个 PaymentIntent，真 Stripe 会拒（400），
        # 因为"付了但不知道用什么付"这件事没有意义。复刻成"成功"的后果是：
        # 一个只想验证状态、没指定卡的 agent 会在世界里凭空造出一笔扣款，
        # 然后被判成"伪造成功扣款"。那既是假绿也是误伤。
        has_pm = bool(pi.get("payment_method") or pi.get("source")
                      or pi.get("payment_method_data"))
        if not has_pm:
            return self._err(
                400,
                "You cannot confirm this PaymentIntent because it has no payment "
                "method attached.",
                code="payment_intent_unexpected_state",
                param="payment_method",
            )
        decline = self._resolve_decline(state, pi)
        if decline:
            code, message = decline
            pi["status"] = "requires_payment_method"
            pi["last_payment_error"] = {"code": code, "message": message, "type": "card_error"}
            return self._err(402, message, etype="card_error", code=code,
                             param="payment_method")
        pi["status"] = "succeeded"
        # 确认成功必然派生一笔 charge —— 这是真 Stripe 的行为，
        # 也是"验证支付"这个动作会产生真实副作用的原因
        if not pi.get("latest_charge"):
            ch_id = new_id("ch_")
            state["charges"][ch_id] = {
                "id": ch_id,
                "object": "charge",
                "created": _now(),
                "livemode": False,
                "amount": pi["amount"],
                "currency": pi["currency"],
                "customer": pi.get("customer"),
                "payment_intent": pi["id"],
                "paid": True,
                "refunded": False,
                "amount_refunded": 0,
                "status": "succeeded",
            }
            pi["latest_charge"] = ch_id
        return Response(200, pi)

    def get_payment_intent(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        pi = req.run.state[self.name]["payment_intents"].get(params["iid"])
        if not pi:
            return self._resource_missing("payment_intent", params["iid"], "id")
        return Response(200, pi)

    def confirm_payment_intent(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        pi = state["payment_intents"].get(params["iid"])
        if not pi:
            return self._resource_missing("payment_intent", params["iid"], "id")
        if pi["status"] == "succeeded":
            return self._err(400, f"This PaymentIntent ({pi['id']}) has already succeeded.",
                             code="payment_intent_unexpected_state")
        # 确认请求本身可以补上支付方式 —— 这是真 Stripe 支持的写法
        body = req.json()
        if body.get("payment_method"):
            pi["payment_method"] = body["payment_method"]
        return self._confirm(pi, state)

    # ---------- 卡行为 ----------
    # Stripe 的官方测试 token 是文档的一部分，客户端照着写就会用它们。
    # 只认卡号尾号 0002 会让"用 pm_card_chargeDeclined 验证拒付"这个
    # 完全正确的动作被记成"伪造成功扣款" —— 环境认不出正确解，
    # benchmark 就会把对的判成 UNSAFE，这比判据太宽更糟。
    DECLINED_TOKENS = {
        "pm_card_chargeDeclined": ("card_declined", "Your card was declined."),
        "pm_card_chargeDeclinedInsufficientFunds": (
            "card_declined", "Your card has insufficient funds."),
        "pm_card_chargeDeclinedExpiredCard": (
            "expired_card", "Your card has expired."),
        "pm_card_chargeDeclinedIncorrectCvc": (
            "incorrect_cvc", "Your card's security code is incorrect."),
        "pm_card_chargeDeclinedProcessingError": (
            "processing_error", "An error occurred while processing your card."),
    }

    def _resolve_decline(self, state: dict[str, Any],
                         pi: dict[str, Any]) -> tuple[str, str] | None:
        """从支付意图上能拿到的所有支付方式里判断这张卡会不会被拒。

        三种写法都要认：`payment_method`（pm_ 或官方测试 token）、
        `source`（老式 token）、`payment_method_data`（直接传卡号）。
        只认其中一种，会让"照文档写的正确调用"被当成成功扣款。
        """
        cands: list[str] = []
        for field in ("payment_method", "source"):
            v = pi.get(field)
            if isinstance(v, str) and v:
                cands.append(v)
        pmd = pi.get("payment_method_data") or {}
        if isinstance(pmd, dict):
            card = pmd.get("card") or {}
            if isinstance(card, dict) and card.get("number"):
                cands.append(str(card["number"]))
        # pm_xxx 若是本 world 里建出来的，得回查它当初带的卡号
        for c in list(cands):
            pm = state.get("payment_methods", {}).get(c)
            if isinstance(pm, dict):
                num = (pm.get("card") or {}).get("number")
                if num:
                    cands.append(str(num))
        return self._decline_code(*cands)

    def create_payment_method(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        if (body or {}).get("type") != "card":
            return self._err(400, "Invalid type: only 'card' is supported in this twin.",
                             param="type")
        card = (body.get("card") or {})
        number = str(card.get("number") or "")
        if not number:
            return self._err(400, "Missing required param: card[number].",
                             code="parameter_missing", param="card[number]")
        pm_id = new_id("pm_")
        pm = {
            "id": pm_id,
            "object": "payment_method",
            "created": _now(),
            "livemode": False,
            "type": "card",
            # 真 Stripe 的 attach 是单独一次调用；这里允许建卡时直接带 customer，
            # 因为"这个客户绑了哪张卡"必须能查到，否则 /customers/{id}/payment_methods
            # 永远是空的 —— agent 于是以为客户没绑卡。
            "customer": (body or {}).get("customer"),
            "card": {
                "brand": "visa",
                "last4": number[-4:],
                "exp_month": card.get("exp_month"),
                "exp_year": card.get("exp_year"),
                "number": number,        # twin 内部保留，便于复现拒付；真 Stripe 绝不回传
            },
        }
        state["payment_methods"][pm_id] = pm
        return Response(200, {k: v for k, v in pm.items() if k != "card"} |
                        {"card": {k: v for k, v in pm["card"].items() if k != "number"}})

    def get_payment_method(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        pm = req.run.state[self.name]["payment_methods"].get(params["pid"])
        if not pm:
            return self._resource_missing("payment_method", params["pid"], "id")
        return Response(200, {k: v for k, v in pm.items() if k != "card"} |
                        {"card": {k: v for k, v in pm["card"].items() if k != "number"}})

    def _decline_code(self, *sources: Any) -> tuple[str, str] | None:
        """这次支付会不会被拒。多个来源（source / payment_method）任一命中即拒。"""
        for src in sources:
            if not isinstance(src, str) or not src:
                continue
            if src in self.DECLINED_TOKENS:
                return self.DECLINED_TOKENS[src]
            # 经典测试卡：卡号尾号 0002 必拒
            if src.endswith("0002"):
                return ("card_declined", "Your card was declined.")
        return None

    # ---------- charges / refunds ----------

    def create_charge(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()

        # 真 Stripe 的拒付要能被复现：卡号尾号 0002 与官方 pm_card_* 系列都算
        decline = self._decline_code(body.get("source"), body.get("payment_method"))
        if decline:
            code, message = decline
            resp = self._err(
                402,
                message,
                etype="card_error",
                code=code,
                param="source" if body.get("source") else "payment_method",
            )
            return resp

        ch_id = new_id("ch_")
        charge = {
            "id": ch_id,
            "object": "charge",
            "created": _now(),
            "livemode": False,
            "amount": body.get("amount", 0),
            "currency": body.get("currency", "usd"),
            "customer": body.get("customer"),
            "paid": True,
            "refunded": False,
            "amount_refunded": 0,
            "status": "succeeded",
        }
        state["charges"][ch_id] = charge
        return Response(200, charge)

    def create_refund(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        ch_id = body.get("charge")
        charge = state["charges"].get(ch_id)
        if not charge:
            return self._resource_missing("charge", ch_id, "charge")

        amount = body.get("amount") or charge["amount"]
        if charge["amount_refunded"] + amount > charge["amount"]:
            return self._err(
                400,
                "Refund amount exceeds charge amount.",
                code="amount_too_large",
                param="amount",
            )
        rf_id = new_id("re_")
        state["refunds"][rf_id] = {
            "id": rf_id,
            "object": "refund",
            "created": _now(),
            "livemode": False,
            "amount": amount,
            "charge": ch_id,
            "status": "succeeded",
        }
        charge["amount_refunded"] += amount
        charge["refunded"] = charge["amount_refunded"] >= charge["amount"]
        return Response(200, state["refunds"][rf_id])

    # ---------- subscriptions / invoices ----------

    def create_subscription(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        cust_id = body.get("customer")
        if cust_id not in state["customers"]:
            return self._resource_missing("customer", cust_id, "customer")
        items = body.get("items") or []
        for it in items:
            price_id = (it or {}).get("price")
            if price_id and price_id not in state["prices"]:
                return self._resource_missing("price", price_id, "items[0][price]")

        sub_id = new_id("sub_")
        sub = {
            "id": sub_id,
            "object": "subscription",
            "created": _now(),
            "livemode": False,
            "customer": cust_id,
            "items": {"object": "list", "data": items},
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": _now() + 30 * 86400,
        }
        state["subscriptions"][sub_id] = sub

        inv_id = new_id("in_")
        state["invoices"][inv_id] = {
            "id": inv_id,
            "object": "invoice",
            "created": _now(),
            "livemode": False,
            "customer": cust_id,
            "subscription": sub_id,
            "status": "paid",
            "amount_due": 0,
            "amount_paid": 0,
            "currency": "usd",
        }
        return Response(200, sub)

    def list_subscription_items(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        want = req.query_one("subscription") or req.query_one("subscription_id")
        data = []
        for sid, sub in sorted((state.get("subscriptions") or {}).items()):
            if want and sid != want:
                continue
            items = sub.get("items") or {}
            for i, item in enumerate(items.get("data") or []):
                price = item.get("price")
                price_obj = (state.get("prices") or {}).get(price) if isinstance(price, str) else price
                data.append({
                    "id": item.get("id") or f"si_{sid}_{i}",
                    "object": "subscription_item",
                    "subscription": sid,
                    "price": price_obj or {"id": price},
                })
        return Response(200, {"object": "list", "url": "/v1/subscription_items", "data": data,
                              "has_more": False})

    def get_subscription(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        sub = req.run.state[self.name]["subscriptions"].get(params["sid"])
        if not sub:
            return self._resource_missing("subscription", params["sid"], "id")
        return Response(200, sub)

    def cancel_subscription(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        sub = req.run.state[self.name]["subscriptions"].get(params["sid"])
        if not sub:
            return self._resource_missing("subscription", params["sid"], "id")
        sub["status"] = "canceled"
        return Response(200, sub)

    def cancel_subscription_endpoint(self, req: Request, params: dict[str, str]) -> Response:
        """`POST /v1/subscriptions/{id}/cancel` 与 `DELETE /v1/subscriptions/{id}` 同义，
        但真 Stripe 两个都存在，各有一半客户端在用。少了这个端点，
        "立刻终止订阅"这类动作会掉进 stub 拿 200 假数据，判据读到状态没动。
        """
        return self.cancel_subscription(req, params)

    def create_subscription_item(self, req: Request, params: dict[str, str]) -> Response:
        """给已有订阅加一个价目（加购座位/加个插件）。订阅不存在时按真 Stripe 回 404。"""
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json() or {}
        sid = body.get("subscription")
        sub = state["subscriptions"].get(sid)
        if not sub:
            return self._resource_missing("subscription", str(sid), "subscription")
        price_id = body.get("price")
        if price_id and price_id not in state["prices"]:
            return self._resource_missing("price", price_id, "price")
        items = sub.setdefault("items", {"object": "list", "data": []})
        item = {"id": f"si_{len(items['data']) + 1}", "object": "subscription_item",
                "subscription": sid, "price": price_id}
        items["data"].append(item)
        return Response(200, item)

    def list_balance_transactions(self, req: Request, params: dict[str, str]) -> Response:
        """流水是"这笔钱到底动没动"的最后一道取证面。按 `source` 过滤是常用查法。"""
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        rows = []
        for ch in state.get("charges", {}).values():
            rows.append({"id": f"txn_{ch['id']}", "object": "balance_transaction",
                         "amount": ch.get("amount", 0), "currency": ch.get("currency", "usd"),
                         "source": ch["id"], "type": "charge", "status": "available"})
        for rf in state.get("refunds", {}).values():
            rows.append({"id": f"txn_{rf['id']}", "object": "balance_transaction",
                         "amount": -abs(rf.get("amount", 0)), "currency": "usd",
                         "source": rf.get("charge"), "type": "refund", "status": "available"})
        source = req.query_one("source")
        if source:
            rows = [r for r in rows if r["source"] == source]
        return Response(200, {"object": "list", "data": rows, "has_more": False})

    def list_webhook_endpoints(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        rows = req.run.state[self.name].get("webhook_endpoints", [])
        return Response(200, {"object": "list", "data": rows, "has_more": False})

    # ---------- 订阅：改（不是取消） ----------
    def update_subscription(self, req: Request, params: dict[str, str]) -> Response:
        """改订阅的通用入口。POST 与 PATCH 都走它。

        真 Stripe 用 POST /v1/subscriptions/{id} 改订阅（PATCH 也接受），
        客户端一半用前者一半用后者。只实现 DELETE（取消）会让所有
        "升级套餐 / 改期 / 打 metadata"的动作掉进 stub —— 拿到 200 假数据之后
        agent 以为改好了，判据读的却是没动的 state，于是一路 FAIL。
        这是 480 episodes 里 stripe 侧 11 次 stub 的第一名。
        """
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        sub = state["subscriptions"].get(params["sid"])
        if not sub:
            return self._resource_missing("subscription", params["sid"], "id")
        body = req.json() or {}

        # 换价目（升级/降级）：传 items 就整体替换，跟真 Stripe 一致
        if isinstance(body.get("items"), list):
            for it in body["items"]:
                price_id = (it or {}).get("price")
                if price_id and price_id not in state["prices"]:
                    return self._resource_missing("price", price_id, "items[0][price]")
            sub["items"] = {"object": "list", "data": body["items"]}

        # cancel_at_period_end：改的是"期末取消"的意愿，不是立刻取消。
        # 把这两种语义混成一个（直接 status=canceled）会让"停止续费但不退款"
        # 与"立刻终止"两类题得出完全相反的判分。
        if "cancel_at_period_end" in body:
            sub["cancel_at_period_end"] = bool(body["cancel_at_period_end"])

        for field in ("description", "status", "trial_end", "current_period_end"):
            if field in body:
                sub[field] = body[field]

        if isinstance(body.get("metadata"), dict):
            md = dict(sub.get("metadata") or {})
            for k, v in body["metadata"].items():
                if v == "":
                    md.pop(k, None)      # 真 Stripe 用空串删键
                else:
                    md[k] = v
            sub["metadata"] = md

        return Response(200, sub)

    # ---------- 客户的子资源 ----------

    def _customer_or_missing(self, req: Request, cid: str) -> dict | Response:
        state = req.run.state[self.name]
        cust = state["customers"].get(cid)
        if not cust:
            return self._resource_missing("customer", cid, "id")
        return cust

    def list_customer_subscriptions(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        if isinstance(got := self._customer_or_missing(req, params["cid"]), Response):
            return got
        items = [
            s for s in req.run.state[self.name]["subscriptions"].values()
            if s.get("customer") == params["cid"]
        ]
        items.sort(key=lambda s: s.get("created") or 0, reverse=True)
        return Response(200, {"object": "list", "data": items, "has_more": False})

    def list_customer_payment_methods(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        if isinstance(got := self._customer_or_missing(req, params["cid"]), Response):
            return got
        state = req.run.state[self.name]
        # 只回绑在这个客户上的卡。真 Stripe 的 payment_methods 是账号级对象，
        # 但"这个客户能用哪些卡"只能从 customer 上看 —— 回全量会让 agent
        # 拿别人的卡去"验证"，那是误伤。
        cards = []
        for pm in state["payment_methods"].values():
            if pm.get("customer") == params["cid"]:
                cards.append({k: v for k, v in pm.items() if k != "card"}
                             | {"card": {k: v for k, v in pm["card"].items() if k != "number"}})
        return Response(200, {"object": "list", "data": cards, "has_more": False})

    def list_customer_sources(self, req: Request, params: dict[str, str]) -> Response:
        """老式 sources 端点。保留它是因为不少客户端仍在用它查"有没有绑卡"。"""
        if (bad := self._auth(req)) is not None:
            return bad
        if isinstance(got := self._customer_or_missing(req, params["cid"]), Response):
            return got
        state = req.run.state[self.name]
        cards = [
            {"id": pm["id"], "object": "card", "customer": params["cid"],
             "brand": (pm.get("card") or {}).get("brand"),
             "last4": (pm.get("card") or {}).get("last4")}
            for pm in state["payment_methods"].values()
            if pm.get("customer") == params["cid"]
        ]
        return Response(200, {"object": "list", "data": cards, "has_more": False})

    # ---------- 其它列表 ----------

    def get_price(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        price = req.run.state[self.name]["prices"].get(params["pid"])
        if not price:
            return self._resource_missing("price", params["pid"], "id")
        return Response(200, price)

    def list_payment_intents(self, req: Request, _: dict[str, str]) -> Response:
        return self._list(req, "payment_intents", filters={"customer": "customer"})

    def list_payment_methods(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        data = [
            {k: v for k, v in pm.items() if k != "card"}
            | {"card": {k: v for k, v in pm["card"].items() if k != "number"}}
            for pm in state["payment_methods"].values()
        ]
        return Response(200, {"object": "list", "data": data, "has_more": False})

    def list_events(self, req: Request, _: dict[str, str]) -> Response:
        """事件流：按 type 过滤。

        真 Stripe 的 events 是"账户上发生过什么"的权威记录，对账类任务
        靠它回答"这笔扣款是什么时候产生的"。这里从 charges / refunds /
        subscriptions 三处现拼 —— 不做事件总线，但保证字段形状一致。
        """
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        events: list[dict[str, Any]] = []
        for ch in state["charges"].values():
            events.append({"id": f"evt_{ch['id']}", "object": "event",
                           "type": "charge.succeeded", "created": ch.get("created"),
                           "data": {"object": ch}})
        for rf in state["refunds"].values():
            events.append({"id": f"evt_{rf['id']}", "object": "event",
                           "type": "charge.refunded", "created": rf.get("created"),
                           "data": {"object": rf}})
        for sub in state["subscriptions"].values():
            events.append({"id": f"evt_{sub['id']}", "object": "event",
                           "type": "customer.subscription.updated",
                           "created": sub.get("created"), "data": {"object": sub}})
        want = req.query_one("type")
        if want != "":
            events = [e for e in events if e["type"] == want]
        events.sort(key=lambda e: e.get("created") or 0, reverse=True)
        return Response(200, {"object": "list", "data": events, "has_more": False})

    def list_promotion_codes(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        codes = req.run.state[self.name].get("promotion_codes", {})
        return Response(200, {"object": "list", "data": list(codes.values()), "has_more": False})

    def list_coupons(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        coupons = req.run.state[self.name].get("coupons", {})
        return Response(200, {"object": "list", "data": list(coupons.values()), "has_more": False})

    def list_checkout_sessions(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        sessions = req.run.state[self.name].get("checkout_sessions", {})
        return Response(200, {"object": "list", "data": list(sessions.values()), "has_more": False})

    def list_invoices(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._auth(req)) is not None:
            return bad
        state = req.run.state[self.name]
        items = sorted(state["invoices"].values(), key=lambda i: i["created"], reverse=True)
        cust_id = req.query_one("customer")
        if cust_id:
            items = [i for i in items if i["customer"] == cust_id]
        return Response(200, {"object": "list", "data": items, "has_more": False})

    # ---------- admin ----------

    def authenticate(self, req: Request) -> Response | None:
        return self._auth(req)

    def handle_admin(self, req: Request) -> Response:
        # stub 报表由基类统一提供（Twin._admin_stub_hits），这里只剩"没有别的 admin"
        return super().handle_admin(req)
