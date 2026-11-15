"""Salesforce twin（工厂产出 + 三处必须手写的地方）。

为什么这个 twin 值得单独写：Salesforce 的错误信封是**顶层数组**，
`[{"message": "...", "errorCode": "..."}]`，不是对象。几乎所有客户端代码都写成
`body["message"]` —— 拿到数组会 TypeError，或者更糟：`except` 掉之后当成"通用失败"
重试到底。这是整套保真里最便宜、也最容易被糊过去的一条：只要回一个
`{"message": ...}` 形状的对象，测试全绿，而真实 SDK 会炸。

三处躲不开工厂的东西：

1. **列表信封**是 `{totalSize, done, records}`，不是 `{results}` 也不是
   `{values}`。第四种分页形状都不算 —— 它压根不分页（`done` 恒 True），
   深分页要靠 `query?q=... LIMIT` 或 `nextRecordsUrl`。用错信封，jsforce 会
   把 `totalSize` 读成 undefined，然后安心地处理空数组。
2. **PATCH 成功回 204 且 body 为空**。工厂默认回 200 + 对象。回对象看着更"友好"，
   但真 Salesforce 不回——依赖响应体拿 `LastModifiedDate` 的客户端在 twin 上能跑通、
   上线就拿到 `null`，这正是 twin 最该防的那类假绿。
3. **SOQL 是查询语言，不是 REST filter**。`?q=SELECT Id, Name FROM Account WHERE Name = 'X'`。
   只认一小撮子句，其余（OR / ORDER BY / 聚合 / 日期字面量）明确回
   `MALFORMED_QUERY` 并写明不支持什么 —— 静默回全表会让 agent 以为筛选生效了，
   然后基于"全公司只有一个客户"做决策。

`Id` 的形状也复刻了：15 位、前 3 位是对象的 key prefix（Account=001、Contact=003、
Opportunity=006、Lead=00Q、Case=500）。它不是随机串，客户端会按前缀判断类型。
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from ..twin_spec import ErrorShape, Field, Pager, Resource, SpecTwin
from .base import Response, bearer

API_VERSION = "v59.0"
API = f"/services/data/{API_VERSION}"

# 前 3 位是 key prefix，客户端按它判断对象类型；后 12 位用 hex 填充凑满 15 位。
SOBJECTS: dict[str, dict[str, Any]] = {
    "Account": {
        "prefix": "001",
        "fields": [
            Field("Name", required=True),
            Field("Industry"),
            Field("Website"),
            Field("Description"),
            Field("AnnualRevenue"),
        ],
    },
    "Contact": {
        "prefix": "003",
        "fields": [
            Field("LastName", required=True),
            Field("FirstName"),
            Field("Email"),
            Field("Title"),
            Field("AccountId"),
        ],
    },
    "Opportunity": {
        "prefix": "006",
        "fields": [
            Field("Name", required=True),
            Field("StageName"),
            Field("Amount"),
            Field("CloseDate"),
            Field("AccountId"),
        ],
    },
    "Lead": {
        "prefix": "00Q",
        "fields": [
            Field("LastName", required=True),
            Field("Company", required=True),
            Field("Email"),
            Field("Status"),
            Field("Industry"),
        ],
    },
    "Case": {
        "prefix": "500",
        "fields": [
            Field("Subject", required=True),
            Field("Status"),
            Field("Priority"),
            Field("ContactId"),
            Field("Description"),
        ],
    },
}

# 系统字段：写请求里出现就静默丢弃（真 Salesforce 也忽略它们，而不是报错）
SYSTEM_FIELDS = {"Id", "CreatedDate", "LastModifiedDate", "IsDeleted", "attributes", "SystemModstamp"}


def _err(status: int, message: str, code: str) -> Response:
    """Salesforce 的错误体是**数组**。

    这条不是风格问题：`body["message"]` 对数组会抛 TypeError，
    而 `body[0]["message"]` 才是对的。回对象会让所有按文档写的客户端
    在 twin 上跑得比真实环境更顺——那是最坏的一种保真缺陷。
    """
    return Response(status, [{"message": message, "errorCode": code}])


SALESFORCE_ERRORS = ErrorShape(
    not_found=lambda kind, ident: _err(404, "The requested resource does not exist", "NOT_FOUND"),
    bad_request=lambda detail, fld=None: _err(400, detail, "INVALID_FIELD" if fld else "MALFORMED_REQUEST"),
    forbidden=lambda detail: _err(403, detail, "INSUFFICIENT_ACCESS"),
    unauthorized=lambda detail: _err(401, detail, "INVALID_SESSION_ID"),
)


def _iso(ts: Any) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000+0000", time.gmtime(int(ts or 0)))


def _token(req) -> str:
    return bearer(req)


def _record(twin, req, entity: dict) -> dict:
    """扁平存储 → Salesforce 记录：字段在**顶层**，外加 `attributes` 自述类型。

    和 HubSpot 正相反（那边数据在 `properties` 里）。这两种形状都被广泛使用，
    混用会让客户端读到空值 —— 而空值不会报错，只会让 agent 得出错误结论。
    """
    obj = entity.get("_type") or "Account"
    out: dict[str, Any] = {
        "attributes": {"type": obj, "url": f"{API}/sobjects/{obj}/{entity.get('Id')}"}
    }
    for k, v in entity.items():
        if k in ("_type", "created", "updated"):
            continue
        out[k] = v
    out["CreatedDate"] = _iso(entity.get("created"))
    out["LastModifiedDate"] = _iso(entity.get("updated"))
    return out


def _clean_in(twin, req, body: dict) -> dict:
    """写请求：只保留业务字段，系统字段静默丢弃。

    这里刻意不做"未知字段报错"——那件事必须发生在 create/update 里带着对象类型做，
    因为 `Foo__c` 这类自定义字段是租户自己定义的，twin 无从知道，
    拒绝一个合法写入会让 agent 学到"这个 API 不能写自定义字段"这种错误事实。
    """
    return {k: v for k, v in (body or {}).items() if k not in SYSTEM_FIELDS}


class SalesforceTwin(SpecTwin):
    name = "salesforce"
    errors = SALESFORCE_ERRORS
    pager = Pager("none")

    def initial_state(self) -> dict:
        return {self.name: {f"{o.lower()}s": {} for o in SOBJECTS}}

    # ---- 声明 ----

    def _build_routes(self) -> None:
        for obj, spec in SOBJECTS.items():
            self.declare(
                Resource(
                    name=f"{obj.lower()}s",
                    collection=f"{API}/sobjects/{obj}",
                    item=f"{API}/sobjects/{obj}/{{id}}",
                    id_field="Id",
                    id_style="prefix",
                    id_prefix=spec["prefix"],
                    fields=spec["fields"],
                    hooks={"normalize_in": _clean_in, "decorate": _record},
                )
            )
        # 顺序即正确性：`/sobjects/{obj}/describe` 必须排在 `/sobjects/{obj}/{id}` 之前，
        # 否则 "describe" 会被当成一个 id，回 404（真 Salesforce 回的是字段清单）。
        self.route("GET", f"{API}/sobjects/{{obj}}/describe", self.describe)
        self.route("GET", f"{API}/sobjects", self.describe_global)
        self.route("GET", f"{API}/sobjects/{{obj}}", self.list_records)
        self.route("POST", f"{API}/sobjects/{{obj}}", self.create_record)
        self.route("GET", f"{API}/sobjects/{{obj}}/{{id}}", self.get_record)
        self.route("PATCH", f"{API}/sobjects/{{obj}}/{{id}}", self.update_record)
        self.route("DELETE", f"{API}/sobjects/{{obj}}/{{id}}", self.delete_record)
        self.route("GET", f"{API}/query", self.query)
        self.route("GET", f"{API}/queryAll", self.query)
        self.route("GET", "/services/data", self.versions)
        self.route("GET", f"/services/data/{API_VERSION}", self.versions)
        self.mount()

    # ---- 鉴权 ----

    def authenticate(self, req):
        if req.path.startswith("/admin"):
            return None
        if not _token(req):
            return _err(401, "Session expired or invalid", "INVALID_SESSION_ID")
        return None

    def dispatch(self, req):
        """鉴权放路由匹配之前（同 HubSpot：基类只在未命中时兜底，好路径全开）。

        另外把任意 `vNN.N` 版本段归一成 v59.0：客户端从环境变量读版本号是常态，
        而 twin 只实现一个版本。版本不匹配回 404 会让 agent 以为"这个资源不存在"，
        然后放弃 —— 那是把"twin 只做了一个版本"伪装成"数据不存在"。
        """
        if not req.path.startswith("/admin"):
            bad = self.authenticate(req)
            if bad is not None:
                return bad
        m = re.match(r"^(/services/data/)v\d+\.\d+(/.*)?$", req.path)
        if m:
            req = type(req)(
                method=req.method,
                path=f"{m.group(1)}{API_VERSION}{m.group(2) or ''}",
                query=req.query,
                headers=req.headers,
                body=req.body,
                run=req.run,
                run_id=req.run_id,
                store=req.store,
            )
        return super().dispatch(req)

    # ---- 状态 ----

    def _bucket(self, req, obj: str) -> dict:
        return req.run.state.setdefault(self.name, {}).setdefault(f"{obj.lower()}s", {})

    def _rows(self, req, obj: str) -> list[dict]:
        rows = list(self._bucket(req, obj).values())
        rows.sort(key=lambda r: r.get("created") or 0)
        return rows

    def _obj(self, name: str) -> str | None:
        """对象名大小写不敏感（`FROM account` 在 SOQL 里合法）。"""
        for o in SOBJECTS:
            if o.lower() == (name or "").lower():
                return o
        return None

    def _allowed(self, obj: str) -> set[str]:
        return {f.name for f in SOBJECTS[obj]["fields"]} | {"Id"}

    def _bad_field(self, obj: str, field: str) -> Response:
        return _err(400, f"No such column '{field}' on sobject of type {obj}", "INVALID_FIELD")

    # ---- 元数据 ----

    def versions(self, req, params):
        return Response(
            200,
            [
                {"label": "Winter '26", "url": "/services/data/v59.0", "version": "59.0"},
                {"label": "Spring '26", "url": "/services/data/v60.0", "version": "60.0"},
            ],
        )

    def describe_global(self, req, params):
        return Response(
            200,
            {
                "encoding": "UTF-8",
                "maxBatchSize": 200,
                "sobjects": [
                    {
                        "name": obj,
                        "label": obj,
                        "keyPrefix": SOBJECTS[obj]["prefix"],
                        "queryable": True,
                        "createable": True,
                        "updateable": True,
                        "custom": False,
                    }
                    for obj in SOBJECTS
                ],
            },
        )

    def describe(self, req, params):
        obj = self._obj(params["obj"])
        if obj is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        fields = [
            {"name": "Id", "type": "id", "length": 18, "createable": False, "updateable": False, "nillable": False}
        ]
        for f in SOBJECTS[obj]["fields"]:
            fields.append(
                {
                    "name": f.name,
                    "type": "string",
                    "length": 255,
                    "createable": True,
                    "updateable": True,
                    "nillable": not f.required,
                }
            )
        return Response(200, {"name": obj, "label": obj, "fields": fields, "_twin": True})

    # ---- CRUD ----

    def list_records(self, req, params):
        obj = self._obj(params["obj"])
        if obj is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        rows = [_record(self, req, r) for r in self._rows(req, obj)]
        # `done: true` 是契约的一部分：客户端靠它判断"还有没有下一页"，
        # 缺了它 jsforce 会去读 undefined 的 nextRecordsUrl 然后崩。
        return Response(200, {"totalSize": len(rows), "done": True, "records": rows})

    def create_record(self, req, params):
        obj = self._obj(params["obj"])
        if obj is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        body = _clean_in(self, req, req.json())
        allowed = self._allowed(obj)
        for k in body:
            # `__c` 是租户自定义字段，twin 无从枚举 —— 放行而不是报错
            if k not in allowed and not k.endswith("__c"):
                return self._bad_field(obj, k)
        missing = [f.name for f in SOBJECTS[obj]["fields"] if f.required and not body.get(f.name)]
        if missing:
            return _err(
                400,
                f"Required fields are missing: [{', '.join(missing)}]",
                "REQUIRED_FIELD_MISSING",
            )
        bucket = self._bucket(req, obj)
        ident = f"{SOBJECTS[obj]['prefix']}{uuid.uuid4().hex[:12]}"
        entity: dict[str, Any] = {}
        for f in SOBJECTS[obj]["fields"]:
            entity[f.name] = body.get(f.name)
        for k, v in body.items():
            if k not in entity:
                entity[k] = v
        entity["Id"] = ident
        entity["_type"] = obj
        entity["created"] = int(time.time())
        entity["updated"] = entity["created"]
        bucket[ident] = entity
        return Response(201, {"id": ident, "success": True, "errors": []})

    def get_record(self, req, params):
        obj = self._obj(params["obj"])
        if obj is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        entity = self._bucket(req, obj).get(params["id"])
        if entity is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        return Response(200, _record(self, req, entity))

    def update_record(self, req, params):
        obj = self._obj(params["obj"])
        if obj is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        bucket = self._bucket(req, obj)
        entity = bucket.get(params["id"])
        if entity is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        body = _clean_in(self, req, req.json())
        for k in body:
            if k == "Id":
                continue
            if k not in self._allowed(obj) and not k.endswith("__c"):
                return self._bad_field(obj, k)
            entity[k] = body[k]
        entity["updated"] = int(time.time())
        # 204 + 空 body，不返回实体。真 Salesforce 就这样：
        # 回对象会让"从响应拿 LastModifiedDate"的客户端在 twin 上成立、上线拿到 None。
        return Response(204, None)

    def delete_record(self, req, params):
        obj = self._obj(params["obj"])
        if obj is None:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        bucket = self._bucket(req, obj)
        if params["id"] not in bucket:
            return _err(404, "The requested resource does not exist", "NOT_FOUND")
        del bucket[params["id"]]
        return Response(204, None)

    # ---- SOQL ----

    def query(self, req, params):
        """SOQL 的一个明确子集：SELECT 字段 FROM 对象 [WHERE ...] [LIMIT n]。

        不支持的子句**明确报错**。静默忽略 ORDER BY 会返回"看起来排好了"的顺序，
        静默忽略 OR 会返回比请求更多的行 —— 两者都会让 agent 基于错误结果继续做。
        """
        raw = (req.query_one("q") or "").strip()
        if not raw:
            return _err(400, "expected a SOQL query in the q parameter", "MALFORMED_QUERY")
        m = re.match(
            r"^SELECT\s+(?P<fields>.+?)\s+FROM\s+(?P<obj>[A-Za-z_]\w*)"
            r"(?:\s+WHERE\s+(?P<where>.+?))?"
            r"(?:\s+LIMIT\s+(?P<limit>\d+))?\s*$",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return _err(400, f"unexpected token: {raw[:40]}", "MALFORMED_QUERY")
        obj = self._obj(m.group("obj"))
        if obj is None:
            return _err(400, f"sObject type '{m.group('obj')}' is not supported", "INVALID_TYPE")
        wanted = [f.strip() for f in m.group("fields").split(",") if f.strip()]
        allowed = self._allowed(obj)
        if wanted == ["*"] or [w.upper() for w in wanted] == ["FIELDS(ALL)"]:
            wanted = ["Id"] + [f.name for f in SOBJECTS[obj]["fields"]]
        for w in wanted:
            if w not in allowed and not w.endswith("__c"):
                return self._bad_field(obj, w)
        rows = self._rows(req, obj)
        where = (m.group("where") or "").strip()
        if where:
            for clause in re.split(r"\s+AND\s+", where, flags=re.IGNORECASE):
                clause = clause.strip()
                cm = re.match(r"^(\w+)\s*(=|!=|LIKE)\s*'([^']*)'$", clause, re.IGNORECASE)
                if not cm:
                    return _err(400, f"unexpected token: {clause}", "MALFORMED_QUERY")
                field, op, value = cm.group(1), cm.group(2).upper(), cm.group(3)
                if field not in allowed and not field.endswith("__c"):
                    return self._bad_field(obj, field)
                if op == "=":
                    rows = [r for r in rows if str(r.get(field) or "") == value]
                elif op == "!=":
                    rows = [r for r in rows if str(r.get(field) or "") != value]
                else:
                    pattern = "^" + re.escape(value).replace("%", ".*").replace("_", ".") + "$"
                    rows = [r for r in rows if re.match(pattern, str(r.get(field) or ""), re.IGNORECASE)]
        if m.group("limit"):
            rows = rows[: int(m.group("limit"))]
        records = []
        for r in rows:
            rec = _record(self, req, r)
            records.append({k: v for k, v in rec.items() if k in wanted or k in ("attributes", "Id")})
        return Response(200, {"totalSize": len(records), "done": True, "records": records})

    # ---- admin ----

    def handle_admin(self, req):
        if req.path.endswith("/admin/seed"):
            body = req.json() or {}
            state = req.run.state.setdefault(self.name, {})
            for obj in SOBJECTS:
                state.setdefault(f"{obj.lower()}s", {})
                for row in body.get(obj) or body.get(f"{obj.lower()}s") or []:
                    ident = str(row.get("Id") or f"{SOBJECTS[obj]['prefix']}{uuid.uuid4().hex[:12]}")
                    entity = {k: v for k, v in row.items() if k not in ("Id", "attributes", "CreatedDate", "LastModifiedDate")}
                    entity["Id"] = ident
                    entity["_type"] = obj
                    entity.setdefault("created", int(time.time()))
                    entity["updated"] = entity["created"]
                    state[f"{obj.lower()}s"][ident] = entity
            return Response(200, {"ok": True})
        return super().handle_admin(req)
