"""测试夹具。

刻意每个测试用独立 scenario 目录（tmp_path）：场景文件是会影响判分结果的输入，
测试之间共享一个目录就等于共享一个会漂移的世界。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from argaclone.server import App

SCENARIO_SRC = Path(__file__).resolve().parent.parent / "scenarios"

STRIPE_KEY = {"Authorization": "Bearer sk_test_arga"}
SLACK_AUTH = {"content-type": "application/x-www-form-urlencoded"}


@pytest.fixture()
def scenario_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "scenarios"
    shutil.copytree(SCENARIO_SRC, dst)
    return dst


@pytest.fixture()
def app(scenario_dir: Path) -> App:
    return App(scenario_dir)


@pytest.fixture()
def run(app: App):
    return app.provision({"twins": "stripe,slack", "ttl": 600, "scenario_id": "ecom-duplicate-billing"})


def stripe_headers() -> dict[str, str]:
    return dict(STRIPE_KEY)


GITHUB_TOKEN = "ghp_twin_fixture_token_not_real"


@pytest.fixture()
def gh(app: App):
    """一个只带 github twin 的 run，state 里塞好仓库/分支/内容。

    直接写 state 而不是走 HTTP：seed 要能造出 API 造不出来的状态
    （比如两个分支名互为前缀、私有仓库、超长文件），这是 scenario 层的职责。
    """
    view = app.provision({"twins": "github", "ttl": 600})
    run = app.store.get(view["run_id"])
    st = run.state["github"]
    st["repos"]["acme/api"] = {
        "full_name": "acme/api",
        "name": "api",
        "private": False,
        "default_branch": "main",
        "stargazers_count": 7,
    }
    st["repos"]["acme/secret"] = {
        "full_name": "acme/secret",
        "name": "secret",
        "private": True,
        "default_branch": "main",
        "stargazers_count": 0,
    }
    st["branches"]["acme/api"] = {
        "main": "a" * 40,
        "feature": "b" * 40,
        "feature/x": "c" * 40,
    }
    st["contents"]["acme/api"] = {
        "README.md": {"raw": "hello from main", "sha": "1" * 40},
        "docs/guide.md": {"raw": "guide on main", "sha": "2" * 40},
    }
    st["issues"]["acme/api"] = {}
    st["pulls"]["acme/api"] = {}
    st["commits"]["acme/api"] = []
    return run


def gh_headers(token: str = GITHUB_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
