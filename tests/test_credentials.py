"""凭据解析只有一处实现——并盯住它不被改回去。

背景：`Authorization` 的解析原来在 8 个 twin 里各写一遍，于是"空令牌放行"
这个缺陷在 5 个 twin 上同时存在（每个实现都只判"头在不在"）。抽成
`twins/base.py:bearer()` 之后，这条约束只有一处可能被改坏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argaclone.twins.base import Request, bearer
from argaclone.twins.stripe_twin import StripeTwin

TWINS_DIR = Path(__file__).resolve().parent.parent / "argaclone" / "twins"


@pytest.fixture
def ctx(app):
    """(Run 对象, Store)。conftest 的 `run` fixture 返回的是 public view，这里要真对象。"""
    view = app.provision({"twins": "stripe", "ttl_seconds": 60})
    return app.store.get(view["run_id"]), app.store


def _req(headers: dict[str, str], run, store) -> Request:
    return Request(
        method="GET", path="/x", query={}, headers=headers, body=b"",
        run=run, run_id=run.run_id, store=store,
    )


@pytest.mark.parametrize(
    "header_value, expected",
    [
        (None, ""),                    # 没有这个头
        ("", ""),                      # 头存在但为空
        ("Bearer ", ""),               # 空令牌：当年 5 个 twin 全放行
        ("Bearer    ", ""),            # 全空格
        ("Bearer", ""),                # 没有分隔空格
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),   # 方案名不区分大小写
        ("BEARER abc123", "abc123"),
        ("Bearer  abc123 ", "abc123"), # 多余空格
        ("Basic abc123", ""),          # 非 Bearer 方案不能当令牌用
        ("Token abc123", ""),
    ],
)
def test_bearer_extraction(header_value, expected, ctx):
    headers = {} if header_value is None else {"authorization": header_value}
    run, store = ctx
    assert bearer(_req(headers, run, store)) == expected


def test_header_lookup_is_case_insensitive(ctx):
    """进程内调用方（测试/MCP/SDK）不一定把 header 归一成小写。"""
    run, store = ctx
    req = _req({"Authorization": "Bearer abc123"}, run, store)
    assert req.header("authorization") == "Bearer abc123"
    assert req.header("AUTHORIZATION") == "Bearer abc123"


def test_mixed_case_header_reaches_the_twin(app):
    """带原始大小写的头打到 twin，必须是业务响应而不是 401。"""
    view = app.provision({"twins": "stripe", "ttl_seconds": 60})
    run = app.store.get(view["run_id"])
    req = _req({"Authorization": f"Bearer {view['credentials']['stripe']}"},
               run, app.store)
    req.path = "/v1/customers"
    resp = StripeTwin().dispatch(req)
    assert resp.status == 200


def test_no_twin_reimplements_token_parsing():
    """结构断言：解析逻辑只许出现在 base.py。

    这条比"有没有某个函数"更实在——它拦的是"有人在第 12 个 twin 里又抄一遍"，
    而那正是这个缺陷当年扩散的方式。
    """
    offenders = []
    for path in sorted(TWINS_DIR.glob("*_twin.py")):
        src = path.read_text()
        for marker in ('startswith("bearer', "header[7:]", 'len("bearer ") :'):
            if marker in src:
                offenders.append(f"{path.name}: {marker}")
    assert not offenders, "凭据解析在 twin 里重复实现了：" + "; ".join(offenders)
