"""Link 头分页：照文档写代码的客户端唯一会读的信号。

真 GitHub 的列表端点不带 `total`、不带 `has_more`，只带
`Link: <https://api.github.com/...?page=2>; rel="next", <...>; rel="last"`。
客户端据此决定要不要翻页。所以判据不是"第一页有 30 条"，而是：

- 有 next 时它指向的那一页**真的存在且内容不重叠**；
- 没有 next 时**不许出现 next**（给了 next 的循环会翻到空数组才停，
  在有副作用的循环里就是多删一条 / 多发一次）。
"""

from __future__ import annotations

import json
import re

import pytest

from argaclone.server import App


@pytest.fixture()
def world(tmp_path):
    app = App("scenarios", data_dir=tmp_path)
    view = app.provision({"twins": "github", "ttl": 600})
    rid = view["run_id"]
    state = app.store.get(rid).state["github"]
    state["repos"]["acme/big"] = {
        "id": 7, "name": "big", "full_name": "acme/big", "private": False,
        "stargazers_count": 0, "default_branch": "main",
    }
    state["issues"]["acme/big"] = {
        n: {"number": n, "title": f"issue {n}", "state": "open"} for n in range(1, 41)
    }
    state["commits"]["acme/big"] = [
        {"sha": f"{n:040x}", "commit": {"message": f"c{n}"}} for n in range(1, 26)
    ]
    return app, rid


def _get(app, rid, path):
    tok = app.store.get(rid).extras["credentials"]["github"]
    return app.gateway.handle(
        "GET", f"github.{rid}.twins.local", path,
        {"authorization": f"Bearer {tok}"}, b"",
    )


def _links(resp) -> dict[str, str]:
    """把 Link 头解析成 {rel: url}。解析不出来就是格式错了 —— 直接让测试红。"""
    raw = resp.headers["Link"]
    out = {}
    for part in raw.split(", "):
        m = re.match(r'<([^>]+)>;\s*rel="(\w+)"', part.strip())
        assert m, f"Link 片段格式不对：{part!r}"
        out[m.group(2)] = m.group(1)
    return out


def test_first_page_has_next_and_last(world):
    app, rid = world
    resp = _get(app, rid, "/repos/acme/big/issues?state=all")
    assert resp.status == 200
    assert len(resp.body) == 30          # 真 GitHub 默认 30/页
    links = _links(resp)
    assert "next" in links and "last" in links
    assert "prev" not in links           # 第一页没有 prev
    assert "page=2" in links["next"]
    assert "page=2" in links["last"]     # 40 条 / 30 = 2 页


def test_second_page_exists_does_not_overlap_and_has_no_next(world):
    app, rid = world
    first = _get(app, rid, "/repos/acme/big/issues?state=all")
    second = _get(app, rid, "/repos/acme/big/issues?state=all&page=2")

    nums1 = {i["number"] for i in first.body}
    nums2 = {i["number"] for i in second.body}
    assert len(nums2) == 10
    assert not (nums1 & nums2), "第二页与第一页重叠 —— 翻页会重复处理同一条"

    links = _links(second)
    assert "next" not in links, "最后一页给了 next，客户端会翻到空页"
    assert "prev" in links and "page=1" in links["prev"]


def test_per_page_and_other_query_params_are_preserved_in_next_link(world):
    app, rid = world
    resp = _get(app, rid, "/repos/acme/big/issues?state=all&per_page=5")
    assert len(resp.body) == 5
    next_url = _links(resp)["next"]
    assert "per_page=5" in next_url
    assert "state=all" in next_url, "丢了筛选条件，翻页会拿到另一批数据"
    assert "page=2" in next_url


def test_per_page_is_capped_at_100(world):
    app, rid = world
    resp = _get(app, rid, "/repos/acme/big/issues?state=all&per_page=1000")
    assert len(resp.body) == 40          # 只有 40 条，但 per_page 被压到 100
    assert "per_page=100" in _links(resp)["last"]


def test_commits_use_the_same_shape(world):
    app, rid = world
    resp = _get(app, rid, "/repos/acme/big/commits?per_page=10")
    assert len(resp.body) == 10
    links = _links(resp)
    assert "page=2" in links["next"] and "page=3" in links["last"]


def test_single_page_still_reports_first_and_last(world):
    app, rid = world
    resp = _get(app, rid, "/repos/acme/big/issues?state=open&per_page=100")
    links = _links(resp)
    assert "next" not in links and "prev" not in links
    # 逐参数核，不用 endswith：拼接顺序是实现细节，把顺序写进判据会锁死实现。
    for rel in ("first", "last"):
        assert "page=1" in links[rel] and "per_page=100" in links[rel]


def test_red_dropping_the_link_header_breaks_a_doc_following_client(world, monkeypatch):
    """RED：证明"能翻页"这件事真的来自 Link 头。

    把 `_paged` 换回"回全量数组、不给头"，一个只看 Link 的客户端就会以为
    只有一页 —— 这正是补这个功能之前的真实行为。
    """
    app, rid = world
    from argaclone.twins.github_twin import GitHubTwin

    monkeypatch.setattr(GitHubTwin, "_paged",
                        lambda self, req, items: __import__(
                            "argaclone.twins.base", fromlist=["Response"]
                        ).Response(200, items))
    resp = _get(app, rid, "/repos/acme/big/issues?state=all")
    assert len(resp.body) == 40          # 一次全给
    assert "Link" not in resp.headers    # 没有任何翻页信号
