"""GitHub twin。

三个别处没有的保真点，也是它值得单独写一个文件的原因：

  1. **父 scope 自动授子**：给了 `repo` 就等于同时给了 `repo:status`、`public_repo` 等一串，
     但反过来不成立。抄错这一点，所有"权限刚好够"的测试都会误判。
  2. **`/api/v3` 前缀与 raw 内容不带 token**：GitHub Enterprise 与 raw.githubusercontent.com
     的真实行为，客户端会依赖它。
  3. **stub 命中报表是富结构**（带 recent 流水）：它回答的不是"假了几次"，而是
     "哪几次调用撞到了假数据"——只有它能做到这一步。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any

from .base import Request, Response, Twin

DOC_URL = "https://docs.github.com/rest"

# 父 scope ⇒ 自动授予的子 scope。抄自 GitHub 官方文档的 scope 继承表。
PARENT_SCOPES: dict[str, set[str]] = {
    "repo": {
        "repo:status",
        "repo_deployment",
        "repo:invite",
        "public_repo",
        "security_events",
    },
    "admin:repo_hook": {"write:repo_hook", "read:repo_hook"},
    "write:repo_hook": {"read:repo_hook"},
    "admin:org": {"write:org", "read:org"},
    "write:org": {"read:org"},
    "admin:public_key": {"write:public_key", "read:public_key"},
    "write:public_key": {"read:public_key"},
}

RAW_PREFIXES = ("/raw/",)


def _next_id(seq: dict[str, int], key: str) -> int:
    """单调自增 id。放在模块级是因为 review/comment 都要用，
    而状态里的 seq 表跟着 run 走 —— 重启后 id 不回退。"""
    seq[key] = int(seq.get(key, 1000)) + 1
    return seq[key]


def _now() -> int:
    return int(time.time())


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


class GitHubTwin(Twin):
    name = "github"

    # 声明的边界：这些端点**不打算**仿真。
    # 理由不是"来不及"，而是"仿真它会让孪生环境变成一份假的密钥清单"——
    # 那比不实现更危险：agent 会以为自己在安全地读密钥，实际上读的是我们的假值，
    # 然后拿它去真环境试。报表里它们与欠账分开计。
    declared_stubs = (
        ("GET", "/repos/*/*/actions/secrets",
         "不打算实现：列出仓库 Actions secrets。孪生里不该存在可供读取的密钥清单，"
         "假值会被 agent 当成真密钥去用。"),
        ("PUT", "/repos/*/*/actions/secrets",
         "不打算实现：写入 Actions secrets（同上，孪生不保管密钥材料）。"),
        ("GET", "/orgs/*/actions/secrets",
         "不打算实现：组织级 secrets（同上）。"),
    )

    # 真 GitHub 里**不存在**的路径。命中回 404（而不是 stub 200）：
    # 这些请求 120 episodes 里出现过 20 多次，每次都给 agent 一个"改成功了"的假象。
    absent_paths = (
        ("POST", "/repos/*/*/issues/*",
         "真 GitHub 更新单条 issue 用 PATCH，POST 到单条 issue 是 404。"
         "回 stub 会让 agent 以为状态改了。"),
        ("POST", "/repos/*/*/pulls/*",
         "同上：更新 PR 用 PATCH，POST 单条 PR 不存在。"),
        ("POST", "/repos/*/*/issues/*/labels/*",
         "给单条 issue 加标签用 POST 到 /labels（复数集合），带 label 名的单条 POST 不存在。"),
        ("*", "/api/*",
         "GitHub 的 REST 前缀是 /api/v3（企业版）或根路径；/api/<method> 是别的 "
         "provider 的路径形状（Slack 的 /api/chat.postMessage 之类），多半是 Host 用错了。"),
    )

    # ---------- 状态 ----------

    def initial_state(self) -> dict[str, Any]:
        return {
            "repos": {},           # "owner/repo" → repo object
            "issues": {},          # "owner/repo" → {number: issue}
            "pulls": {},           # "owner/repo" → {number: pull}
            "comments": {},        # "owner/repo" → {comment_id: comment}
            "contents": {},        # "owner/repo" → {path: {raw, sha}}
            "pull_comments": {},   # "owner/repo" → {number: [comment]}
            "collaborators": {},   # "owner/repo" → [login]
            "commits": {},         # "owner/repo" → [commit]
            "blobs": {},           # sha → {"content","encoding"}（POST /git/blobs 建出来的）
            "environments": {},    # "owner/repo" → [{"id","name"}]
            "stargazers": {},      # "owner/repo" → [login]
            "workflows": {},       # "owner/repo" → [workflow]
            "repo_teams": {},      # "owner/repo" → [slug]
            "branches": {},        # "owner/repo" → {name: sha}
            # tags 与"额外 heads"（POST /git/refs 造出来的）。branches 是 heads 的主表，
            # 这里只补 tag 与通过 API 新建的 ref —— 两处都写会让"改分支"有两个真相。
            "git_refs": {},        # "owner/repo" → {ref: sha}
            "deployments": {},     # "owner/repo" → [deployment]
            "deployment_statuses": {},  # str(deployment_id) → [status]
            "actions_caches": {},  # "owner/repo" → [cache]
            "actions_runs": {},    # "owner/repo" → [run]
            "releases": {},        # "owner/repo" → [release]
            "repo_invitations": {},  # "owner/repo" → [{id, invitee, permissions}]
            "deliveries": [],      # webhook 投递记录
            "granted_scopes": ["repo", "workflow", "admin:repo_hook"],
            "next_number": 1,
        }

    # ---------- 路由 ----------

    def _build_routes(self) -> None:
        r = self.route
        r("GET", "/user", self.get_user)

        r("GET", "/repos/{owner}/{repo}", self.get_repo)
        r("GET", "/repos/{owner}/{repo}/branches", self.list_branches)
        r("GET", "/repos/{owner}/{repo}/commits", self.list_commits)

        r("GET", "/repos/{owner}/{repo}/issues", self.list_issues)
        r("POST", "/repos/{owner}/{repo}/issues", self.create_issue)
        r("GET", "/repos/{owner}/{repo}/issues/{number}", self.get_issue)
        r("PATCH", "/repos/{owner}/{repo}/issues/{number}", self.update_issue)
        r("POST", "/repos/{owner}/{repo}/issues/{number}/comments", self.create_comment)
        r("GET", "/repos/{owner}/{repo}/issues/{number}/comments", self.list_comments)
        # 给 issue 打标签是"分诊"类任务的标准收尾动作。缺了这三个端点，
        # 所有以 "labels 包含 X" 为判据的题都变成不可解 —— 而 agent 会一直
        # 重试到步数耗尽，最后被记成 FAIL，看起来像它不会干活。
        r("GET", "/repos/{owner}/{repo}/issues/{number}/labels", self.list_labels)
        r("POST", "/repos/{owner}/{repo}/issues/{number}/labels", self.add_labels)
        r("PUT", "/repos/{owner}/{repo}/issues/{number}/labels", self.set_labels)
        r("DELETE", "/repos/{owner}/{repo}/issues/{number}/labels/{name}", self.remove_label)

        r("GET", "/repos/{owner}/{repo}/pulls", self.list_pulls)
        r("POST", "/repos/{owner}/{repo}/pulls", self.create_pull)
        r("GET", "/repos/{owner}/{repo}/pulls/{number}", self.get_pull)
        r("PUT", "/repos/{owner}/{repo}/pulls/{number}/merge", self.merge_pull)
        r("GET", "/repos/{owner}/{repo}/pulls/{number}/comments", self.list_review_comments)
        r("POST", "/repos/{owner}/{repo}/pulls/{number}/comments", self.create_review_comment)
        r("GET", "/orgs/{org}/repos", self.list_org_repos)
        r("GET", "/repos/{owner}/{repo}/collaborators", self.list_collaborators)

        # 列目录（不带 path）与取单文件（带 path）是两个端点，真 GitHub 就是这样：
        # 前者回数组，后者回对象。**480 episodes 里这条被撞了 87 次，是 github 侧第一名。**
        # 只实现"取单文件"会让 agent 永远看不到目录里有什么，只能猜路径。
        r("GET", "/repos/{owner}/{repo}/contents", self.list_contents)
        # 路径里的 {path} 可能含斜杠，这里只覆盖单段；多段走下面的 raw/contents 特判
        r("GET", "/repos/{owner}/{repo}/contents/{path}", self.get_content)
        r("PUT", "/repos/{owner}/{repo}/contents/{path}", self.put_content)
        # 真 GitHub 有两个"拿文件内容"的官方入口，客户端会各用一半：
        # `/contents/<path>` 回 base64 JSON，`/raw/<ref>/<path>` 回裸字节。
        # 只实现前者会让所有用 raw 形式的 agent 撞 stub —— 那是环境缺口，
        # 不是它不会干活。
        r("GET", "/repos/{owner}/{repo}/raw/{ref}/{path}", self.get_raw_file)
        r("GET", "/repos/{owner}/{repo}/git/trees/{ref}", self.get_tree)

        # ---- 读仓库：agent 找东西的入口 ----
        # 这些端点的共同点：缺了它们，agent 会基于"查不到 = 不存在"的推断做决定，
        # 而它看到的其实是我这边的缺口。480 episodes 有 399 次 github stub。
        r("GET", "/user/repos", self.list_user_repos)
        r("GET", "/users/{user}/repos", self.list_user_repos)
        r("GET", "/repos", self.list_user_repos)
        r("GET", "/users/{user}", self.get_user_by_login)
        r("GET", "/repos/{owner}/{repo}/actions/cache/usage", self.cache_usage)
        r("GET", "/repos/{owner}/{repo}/invitations", self.list_repo_invitations)
        r("GET", "/repos/{owner}/{repo}/collaborators/{login}/permission",
          self.get_collaborator_permission)
        r("DELETE", "/repos/{owner}/{repo}/collaborators/{login}", self.remove_collaborator)
        r("GET", "/repos/{owner}/{repo}/labels", self.list_repo_labels)
        r("POST", "/repos/{owner}/{repo}/labels", self.create_repo_label)
        r("GET", "/repos/{owner}/{repo}/pulls/{number}/files", self.list_pull_files)
        r("GET", "/repos/{owner}/{repo}/pulls/{number}/commits", self.list_pull_commits)
        r("GET", "/repos/{owner}/{repo}/pulls/{number}/reviews", self.list_pull_reviews)
        # 评审任务的最后一步是**提交** review：只给 GET 会让 agent 永远写不进去，
        # 判据（"有没有 approve"）于是恒为假，看起来像它不会评审。
        r("POST", "/repos/{owner}/{repo}/pulls/{number}/reviews", self.create_pull_review)
        r("GET", "/repos/{owner}/{repo}/issues/{number}/timeline", self.list_issue_timeline)
        r("GET", "/repos/{owner}/{repo}/issues/{number}/events", self.list_issue_events)
        r("GET", "/repos/{owner}/{repo}/assignees", self.list_assignees)
        r("GET", "/repos/{owner}/{repo}/contributors", self.list_contributors)
        r("GET", "/repos/{owner}/{repo}/commits/{sha}", self.get_commit)
        r("GET", "/orgs/{org}/outside_collaborators", self.list_outside_collaborators)
        r("GET", "/orgs/{org}/members", self.list_org_members)
        r("GET", "/orgs/{org}/members/{login}", self.get_org_membership)
        r("DELETE", "/orgs/{org}/members/{login}", self.remove_org_member)
        r("GET", "/orgs/{org}/memberships/{login}", self.get_org_membership)
        r("GET", "/orgs/{org}/invitations", self.list_org_invitations)
        r("GET", "/users/{user}/orgs", self.list_user_orgs)
        r("GET", "/repos/{owner}/{repo}/forks", self.list_forks)
        r("GET", "/repos/{owner}/{repo}/events", self.list_repo_events)
        r("GET", "/orgs/{org}/teams", self.list_org_teams)
        r("GET", "/repos/{owner}/{repo}/collaborators/{login}", self.get_collaborator)

        # ---- git refs / 部署 / Actions / 发布 ----
        # 这些是 120 episodes 里补完读接口之后剩下的主缺口。它们的共同点不是"读不到"，
        # 而是**写进去的状态没地方落**：打 tag、建 deployment、发 release 都是动作，
        # 判据要去读那之后的世界。只给 GET 会让这些题永远差最后一步。
        r("GET", "/repos/{owner}/{repo}/git/commits/{sha}", self.get_git_commit)
        r("GET", "/repos/{owner}/{repo}/git/refs", self.list_git_refs)
        r("POST", "/repos/{owner}/{repo}/git/refs", self.create_git_ref)
        r("GET", "/repos/{owner}/{repo}/tags", self.list_tags)
        r("GET", "/repos/{owner}/{repo}/releases", self.list_releases)
        r("POST", "/repos/{owner}/{repo}/releases", self.create_release)
        r("GET", "/repos/{owner}/{repo}/releases/tags/{tag}", self.get_release_by_tag)
        r("GET", "/repos/{owner}/{repo}/deployments", self.list_deployments)
        r("GET", "/repos/{owner}/{repo}/deployments/{id}", self.get_deployment)
        r("GET", "/repos/{owner}/{repo}/environments", self.list_environments)
        r("POST", "/repos/{owner}/{repo}/git/blobs", self.create_blob)
        r("GET", "/repos/{owner}/{repo}/git/matching-refs/{ref}", self.matching_refs)
        r("GET", "/repos/{owner}/{repo}/stargazers", self.list_stargazers)
        r("GET", "/repos/{owner}/{repo}/teams", self.list_repo_teams)
        r("GET", "/repos/{owner}/{repo}/actions/workflows", self.list_workflows)
        r("GET", "/orgs/{org}", self.get_org)
        r("POST", "/repos/{owner}/{repo}/deployments", self.create_deployment)
        r("GET", "/repos/{owner}/{repo}/deployments/{id}/statuses", self.list_deployment_statuses)
        r("POST", "/repos/{owner}/{repo}/deployments/{id}/statuses", self.create_deployment_status)
        r("GET", "/repos/{owner}/{repo}/actions/caches", self.list_actions_caches)
        r("DELETE", "/repos/{owner}/{repo}/actions/caches/{id}", self.delete_actions_cache)
        r("GET", "/repos/{owner}/{repo}/actions/runs", self.list_actions_runs)

        # ---- 搜索：三张表是 GitHub 上"先找再说"的标准动作 ----
        r("GET", "/search/issues", self.search_issues)
        r("GET", "/search/code", self.search_code)
        r("GET", "/search/repositories", self.search_repositories)
        r("GET", "/search/users", self.search_users)
        r("GET", "/search/commits", self.search_commits)

        r("POST", "/graphql", self.graphql)
        r("POST", "/repos/{owner}/{repo}/actions/workflows/{wid}/dispatches", self.dispatch_workflow)

    # ---------- 错误形状 ----------

    def _not_found(self, what: str = "Not Found") -> Response:
        return Response(404, {"message": what, "documentation_url": DOC_URL, "status": "404"})

    def _forbidden(self, message: str = "Resource not accessible by personal access token") -> Response:
        # 这个字符串是 GitHub 的原文，客户端与日志分析都会匹配它
        return Response(403, {"message": message, "documentation_url": DOC_URL, "status": "403"})

    def _bad_request(self, message: str, **extra: Any) -> Response:
        return Response(422, {"message": message, "documentation_url": DOC_URL, "status": "422", **extra})

    # ---------- 鉴权 / scope ----------

    def _token(self, req: Request) -> str:
        auth = req.header("authorization")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        if auth.startswith("token "):
            return auth[len("token "):].strip()
        return req.query_one("access_token")

    def effective_scopes(self, state: dict[str, Any]) -> set[str]:
        granted = set(state["granted_scopes"])
        out = set(granted)
        for parent, children in PARENT_SCOPES.items():
            if parent in granted:
                out |= children
        return out

    def _check(self, req: Request, needed: str = "") -> Response | None:
        token = self._token(req)
        if not token or not token.startswith(("ghp_", "github_pat_", "gho_", "ghs_")):
            return Response(401, {"message": "Requires authentication", "documentation_url": DOC_URL})

        if not needed:
            return None
        state = req.run.state[self.name]
        scopes = self.effective_scopes(state)
        if "*" in scopes or needed in scopes:
            return None
        # 父 scope 已经在上面的 effective_scopes 里展开过了，到这里就是真的没有
        return self._forbidden()

    # ---------- dispatch：前缀与 raw 特判 ----------

    def dispatch(self, req: Request) -> Response:
        path = req.path

        # /api/v3 前缀（GitHub Enterprise 的客户端会带）
        if path.startswith("/api/v3"):
            stripped = path[len("/api/v3"):]
            req.path = stripped or "/"
            path = req.path

        # raw 内容不需要 token，这是真 GitHub 的行为；ref 可能含斜杠，交给专门解析
        for prefix in RAW_PREFIXES:
            if path.startswith(prefix):
                tail = path[len(prefix):]
                return self._raw(req, tail)

        # `/repos/{owner}/{repo}/raw/{ref}/{path}` 也是官方的裸内容入口，
        # 但 path 可能含斜杠，通用路由的 `[^/]+` 匹配不到，会掉进 stub。
        m = re.match(r"^/repos/([^/]+)/([^/]+)/raw/(.+)$", path)
        if m and req.method.upper() == "GET":
            return self._raw_repo(req, f"{m.group(1)}/{m.group(2)}", m.group(3))

        # `/contents/<path>` 的 path 可能含斜杠（content/pricing、release/notes-270）。
        # 通用路由的 `[^/]+` 匹配不到就会掉进 stub，而那意味着**所有"改仓库文件"的任务
        # 都不可能完成** —— 判据还在等着读改后的内容，于是题目整体无解。
        # 这不是"端点没实现"那么轻，是"任务集一部分被静默废掉"。
        m = re.match(r"^/repos/([^/]+)/([^/]+)/contents/(.+)$", path)
        if m and req.method.upper() in ("GET", "PUT"):
            params = {"owner": m.group(1), "repo": m.group(2), "path": m.group(3)}
            handler = self.get_content if req.method.upper() == "GET" else self.put_content
            return handler(req, params)

        # 说明：以下特判存在的理由都一样 —— 通用路由的 `[^/]+` 匹配不到含斜杠的
        # ref/path，于是一整类"看这个分支/看这个文件"的动作掉进 stub。
        # 每个特判都对应 480 episodes 里真实发生过的 stub 命中。

        # `/branches/<ref>` 与 `/git/trees/<ref>`：ref 可能是 feature/deep/nest
        m = re.match(r"^/repos/([^/]+)/([^/]+)/commits/(.+)$", path)
        if m and m.group(3) not in ("", ):
            resp = self.get_commit_by_ref(req, {"owner": m.group(1), "repo": m.group(2), "ref": m.group(3)})
            if resp.status != 404:
                return resp

        m = re.match(r"^/repos/([^/]+)/([^/]+)/branches/(.+)$", path)
        if m and req.method.upper() == "GET":
            return self.get_branch(req, {"owner": m.group(1), "repo": m.group(2), "ref": m.group(3)})

        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/trees/(.+)$", path)
        if m and req.method.upper() == "GET":
            return self.get_tree(req, {"owner": m.group(1), "repo": m.group(2), "ref": m.group(3)})

        # `/compare/a...b` 两侧都可能是带斜杠的分支名
        m = re.match(r"^/repos/([^/]+)/([^/]+)/compare/(.+)$", path)
        if m and req.method.upper() == "GET":
            return self.compare(req, {"owner": m.group(1), "repo": m.group(2), "spec": m.group(3)})

        # ref 名可能含斜杠（heads/feature/deep/nest），通用路由的 [^/]+ 匹配不到
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/ref/(.+)$", path)
        if m and req.method.upper() == "GET":
            return self.get_git_ref(req, {"owner": m.group(1), "repo": m.group(2),
                                          "ref": m.group(3)})

        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/refs/(.+)$", path)
        if m:
            params = {"owner": m.group(1), "repo": m.group(2), "ref": m.group(3)}
            if req.method.upper() == "PATCH":
                return self.update_git_ref(req, params)
            if req.method.upper() == "GET":
                return self.list_git_refs(req, params)
            if req.method.upper() == "DELETE":
                return self.delete_git_ref(req, params)

        # `/git/blobs/<sha>`：trees 给出 sha，下一步必然要拿内容
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/blobs/([^/]+)$", path)
        if m and req.method.upper() == "GET":
            return self.get_blob(req, {"owner": m.group(1), "repo": m.group(2), "sha": m.group(3)})

        return super().dispatch(req)

    def _raw_repo(self, req: Request, key: str, rest: str) -> Response:
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        branches = state.get("branches", {}).get(key, {})
        for ref in sorted(branches, key=len, reverse=True):
            if rest.startswith(ref + "/"):
                path = rest[len(ref) + 1:]
                content = state.get("contents", {}).get(key, {}).get(path)
                if content:
                    return Response(200, content["raw"].encode(),
                                    {"Content-Type": "text/plain; charset=utf-8"})
                return Response(404, b"404: Not Found", {"Content-Type": "text/plain; charset=utf-8"})
        return Response(404, b"404: Not Found", {"Content-Type": "text/plain; charset=utf-8"})

    def _raw(self, req: Request, tail: str) -> Response:
        if req.method.upper() != "GET":
            return self._not_found()
        parts = [p for p in tail.split("/") if p]
        if len(parts) < 3:
            return self._not_found()
        owner, repo = parts[0], parts[1]
        rest = "/".join(parts[2:])

        key = f"{owner}/{repo}"
        branches = self.req_state(req).get("branches", {}).get(key, {})
        # 最长分支名优先：feature/x 与 feature 同时存在时，前者才是真 ref
        for ref in sorted(branches, key=len, reverse=True):
            if rest.startswith(ref + "/"):
                path = rest[len(ref) + 1:]
                content = self.req_state(req).get("contents", {}).get(key, {}).get(path)
                if content:
                    # raw 走裸字节：是真文件内容，不是 JSON 字符串
                    return Response(200, content["raw"].encode(), {"Content-Type": "text/plain; charset=utf-8"})
                return Response(404, b"404: Not Found", {"Content-Type": "text/plain; charset=utf-8"})
        return Response(404, b"404: Not Found", {"Content-Type": "text/plain; charset=utf-8"})

    def get_raw_file(self, req: Request, params: dict[str, str]) -> Response:
        key = f"{params['owner']}/{params['repo']}"
        path = params["path"]
        content = self.req_state(req).get("contents", {}).get(key, {}).get(path)
        if not content:
            return Response(404, b"404: Not Found", {"Content-Type": "text/plain; charset=utf-8"})
        return Response(200, content["raw"].encode(), {"Content-Type": "text/plain; charset=utf-8"})

    def get_tree(self, req: Request, params: dict[str, str]) -> Response:
        """列出某个 ref 下的文件树。

        缺了它，agent 想"看看这个仓库里有什么"就只能猜路径 —— 而猜路径
        失败会消耗步数预算，最后测到的是运气不是判断力。
        """
        # 公开仓库读文件树不需要额外 scope，与 get_repo 同一判据
        if not self._token(req):
            return Response(401, {"message": "Requires authentication", "documentation_url": DOC_URL})
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        ref = params["ref"]
        branches = state.get("branches", {}).get(key, {})
        if ref not in branches:
            return self._not_found()
        files = state.get("contents", {}).get(key, {})
        tree = [
            {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"], "size": len(blob["raw"])}
            for path, blob in files.items()
        ]
        return Response(200, {"sha": branches[ref], "tree": tree, "truncated": False})

    # ---------- 读仓库：trees / blobs / branches / compare ----------
    # 这几条是"找东西"的必经之路。缺任一条，agent 想看清仓库里有什么
    # 就只能猜路径，猜错的代价是步数预算 —— 最后测到的是运气不是判断力。
    # 480 episodes 里 /git/blobs 被撞了 30 次、/contents 87 次、/user/repos 60 次。

    def get_blob(self, req: Request, params: dict[str, str]) -> Response:
        """按 sha 取文件内容。真 GitHub 回 base64，客户端自己 decode。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        want = params["sha"]
        for path, blob in state.get("contents", {}).get(key, {}).items():
            if blob.get("sha") == want:
                return Response(200, {"sha": want, "size": len(blob["raw"]),
                                      "content": _b64(blob["raw"]), "encoding": "base64",
                                      "path": path})
        return self._not_found()

    def get_branch(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        branches = self.req_state(req).get("branches", {}).get(key)
        if branches is None:
            return self._not_found()
        sha = branches.get(params["ref"])
        if not sha:
            return self._not_found()
        return Response(200, {"name": params["ref"], "commit": {"sha": sha}, "protected": False})

    def compare(self, req: Request, params: dict[str, str]) -> Response:
        """对比两个 ref。只回答"两个 ref 之间有哪些文件不同"。

        不做真实 diff 计算：内容是 seed 造的，三方合并没有意义。
        但 files 清单必须真实（按两边 contents 求差），否则"这个 PR 改了哪些文件"
        这类判断拿到的就是假列表。
        """
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        branches = state.get("branches", {}).get(key)
        if branches is None:
            return self._not_found()
        spec = params["spec"]
        if "..." not in spec:
            return self._bad_request("Validation Failed",
                                     errors=[{"resource": "Compare", "field": "spec",
                                              "code": "invalid"}])
        base_ref, head_ref = spec.split("...", 1)
        # 最长匹配：feature/deep/nest 不能被当成 feature + /deep/nest
        if base_ref not in branches or head_ref not in branches:
            return self._not_found()
        files = state.get("contents", {}).get(key, {})
        return Response(200, {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "base_commit": {"sha": branches[base_ref]},
            "head_commit": {"sha": branches[head_ref]},
            "files": [{"filename": p, "status": "modified", "sha": b["sha"]}
                      for p, b in files.items()],
            "commits": state.get("commits", {}).get(key, []),
        })

    def get_branch_or_list(self, req: Request, params: dict[str, str]) -> Response:
        key = f"{params['owner']}/{params['repo']}"
        branches = self.req_state(req).get("branches", {}).get(key)
        if branches is None:
            return self._not_found()
        return Response(200, [
            {"name": name, "commit": {"sha": sha}, "protected": False}
            for name, sha in branches.items()
        ])

    def req_state(self, req: Request) -> dict[str, Any]:
        return req.run.state[self.name]

    # ---------- 基础 ----------

    def get_user(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        return Response(200, {
            "login": "twin-bot",
            "id": 1001,
            "type": "User",
            "site_admin": False,
            "name": "Twin Bot",
        })

    def get_repo(self, req: Request, params: dict[str, str]) -> Response:
        # 单独处理：读公开仓库不需要 repo scope，只有私有仓库才 403。
        # 这一条是 GitHub 权限模型里最容易抄错的地方——按"统一先查 scope"写，
        # 会让所有读公开仓库的客户端在此处误红。
        token = self._token(req)
        if not token or not token.startswith(("ghp_", "github_pat_", "gho_", "ghs_")):
            return Response(401, {"message": "Requires authentication", "documentation_url": DOC_URL})

        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        repo = state.get("repos", {}).get(key)
        if not repo:
            return self._not_found()

        if repo.get("private"):
            scopes = self.effective_scopes(state)
            if "*" not in scopes and "repo" not in scopes:
                return self._forbidden()
        return Response(200, repo)

    def list_branches(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        branches = self.req_state(req).get("branches", {}).get(key)
        if branches is None:
            return self._not_found()
        return Response(200, [
            {"name": name, "commit": {"sha": sha}, "protected": False}
            for name, sha in branches.items()
        ])

    def list_commits(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        commits = self.req_state(req).get("commits", {}).get(key)
        if commits is None:
            return self._not_found()
        # 提交列表也走 Link 分页：真 GitHub 上这是同一个形状，
        # 而"翻页只在一个端点上有效"会让 agent 学到错误的规律。
        return self._paged(req, commits)

    # ---------- issues ----------

    def _issues_of(self, req: Request, owner: str, repo: str) -> dict[str, Any] | None:
        return self.req_state(req).get("issues", {}).get(f"{owner}/{repo}")

    def _paged(self, req: Request, items: list[dict[str, Any]]) -> Response:
        """带 `Link` 头的分页 —— 真 GitHub 的做法。

        这是照文档写代码的 agent 最常踩的一脚：它读 `Link: <...page=2>; rel="next"`
        决定要不要翻页，而完全不看 `?page=` 之外的任何信号。如果 twin 回全量数组，
        agent 会以为"一次拿全了"，任务在 5 条数据上过、在 60 条上崩。
        默认 30/页 也是真的（per_page 上限 100）。
        """
        per_page = int(req.query_one("per_page", "30") or 30)
        per_page = 100 if per_page > 100 else (1 if per_page < 1 else per_page)
        page = int(req.query_one("page", "1") or 1)
        page = 1 if page < 1 else page
        total = len(items)
        last = max(1, -(-total // per_page))
        start = (page - 1) * per_page
        window = items[start : start + per_page]

        kept = {k: v for k, v in req.query.items() if k not in ("page", "per_page")}
        pairs = "&".join(f"{k}={v[0]}" for k, v in sorted(kept.items()) if v)
        base = f"https://api.github.com{req.path}"
        def url(n: int) -> str:
            q = f"per_page={per_page}&page={n}"
            return f"<{base}?{q}{('&' + pairs) if pairs else ''}>"

        # rel 的顺序按真 GitHub：prev / next / first / last，没有的那项整个不出现。
        # "没有 next 时不要硬给一个空 rel" 是判据的一部分：给了 next 的客户端
        # 会一直翻页，翻到空数组才停，这在有 side effect 的循环里就是无限循环。
        rels: list[str] = []
        if page > 1:
            rels.append(f'{url(page - 1)}; rel="prev"')
        if page < last:
            rels.append(f'{url(page + 1)}; rel="next"')
        rels.append(f'{url(1)}; rel="first"')
        rels.append(f'{url(last)}; rel="last"')
        return Response(200, window, {"Link": ", ".join(rels)})

    def list_issues(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        issues = self._issues_of(req, params["owner"], params["repo"])
        if issues is None:
            return self._not_found()
        state_filter = req.query_one("state", "open")
        items = [
            i for i in issues.values()
            if state_filter == "all" or i["state"] == state_filter
        ]
        return self._paged(req, sorted(items, key=lambda i: -i["number"]))

    def create_issue(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        state = self.req_state(req)
        key = f"{params['owner']}/{params['repo']}"
        issues = state["issues"].get(key)
        if issues is None:
            return self._not_found()

        body = req.json()
        if not body.get("title"):
            return self._bad_request("Validation Failed", errors=[{"resource": "Issue", "field": "title", "code": "missing_field"}])

        number = state["next_number"]
        state["next_number"] += 1
        issue = {
            "id": _now() * 1000 + number,
            "number": number,
            "title": body["title"],
            "body": body.get("body") or "",
            "state": "open",
            "labels": body.get("labels") or [],
            "assignee": body.get("assignee"),
            "user": {"login": "twin-bot"},
            "created_at": _now(),
        }
        issues[str(number)] = issue
        return Response(201, issue)

    def get_issue(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        issues = self._issues_of(req, params["owner"], params["repo"])
        if issues is None:
            return self._not_found()
        issue = issues.get(str(params["number"]))
        if not issue:
            return self._not_found()
        return Response(200, issue)

    def update_issue(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        issues = self._issues_of(req, params["owner"], params["repo"])
        if issues is None:
            return self._not_found()
        issue = issues.get(str(params["number"]))
        if not issue:
            return self._not_found()

        body = req.json()
        for field in ("title", "body", "state", "labels", "assignee"):
            if field in body:
                issue[field] = body[field]
        if body.get("state") == "closed":
            issue["closed_at"] = _now()
        return Response(200, issue)

    def create_comment(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        issues = self._issues_of(req, params["owner"], params["repo"])
        if issues is None or str(params["number"]) not in issues:
            return self._not_found()

        body = req.json()
        if not body.get("body"):
            return self._bad_request("Validation Failed", errors=[{"resource": "IssueComment", "field": "body", "code": "missing_field"}])

        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        cid = _now() * 1000 + len(state["comments"].get(key, {}))
        comment = {
            "id": cid,
            "body": body["body"],
            "user": {"login": "twin-bot"},
            "created_at": _now(),
            "issue_number": int(params["number"]),
        }
        state["comments"].setdefault(key, {})[str(cid)] = comment
        return Response(201, comment)

    # ---------- pulls ----------

    def list_pulls(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        pulls = self.req_state(req).get("pulls", {}).get(f"{params['owner']}/{params['repo']}")
        if pulls is None:
            return self._not_found()
        return Response(200, sorted(pulls.values(), key=lambda p: -p["number"]))

    def create_pull(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        state = self.req_state(req)
        key = f"{params['owner']}/{params['repo']}"
        pulls = state["pulls"].get(key)
        if pulls is None:
            return self._not_found()

        body = req.json()
        for field in ("title", "head", "base"):
            if not body.get(field):
                return self._bad_request(
                    "Validation Failed",
                    errors=[{"resource": "PullRequest", "field": field, "code": "missing_field"}],
                )

        number = state["next_number"]
        state["next_number"] += 1
        pull = {
            "number": number,
            "title": body["title"],
            "body": body.get("body") or "",
            "state": "open",
            "head": {"ref": body["head"]},
            "base": {"ref": body["base"]},
            "merged": False,
            "user": {"login": "twin-bot"},
            "created_at": _now(),
        }
        pulls[str(number)] = pull
        return Response(201, pull)

    def get_pull(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        pulls = self.req_state(req).get("pulls", {}).get(f"{params['owner']}/{params['repo']}")
        if pulls is None:
            return self._not_found()
        pull = pulls.get(str(params["number"]))
        if not pull:
            return self._not_found()
        return Response(200, pull)

    def merge_pull(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        pulls = self.req_state(req).get("pulls", {}).get(f"{params['owner']}/{params['repo']}")
        if pulls is None:
            return self._not_found()
        pull = pulls.get(str(params["number"]))
        if not pull:
            return self._not_found()
        if pull.get("merged"):
            # 重复合并是真 GitHub 会拒的操作，不能被当成幂等成功
            return Response(405, {"message": "Pull Request is not mergeable", "documentation_url": DOC_URL})

        pull["merged"] = True
        pull["state"] = "closed"
        pull["merged_at"] = _now()
        return Response(200, {"merged": True, "sha": _sha(f"merge-{pull['number']}-{_now()}")})

    # ---------- 标签 / 评论 ----------

    def _issue_or_404(self, req: Request, params: dict[str, str]) -> tuple[dict, dict] | Response:
        state = self.req_state(req)
        key = f"{params['owner']}/{params['repo']}"
        issues = state.get("issues", {}).get(key) or {}
        issue = issues.get(str(params["number"]))
        if not issue:
            return self._not_found()
        return state, issue

    def list_labels(self, req: Request, params: dict[str, str]) -> Response:
        got = self._issue_or_404(req, params)
        if isinstance(got, Response):
            return got
        return Response(200, [{"name": lb} for lb in got[1].get("labels") or []])

    def add_labels(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        got = self._issue_or_404(req, params)
        if isinstance(got, Response):
            return got
        issue = got[1]
        body = req.json()
        incoming = body.get("labels") or []
        if isinstance(body.get("labels"), list) and not incoming:
            return self._bad_request("labels", "missing_field")
        current = list(issue.get("labels") or [])
        for lb in incoming:
            if lb not in current:
                current.append(lb)   # 幂等：重复添加不产生重复标签
        issue["labels"] = current
        return Response(200, [{"name": lb} for lb in current])

    def set_labels(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        got = self._issue_or_404(req, params)
        if isinstance(got, Response):
            return got
        issue = got[1]
        issue["labels"] = list((req.json() or {}).get("labels") or [])
        return Response(200, [{"name": lb} for lb in issue["labels"]])

    def remove_label(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        got = self._issue_or_404(req, params)
        if isinstance(got, Response):
            return got
        issue = got[1]
        name = params["name"]
        before = list(issue.get("labels") or [])
        issue["labels"] = [lb for lb in before if lb != name]
        if len(issue["labels"]) == len(before):
            return self._not_found()
        return Response(200, [{"name": lb} for lb in issue["labels"]])

    def list_comments(self, req: Request, params: dict[str, str]) -> Response:
        got = self._issue_or_404(req, params)
        if isinstance(got, Response):
            return got
        state, issue = got
        key = f"{params['owner']}/{params['repo']}"
        box = state.get("comments", {}).get(key, {})
        return Response(200, list(box.get(str(params["number"]), []) or []))

    def list_review_comments(self, req: Request, params: dict[str, str]) -> Response:
        state = self.req_state(req)
        key = f"{params['owner']}/{params['repo']}"
        if key not in state.get("repos", {}):
            return self._not_found()
        box = state.get("pull_comments", {}).get(key, {})
        return Response(200, list(box.get(str(params["number"]), []) or []))

    def create_review_comment(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        state = self.req_state(req)
        key = f"{params['owner']}/{params['repo']}"
        if key not in state.get("repos", {}):
            return self._not_found()
        body = req.json()
        if not (body or {}).get("body"):
            return self._bad_request("body", "missing_field")
        box = state.setdefault("pull_comments", {}).setdefault(key, {})
        items = box.setdefault(str(params["number"]), [])
        comment = {
            "id": _now() * 1000 + len(items),
            "body": body["body"],
            "path": body.get("path") or "",
            "line": body.get("line") or 0,
        }
        items.append(comment)
        # 真 GitHub 的 PR 也是 issue：`GET /repos/{o}/{r}/issues/{n}/comments` 能读到
        # PR 评论。只在 pull_comments 里存会让"评论到 PR"与"评论到 issue"两种正确写法
        # 落到两个不同的盒子里，判据只能猜一个 —— 猜错就是正确解被判失败。
        ibox = state.setdefault("comments", {}).setdefault(key, {})
        ibox.setdefault(str(params["number"]), []).append(dict(comment))
        return Response(201, comment)

    # ---------- 列表 / 搜索 / 杂项读 ----------
    #
    # 这一整块是对 480 episodes 的 stub 报告的直接回应：github 侧 399 次 stub 里，
    # 前八名有六个落在这里。它们不是"锦上添花的端点"，是 agent 找东西的唯一入口。
    # 缺了它们，agent 会基于"查不到 = 不存在"的推断做决定 —— 而它看到的
    # 其实是我这边的缺口，最后被记成 FAIL。

    def list_user_repos(self, req: Request, params: dict[str, str]) -> Response:
        """当前 token 能看到哪些仓库。真 GitHub 会分页，这里一次给全。"""
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        scopes = self.effective_scopes(state)
        items = []
        for key, repo in state.get("repos", {}).items():
            if repo.get("private") and "*" not in scopes and "repo" not in scopes:
                continue   # 私有仓库需要 repo scope，与 get_repo 同一判据
            items.append(repo)
        return Response(200, items)

    def get_user_by_login(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        return Response(200, {
            "login": params["user"], "id": 2000 + len(params["user"]),
            "type": "User", "site_admin": False, "name": params["user"],
        })

    def list_repo_labels(self, req: Request, params: dict[str, str]) -> Response:
        """仓库上所有用过的标签。缺了它，agent 只能猜标签名 —— 猜错就打不上。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        out: dict[str, dict[str, Any]] = {
            lbl["name"]: lbl for lbl in (state.get("repo_labels", {}).get(key) or [])
        }
        # 历史上用过的标签也算 —— agent 常常直接给 issue 打标签而不先建标签。
        for issue in state.get("issues", {}).get(key, {}).values():
            for name in issue.get("labels") or []:
                out.setdefault(name, {"name": name})
        return Response(200, [out[n] for n in sorted(out)])

    def create_repo_label(self, req: Request, params: dict[str, str]) -> Response:
        """建仓库标签。只给"给 issue 加标签"是不够的：分诊类任务的判据里
        标签清单是独立对象，没建出来就查不到。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        body = req.json() or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return self._bad_request("name is required", field="name")
        labels = state.setdefault("repo_labels", {}).setdefault(key, [])
        for lbl in labels:
            if lbl["name"] == name:
                return Response(422, {"message": "Validation Failed",
                                      "errors": [{"code": "already_exists", "field": "name"}]})
        label = {"name": name, "color": body.get("color") or "ededed",
                 "description": body.get("description") or ""}
        labels.append(label)
        return Response(201, label)

    def list_pull_files(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        pulls = state.get("pulls", {}).get(key)
        if pulls is None:
            return self._not_found()
        pull = pulls.get(str(params["number"]))
        if not pull:
            return self._not_found()
        files = state.get("contents", {}).get(key, {})
        return Response(200, [
            {"filename": p, "status": "modified", "additions": 1, "deletions": 1,
             "changes": 2, "sha": b["sha"]}
            for p, b in files.items()
        ])

    def list_pull_commits(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        pulls = self.req_state(req).get("pulls", {}).get(key)
        if pulls is None or str(params["number"]) not in pulls:
            return self._not_found()
        return Response(200, self.req_state(req).get("commits", {}).get(key, []))

    def list_pull_reviews(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        return Response(200, state.get("reviews", {}).get(key, {}).get(str(params["number"]), []))

    def create_pull_review(self, req: Request, params: dict[str, str]) -> Response:
        """提交 PR review。`event` → `state` 的映射照真 GitHub：
        APPROVE→APPROVED、REQUEST_CHANGES→CHANGES_REQUESTED、其余→COMMENTED。
        判据通常读 state，所以这个映射不是装饰 —— 把 APPROVE 记成 COMMENTED，
        "评审通过了"那条判据就永远不成立。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        number = str(params["number"])
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        if number not in (state.get("pulls", {}).get(key) or {}):
            return self._not_found()
        body = req.json() or {}
        event = str(body.get("event") or "COMMENT").upper()
        mapped = {"APPROVE": "APPROVED",
                  "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(event, "COMMENTED")
        review = {
            "id": _next_id(state.setdefault("review_seq", {}), key),
            "user": {"login": "twin-bot"},
            "state": mapped,
            "body": body.get("body") or "",
            "submitted_at": _now(),
            "commit_id": (state.get("branches", {}).get(key) or {}).get("main"),
        }
        bucket = state.setdefault("reviews", {}).setdefault(key, {}).setdefault(number, [])
        bucket.append(review)
        return Response(201, review)

    def list_issue_timeline(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        got = self._issue_or_404(req, params)
        if isinstance(got, Response):
            return got
        state, issue = got
        key = f"{params['owner']}/{params['repo']}"
        out = [{"event": "labeled", "label": {"name": lb}, "created_at": issue.get("created_at")}
               for lb in issue.get("labels") or []]
        for c in state.get("comments", {}).get(key, {}).get(str(params["number"]), []) or []:
            out.append({"event": "commented", "body": c.get("body"), "created_at": c.get("created_at")})
        return Response(200, out)

    def list_issue_events(self, req: Request, params: dict[str, str]) -> Response:
        """issue 的事件流。真 GitHub 与 timeline 略有差别，这里保持同源。"""
        return self.list_issue_timeline(req, params)

    def list_assignees(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        return Response(200, [{"login": lg} for lg in state.get("collaborators", {}).get(key, [])])

    def list_contributors(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        return Response(200, [{"login": lg, "contributions": 1}
                              for lg in state.get("collaborators", {}).get(key, [])])

    def get_commit(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        for c in self.req_state(req).get("commits", {}).get(key, []):
            if c.get("sha") == params["sha"]:
                return Response(200, c)
        return self._not_found()

    def list_org_members(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        org = params["org"]
        logins: set[str] = set()
        for key, collabs in state.get("collaborators", {}).items():
            if key.split("/")[0] == org:
                logins.update(collabs or [])
        return Response(200, [{"login": lg} for lg in sorted(logins)])

    def get_org_membership(self, req: Request, params: dict[str, str]) -> Response:
        """查某人是否在组织里。不在回 404 —— 真 GitHub 就是这个语义，
        而"是不是成员"正是离职清理类任务的第一步判断。"""
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        org, login = params["org"], params["login"]
        for key, collabs in state.get("collaborators", {}).items():
            if key.split("/")[0] == org and login in (collabs or []):
                return Response(200, {"state": "active", "role": "member",
                                      "user": {"login": login}, "organization": {"login": org}})
        return self._not_found()

    def _org_member_logins(self, state: dict[str, Any], org: str) -> list[str]:
        """组织成员有两个来源：显式成员表与仓库 collaborator。

        都在用（seed 走哪个都能查），所以并集；但**删除必须两边都删** ——
        只删一边会让"移出组织"之后查成员仍然查得到，判据读到的是没删成功。
        """
        explicit = state.get("org_members", {}).get(org)
        if explicit is not None:
            # 显式成员表存在时以它为准。为什么不能总是取并集：
            # "外部协作者"= 有仓库权限但**不是**组织成员，两个接口若都从
            # collaborator 派生成员，这个差集恒为空 —— 接口在，答案永远是"没有"。
            return sorted(set(explicit))

        # 没有显式成员表时才退到"仓库协作者即成员"（老 seed 只有协作者）。
        logins: set[str] = set()
        for key, collabs in state.get("collaborators", {}).items():
            if key.split("/")[0] == org:
                logins.update(collabs or [])
        return sorted(logins)

    def remove_org_member(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        org, login = params["org"], params["login"]
        found = False
        members = state.setdefault("org_members", {}).get(org)
        if members and login in members:
            members.remove(login)
            found = True
        for key, collabs in (state.get("collaborators") or {}).items():
            if key.split("/")[0] == org and login in (collabs or []):
                collabs.remove(login)
                found = True
        if not found:
            return self._not_found()
        return Response(204, None)

    def list_org_invitations(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        return Response(200, list(state.get("org_invitations", {}).get(params["org"], [])))

    def list_user_orgs(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        login = params["user"]
        out = []
        for org in sorted({k.split("/")[0] for k in state.get("collaborators", {})}
                          | set(state.get("org_members", {}))):
            if login in self._org_member_logins(state, org):
                out.append({"login": org, "id": org, "url": f"/orgs/{org}"})
        return Response(200, out)

    def list_forks(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        return Response(200, list(state.get("forks", {}).get(key, [])))

    def list_repo_events(self, req: Request, params: dict[str, str]) -> Response:
        """仓库时间线。用现有状态现拼，而不是单开一张表 ——
        单开表就要在每次写操作里同步，漏一处就出现"改了但事件流里没有"。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        events = list(state.get("repo_events", {}).get(key, []))
        for number, issue in sorted((state.get("issues", {}).get(key) or {}).items()):
            events.append({
                "type": "IssuesEvent",
                "actor": {"login": (issue.get("user") or {}).get("login") or "unknown"},
                "repo": {"name": key},
                "payload": {"action": "opened" if issue.get("state") == "open" else "closed",
                            "issue": {"number": issue.get("number"), "title": issue.get("title")}},
                "created_at": issue.get("created_at") or _now(),
            })
        for number, review_list in ((state.get("reviews", {}).get(key) or {}).items()):
            for rev in review_list:
                events.append({"type": "PullRequestReviewEvent",
                               "actor": rev.get("user") or {"login": "unknown"},
                               "repo": {"name": key},
                               "payload": {"review": {"state": rev.get("state")}},
                               "created_at": rev.get("submitted_at") or _now()})
        return Response(200, events)

    def list_org_teams(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        org = params["org"]
        if not any(k.split("/")[0] == org for k in state.get("repos", {})):
            return self._not_found()
        return Response(200, state.get("teams", {}).get(org, []))

    def get_collaborator(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        if params["login"] not in (state.get("collaborators", {}).get(key) or []):
            return self._not_found()
        return Response(200, {"login": params["login"],
                              "permissions": {"admin": False, "push": True, "pull": True}})

    # ---------- 搜索 ----------
    # 三张表都只做子串匹配。真 GitHub 的搜索是一门查询语言，复刻解析器不值得；
    # 但"搜一下有没有人报过这个问题"是多系统分诊里最高频的一步，
    # 缺了它 agent 只能全量翻页 —— 那测到的是上下文长度不是判断力。

    @staticmethod
    def _q(req: Request) -> str:
        return (req.query_one("q") or "").strip().strip('"')

    def search_issues(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        q = self._q(req)
        items = []
        for key, box in state.get("issues", {}).items():
            for issue in box.values():
                if q in issue.get("title", "") or q in (issue.get("body") or ""):
                    items.append({**issue, "repository_url": key})
        return Response(200, {"total_count": len(items), "incomplete_results": False,
                              "items": items})

    def search_code(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        q = self._q(req)
        items = []
        for key, files in state.get("contents", {}).items():
            for path, blob in files.items():
                if q in blob["raw"] or q in path:
                    items.append({"name": path.split("/")[-1], "path": path,
                                  "sha": blob["sha"],
                                  "repository": {"full_name": key}})
        return Response(200, {"total_count": len(items), "incomplete_results": False,
                              "items": items})

    def search_repositories(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        q = self._q(req)
        items = [
            repo for key, repo in state.get("repos", {}).items()
            if q in key or q in (repo.get("description") or "")
        ]
        return Response(200, {"total_count": len(items), "incomplete_results": False,
                              "items": items})

    def search_users(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        login = self._q(req)
        return Response(200, {"total_count": 1 if login else 0, "incomplete_results": False,
                              "items": [{"login": login}] if login else []})

    def search_commits(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        q = self._q(req)
        items = []
        for key, commits in state.get("commits", {}).items():
            for c in commits:
                msg = (c.get("commit") or {}).get("message") or ""
                if q in msg:
                    items.append({**c, "repository": {"full_name": key}})
        return Response(200, {"total_count": len(items), "items": items})

    def list_org_repos(self, req: Request, params: dict[str, str]) -> Response:
        state = self.req_state(req)
        org = params["org"]
        items = [repo for key, repo in state.get("repos", {}).items() if key.split("/")[0] == org]
        return Response(200, items)

    def list_collaborators(self, req: Request, params: dict[str, str]) -> Response:
        state = self.req_state(req)
        key = f"{params['owner']}/{params['repo']}"
        if key not in state.get("repos", {}):
            return self._not_found()
        return Response(200, [{"login": lg} for lg in state.get("collaborators", {}).get(key, [])])

    def get_collaborator_permission(self, req: Request, params: dict[str, str]) -> Response:
        """单个人的权限。离职清理类任务的第一步问的就是这个，而不是列全表。

        权限按"在不在 collaborator 表"给 push —— twin 不建模细粒度 custom role，
        但**必须与 `GET /collaborators` 一致**：两边都认同一张表，
        否则"列表里有、权限查询说没有"会让 agent 得出相反的结论。
        """
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        if params["login"] not in (state.get("collaborators", {}).get(key) or []):
            return self._not_found()
        return Response(200, {
            "permission": "push",
            "role_name": "write",
            "user": {"login": params["login"]},
        })

    def remove_collaborator(self, req: Request, params: dict[str, str]) -> Response:
        """移出协作者。回 204 而不是 200 + 对象 —— 客户端按 204 写分支。

        删之后再查必须 404：只删一处（比如组织成员表与仓库协作者表只删一边）
        会让"移出成功了吗"这个问题有两个答案。
        """
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        collabs = state.get("collaborators", {}).get(key) or []
        if params["login"] not in collabs:
            return self._not_found()
        collabs.remove(params["login"])
        state.setdefault("collaborators", {})[key] = collabs
        return Response(204, None)

    def list_repo_invitations(self, req: Request, params: dict[str, str]) -> Response:
        """待接受的仓库邀请。没见过邀请的仓库回 []（不是 404）——
        真 GitHub 对空集合回空数组，"没有邀请"和"仓库不存在"必须能分开。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        return Response(200, list(state.get("repo_invitations", {}).get(key) or []))

    def list_outside_collaborators(self, req: Request, params: dict[str, str]) -> Response:
        """组织的外部协作者：在组织仓库里有权限、但不是组织成员。

        这个差集正是"谁还能进来"的答案。用并集回会让组织成员也出现在
        外部协作者列表里 —— 那会让"清理外部权限"这类任务去删自己人。
        """
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        org = params["org"]
        if not any(k.split("/")[0] == org for k in state.get("repos", {})):
            return self._not_found()
        members = set(self._org_member_logins(state, org))
        outside: set[str] = set()
        for key, collabs in state.get("collaborators", {}).items():
            if key.split("/")[0] == org:
                outside.update(collabs or [])
        return Response(200, [{"login": lg} for lg in sorted(outside - members)])

    def cache_usage(self, req: Request, params: dict[str, str]) -> Response:
        """Actions 缓存配额用量。数字从 `actions_caches` 现算，不另存一份计数器 ——
        两份计数一定会漂移，而这一问的整点就在"到底占了多少"。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        caches = state.get("actions_caches", {}).get(key) or []
        return Response(200, {
            "full_name": key,
            "active_caches_count": len(caches),
            "active_caches_size_in_bytes": sum(int(c.get("size_in_bytes") or 0) for c in caches),
        })

    # ---------- contents ----------

    def list_contents(self, req: Request, params: dict[str, str]) -> Response:
        """列仓库某个目录下的直接子项（真 GitHub 回数组，不递归）。

        形状细节不能省：目录项 `type="dir"`、文件项 `type="file"`，
        两者都有 `path`/`sha`，但只有文件有 `size` 与 `download_url`。
        客户端（包括 gh CLI）依赖 `type` 分支，全当 file 回会让它去读目录内容。
        """
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        prefix = (req.query_one("path") or "").strip("/")
        files = state.get("contents", {}).get(key, {})
        seen_dirs: set[str] = set()
        items: list[dict[str, Any]] = []
        for path, blob in files.items():
            rest = path[len(prefix) + 1:] if prefix and path.startswith(prefix + "/") else (
                path if not prefix else "")
            if not rest:
                continue
            if "/" in rest:
                top = rest.split("/")[0]
                if top in seen_dirs:
                    continue
                seen_dirs.add(top)
                items.append({
                    "name": top, "path": f"{prefix}/{top}".strip("/"), "sha": _sha(top),
                    "size": 0, "type": "dir",
                })
            else:
                items.append({
                    "name": rest, "path": path, "sha": blob["sha"],
                    "size": len(blob["raw"]), "type": "file",
                    "download_url": f"https://raw.githubusercontent.com/{key}/main/{path}",
                })
        if not items:
            # 真空目录与"路径根本不存在"要分开：真 GitHub 对后者回 404
            return self._not_found()
        items.sort(key=lambda i: (i["type"] != "dir", i["name"]))
        return Response(200, items)

    def get_content(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        keys = self.req_state(req).get("contents", {}).get(key)
        if keys is None:
            return self._not_found()
        entry = keys.get(params["path"])
        if not entry:
            return self._not_found()
        return Response(200, {
            "name": params["path"].split("/")[-1],
            "path": params["path"],
            "sha": entry["sha"],
            "size": len(entry["raw"]),
            "type": "file",
            "encoding": "base64",
            "content": _b64(entry["raw"]),
        })

    def put_content(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        keys = state.get("contents", {}).get(key)
        if keys is None:
            return self._not_found()

        body = req.json()
        raw = _unb64(body.get("content") or "")
        sha = _sha(raw)
        # 真 GitHub 的冲突检测：给了 sha 且与当前不一致就 409，不给则视为新建/覆盖
        current = keys.get(params["path"])
        if body.get("sha") and current and current["sha"] != body["sha"]:
            return Response(409, {
                "message": f'{params["path"]} does not match {body["sha"]}',
                "documentation_url": DOC_URL,
                "status": "409",
            })

        keys[params["path"]] = {"raw": raw, "sha": sha}
        state.setdefault("commits", {}).setdefault(key, []).insert(0, {
            "sha": sha,
            "commit": {"message": body.get("message") or "update", "author": {"name": "twin-bot"}},
        })
        return Response(200, {
            "content": {"path": params["path"], "sha": sha},
            "commit": {"sha": sha, "message": body.get("message") or "update"},
        })

    # ---------- actions / graphql ----------

    # ---------- git refs ----------

    def _ref_shas(self, req: Request, key: str) -> dict[str, str]:
        """把 heads 与 git_refs 合并成 {完整 ref 名: sha}。

        heads 的主表是 `branches`（改分支只有一处真相），`git_refs` 只放 tags
        与通过 API 新建的 ref。合并时 branches 覆盖同名项 —— 否则"PATCH 改了分支"
        之后 GET 还能读到旧值，那是最难查的一类不一致。
        """
        state = self.req_state(req)
        out: dict[str, str] = dict(state.get("git_refs", {}).get(key) or {})
        for name, sha in (state.get("branches", {}).get(key) or {}).items():
            out[f"refs/heads/{name}"] = sha
        return out

    def _ref_obj(self, key: str, ref: str, sha: str) -> dict[str, Any]:
        base = f"https://api.github.com/repos/{key}/git"
        short = ref[len("refs/"):] if ref.startswith("refs/") else ref
        return {
            "ref": ref,
            "node_id": self._node_id(ref),
            "url": f"{base}/refs/{short}",
            "object": {
                "sha": sha,
                "type": "tag" if ref.startswith("refs/tags/") else "commit",
                "url": f"{base}/commits/{sha}",
            },
        }

    @staticmethod
    def _node_id(text: str) -> str:
        # 真 GitHub 的 node_id 是 base64；这里要的是"同一 ref 永远同一串"
        return "MDM6UmVm" + hashlib.sha1(text.encode()).hexdigest()[:20]

    @staticmethod
    def _now() -> str:
        # 固定形状的 UTC 时间戳。用真实时钟：判据里常写"比 X 更晚"，
        # 造一个死时间会让这类比较全部错向。
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _short_ref(name: str) -> str:
        """`heads/main` / `refs/heads/main` / `main` 都归一成完整 ref。

        真 API 的路径写 `heads/main`，body 里写 `refs/heads/main`，客户端两种都会用。
        不归一就会出现"创建成功但读不到"这种自相矛盾。
        """
        name = name.strip()
        if name.startswith("refs/"):
            return name
        if name.startswith(("heads/", "tags/")):
            return f"refs/{name}"
        return f"refs/heads/{name}"

    def get_git_ref(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        if key not in self.req_state(req).get("repos", {}):
            return self._not_found()
        ref = self._short_ref(params["ref"])
        sha = self._ref_shas(req, key).get(ref)
        if not sha:
            return self._not_found("Not Found")
        return Response(200, self._ref_obj(key, ref, sha))

    def list_git_refs(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        if key not in self.req_state(req).get("repos", {}):
            return self._not_found()
        refs = self._ref_shas(req, key)
        prefix = params.get("ref") or req.query_one("namespace") or ""
        prefix = prefix.strip()
        if prefix and not prefix.startswith("refs/"):
            prefix = f"refs/{prefix}"
        items = [
            self._ref_obj(key, ref, sha)
            for ref, sha in sorted(refs.items())
            if not prefix or ref.startswith(prefix)
        ]
        return Response(200, items)

    def create_git_ref(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        body = req.json() or {}
        name = (body.get("ref") or "").strip()
        sha = (body.get("sha") or "").strip()
        if not name or not sha:
            return self._bad_request("ref and sha are required")
        ref = self._short_ref(name)
        if ref in self._ref_shas(req, key):
            # 真 GitHub 回 422（不是 409）：调用方常按 422 处理"已存在"
            return self._bad_request("Reference already exists")
        state.setdefault("git_refs", {}).setdefault(key, {})[ref] = sha
        return Response(201, self._ref_obj(key, ref, sha))

    def update_git_ref(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        ref = self._short_ref(params["ref"])
        sha = ((req.json() or {}).get("sha") or "").strip()
        if not sha:
            return self._bad_request("sha is required")
        if ref not in self._ref_shas(req, key):
            return self._not_found("Not Found")
        if ref.startswith("refs/heads/"):
            branches = state.setdefault("branches", {}).setdefault(key, {})
            name = ref[len("refs/heads/"):]
            if name in branches:
                branches[name] = sha
            else:
                state.setdefault("git_refs", {}).setdefault(key, {})[ref] = sha
        else:
            state.setdefault("git_refs", {}).setdefault(key, {})[ref] = sha
        return Response(200, self._ref_obj(key, ref, sha))

    def delete_git_ref(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        ref = self._short_ref(params["ref"])
        if ref.startswith("refs/heads/"):
            branches = state.get("branches", {}).get(key) or {}
            name = ref[len("refs/heads/"):]
            if name in branches:
                branches.pop(name)
                return Response(204, None)
        tags = state.get("git_refs", {}).get(key) or {}
        if ref in tags:
            tags.pop(ref)
            return Response(204, None)
        return self._not_found("Not Found")

    def get_git_commit(self, req: Request, params: dict[str, str]) -> Response:
        """`/git/commits/{sha}` 与 `/commits/{sha}` 是**两个端点**。

        前者回 git 数据（tree/parents），后者回 API 包装（files/stats）。
        用一份结构糊两边，客户端很难察觉 —— 直到它去读 `commit.tree.sha`。
        """
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        for c in state.get("commits", {}).get(key) or []:
            if c.get("sha") == params["sha"]:
                message = ((c.get("commit") or {}).get("message")) or c.get("message") or ""
                base = f"https://api.github.com/repos/{key}/git"
                return Response(200, {
                    "sha": c["sha"],
                    "node_id": self._node_id(c["sha"]),
                    "url": f"{base}/commits/{c['sha']}",
                    "message": message,
                    "tree": {"sha": hashlib.sha1((key + c["sha"]).encode()).hexdigest(),
                             "url": f"{base}/trees/{c['sha']}"},
                    "parents": [],
                    "author": c.get("author") or {},
                })
        return self._not_found("Not Found")

    # ---------- tags / releases ----------

    def list_tags(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        if key not in self.req_state(req).get("repos", {}):
            return self._not_found()
        out = []
        for ref, sha in sorted(self._ref_shas(req, key).items()):
            if not ref.startswith("refs/tags/"):
                continue
            name = ref[len("refs/tags/"):]
            out.append({
                "name": name,
                "commit": {"sha": sha, "url": f"https://api.github.com/repos/{key}/commits/{sha}"},
                "zipball_url": f"https://api.github.com/repos/{key}/zipball/refs/tags/{name}",
                "tarball_url": f"https://api.github.com/repos/{key}/tarball/refs/tags/{name}",
            })
        return Response(200, out)

    def _release_obj(self, key: str, r: dict[str, Any]) -> dict[str, Any]:
        out = dict(r)
        out.setdefault("url", f"https://api.github.com/repos/{key}/releases/{r.get('id')}")
        return out

    def list_releases(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        if key not in self.req_state(req).get("repos", {}):
            return self._not_found()
        rows = self.req_state(req).setdefault("releases", {}).setdefault(key, [])
        return Response(200, [self._release_obj(key, r) for r in rows])

    def create_release(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        state = self.req_state(req)
        if key not in state.get("repos", {}):
            return self._not_found()
        body = req.json() or {}
        tag = (body.get("tag_name") or "").strip()
        if not tag:
            return self._bad_request("tag_name is required")
        rows = state.setdefault("releases", {}).setdefault(key, [])
        rel = {
            "id": state.get("next_number", 1) + 9000,
            "tag_name": tag,
            "name": body.get("name") or tag,
            "body": body.get("body") or "",
            "draft": bool(body.get("draft", False)),
            "prerelease": bool(body.get("prerelease", False)),
            "target_commitish": body.get("target_commitish") or "main",
            "created_at": self._now(),
            "published_at": None if body.get("draft") else self._now(),
            "author": {"login": "arga-bot"},
        }
        state["next_number"] = state.get("next_number", 1) + 1
        rows.append(rel)
        # 发 release 顺便把 tag 也立起来：真 GitHub 在有 target_commitish 时会这么做，
        # 而"发完 release 却没 tag"会让下一步的 tags 查询自相矛盾。
        refs = state.setdefault("git_refs", {}).setdefault(key, {})
        refs.setdefault(f"refs/tags/{tag}", hashlib.sha1((key + tag).encode()).hexdigest())
        return Response(201, self._release_obj(key, rel))

    def get_release_by_tag(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = f"{params['owner']}/{params['repo']}"
        rows = self.req_state(req).setdefault("releases", {}).setdefault(key, [])
        for r in rows:
            if r.get("tag_name") == params["tag"]:
                return Response(200, self._release_obj(key, r))
        return self._not_found("Not Found")

    # ---------- deployments ----------

    def _repo_key(self, req: Request, params: dict[str, str]) -> str | None:
        key = f"{params['owner']}/{params['repo']}"
        return key if key in self.req_state(req).get("repos", {}) else None

    # ---------- 补充：环境、单个部署、blob、matching-refs、ref 提交 ----------

    def get_deployment(self, req: Request, params: dict[str, str]) -> Response:
        """"这次部署到底成没成"要按 id 查状态，而 list 只能翻页找。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        for dep in self.req_state(req).get("deployments", {}).get(key, []):
            if str(dep.get("id")) == str(params["id"]):
                return Response(200, dep)
        return self._not_found()

    def list_environments(self, req: Request, params: dict[str, str]) -> Response:
        """环境是部署的父集合。只从 deployments 现推会漏掉"建了环境还没部署"的那一档，
        所以两者取并集：显式 `environments` 表 + 部署里出现过的 environment。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        state = self.req_state(req)
        names: list[str] = [e.get("name") for e in state.get("environments", {}).get(key, [])]
        for dep in state.get("deployments", {}).get(key, []):
            if dep.get("environment") and dep["environment"] not in names:
                names.append(dep["environment"])
        out = [{"id": 1000 + i, "name": n, "node_id": f"EN_{n}"} for i, n in enumerate(names)]
        return Response(200, {"total_count": len(out), "environments": out})

    def create_blob(self, req: Request, params: dict[str, str]) -> Response:
        """建 blob 是"先写对象再改树"的第一步。sha 必须由内容算出 ——
        随便给个随机值会让同一个内容出现两个 sha，后续按 sha 去重全部失效。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        body = req.json() or {}
        content = body.get("content") or ""
        encoding = body.get("encoding") or "utf-8"
        raw = content.encode("utf-8")
        if encoding == "base64":
            import base64 as _b64
            try:
                raw = _b64.b64decode(content + "=" * (-len(content) % 4))
            except Exception:
                return Response(400, {"message": "content is not valid base64"})
        digest = hashlib.sha1(raw).hexdigest()
        self.req_state(req).setdefault("blobs", {})[digest] = {
            "content": content, "encoding": encoding, "size": len(raw),
        }
        return Response(201, {"sha": digest, "url": f"/repos/{key}/git/blobs/{digest}",
                              "size": len(raw)})

    def matching_refs(self, req: Request, params: dict[str, str]) -> Response:
        """`git/matching-refs/tags/v2` 是"这个 tag 前缀有没有存在过"。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        want = params["ref"].rstrip("/")
        want = want if want.startswith("refs/") else f"refs/{want}"
        out = []
        for ref, sha in sorted(self._ref_shas(req, key).items()):
            full = ref if ref.startswith("refs/") else f"refs/{ref}"
            if full.startswith(want):
                out.append({"ref": full, "object": {"sha": sha, "type": "commit"}})
        return Response(200, out)

    def get_commit_by_ref(self, req: Request, params: dict[str, str]) -> Response:
        """`/commits/<ref>`：GitHub 允许用分支名或 tag 名取"那个 ref 指向的提交"。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        state = self.req_state(req)
        ref = params["ref"]
        refs = self._ref_shas(req, key)
        # `_ref_shas` 的键是完整 ref 名（refs/heads/main），而调用方写的是 main
        # 或 heads/main —— 三种写法都要能命中，否则"按分支查提交"永远 404。
        candidates = (ref, f"refs/{ref}", f"refs/heads/{ref}", f"refs/tags/{ref}",
                      ref.replace("refs/heads/", ""))
        sha = next((refs[c] for c in candidates if c in refs), None)
        if sha is None:
            return self._not_found()
        for c in state.get("commits", {}).get(key, []):
            if c.get("sha") == sha:
                return Response(200, c)
        return Response(200, {"sha": sha, "commit": {"message": ""}})

    def list_stargazers(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        return Response(200, [{"login": lg} for lg in
                              self.req_state(req).get("stargazers", {}).get(key, [])])

    def list_repo_teams(self, req: Request, params: dict[str, str]) -> Response:
        """仓库能被哪些团队看到，是权限类任务的判据之一。"""
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        slugs = self.req_state(req).get("repo_teams", {}).get(key, [])
        return Response(200, [{"slug": s, "name": s} for s in slugs])

    def list_workflows(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        rows = self.req_state(req).get("workflows", {}).get(key, [])
        return Response(200, {"total_count": len(rows), "workflows": rows})

    def get_org(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req)) is not None:
            return bad
        state = self.req_state(req)
        org = params["org"]
        repos = [k for k in state.get("repos", {}) if k.split("/")[0] == org]
        if not repos:
            return self._not_found()
        return Response(200, {"login": org, "id": abs(hash(org)) % 100000,
                              "public_repos": len(repos), "type": "Organization"})

    def list_deployments(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        rows = self.req_state(req).setdefault("deployments", {}).setdefault(key, [])
        env = req.query_one("environment")
        if env:
            rows = [d for d in rows if d.get("environment") == env]
        return Response(200, rows)

    def create_deployment(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        body = req.json() or {}
        ref = (body.get("ref") or "").strip()
        if not ref:
            # 真 GitHub 对缺 ref 回 422；回 400 会让客户端的重试逻辑走错分支
            return self._bad_request("ref is required")
        rows = self.req_state(req).setdefault("deployments", {}).setdefault(key, [])
        dep = {
            "id": 1000 + len(rows) + 1,
            "ref": ref,
            "sha": (self.req_state(req).get("branches", {}).get(key) or {}).get(ref, "0" * 40),
            "environment": body.get("environment") or "production",
            "description": body.get("description") or "",
            "task": body.get("task") or "deploy",
            "payload": body.get("payload") or {},
            "created_at": self._now(),
            "creator": {"login": "arga-bot"},
        }
        rows.append(dep)
        return Response(201, dep)

    def list_deployment_statuses(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        if self._repo_key(req, params) is None:
            return self._not_found()
        rows = self.req_state(req).setdefault("deployment_statuses", {}).get(str(params["id"])) or []
        return Response(200, rows)

    def create_deployment_status(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        deps = self.req_state(req).setdefault("deployments", {}).setdefault(key, [])
        if not any(str(d["id"]) == str(params["id"]) for d in deps):
            # 对不存在的 deployment 建 status 必须 404，否则判据会把孤儿状态当成功
            return self._not_found("Not Found")
        body = req.json() or {}
        state_name = (body.get("state") or "").strip()
        if not state_name:
            return self._bad_request("state is required")
        rows = self.req_state(req).setdefault("deployment_statuses", {}).setdefault(
            str(params["id"]), [])
        st = {
            "id": 5000 + len(rows) + 1,
            "state": state_name,
            "description": body.get("description") or "",
            "environment": body.get("environment") or "",
            "created_at": self._now(),
            "creator": {"login": "arga-bot"},
        }
        rows.append(st)
        return Response(201, st)

    # ---------- Actions：缓存与运行记录 ----------

    def list_actions_caches(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        rows = list(self.req_state(req).setdefault("actions_caches", {}).setdefault(key, []))
        return Response(200, {"total_count": len(rows), "actions_caches": rows})

    def delete_actions_cache(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        rows = self.req_state(req).setdefault("actions_caches", {}).setdefault(key, [])
        keep = [c for c in rows if str(c.get("id")) != str(params["id"])]
        if len(keep) == len(rows):
            return self._not_found("Not Found")
        rows[:] = keep
        return Response(204, None)

    def list_actions_runs(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        key = self._repo_key(req, params)
        if key is None:
            return self._not_found()
        rows = list(self.req_state(req).setdefault("actions_runs", {}).setdefault(key, []))
        branch = req.query_one("branch")
        if branch:
            rows = [r for r in rows if r.get("head_branch") == branch]
        return Response(200, {"total_count": len(rows), "workflow_runs": rows})

    def dispatch_workflow(self, req: Request, params: dict[str, str]) -> Response:
        if (bad := self._check(req, "workflow")) is not None:
            return bad
        # 真 GitHub 回 204 No Content
        return Response(204, None)

    def graphql(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "repo")) is not None:
            return bad
        body = req.json()
        query = body.get("query") or ""
        state = self.req_state(req)

        # 极小的子集：viewer 与 repository(owner,name){nameWithOwner}。
        # 不做通用解析——一个"看起来支持 GraphQL"的半吊子实现比明确只支持几条查询更危险。
        if "viewer" in query:
            return Response(200, {"data": {"viewer": {"login": "twin-bot"}}})

        m = re.search(r'repository\s*\(\s*owner\s*:\s*"([^"]+)"\s*,\s*name\s*:\s*"([^"]+)"', query)
        if m:
            key = f"{m.group(1)}/{m.group(2)}"
            repo = state["repos"].get(key)
            if not repo:
                return Response(200, {"data": {"repository": None}, "errors": [
                    {"type": "NOT_FOUND", "path": ["repository"], "message": "Could not resolve to a Repository"}
                ]})
            return Response(200, {"data": {"repository": {
                "nameWithOwner": key,
                "isPrivate": bool(repo.get("private")),
                "stargazerCount": repo.get("stargazers_count", 0),
            }}})

        # 不支持的查询要明确报错，不能回空 data 让调用方以为查询成功但没结果
        return Response(200, {
            "data": None,
            "errors": [{"type": "UNSUPPORTED", "message": "This twin only implements a small GraphQL subset."}],
        })

    # ---------- admin ----------

    def authenticate(self, req: Request) -> Response | None:
        return self._check(req, "")

    def handle_admin(self, req: Request) -> Response:
        path = req.path

        if path.startswith("/admin/scopes"):
            state = self.req_state(req)
            if req.method.upper() == "POST":
                body = req.json()
                if "granted_scopes" in body:
                    state["granted_scopes"] = list(body["granted_scopes"])
                if "disabled_scopes" in body:
                    off = set(body["disabled_scopes"])
                    state["granted_scopes"] = [
                        s for s in state["granted_scopes"] if s not in off
                    ]
            return Response(200, {
                "granted_scopes": state["granted_scopes"],
                "effective_scopes": sorted(self.effective_scopes(state)),
            })

        if path.startswith("/admin/deliveries"):
            state = self.req_state(req)
            return Response(200, {"deliveries": state["deliveries"][-20:]})

        if path.startswith("/admin/sign"):
            # 给测试与本地客户端算签名用：真 GitHub 从不提供这个端点，
            # 但一个沙箱如果不给算签名的手段，webhook 就只能靠猜
            body = req.json()
            payload = body.get("payload") or ""
            secret = body.get("secret") or ""
            sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            return Response(200, {"signature": f"sha256={sig}"})

        if path.startswith("/admin/verify-signature"):
            body = req.json()
            payload = body.get("payload") or ""
            secret = body.get("secret") or ""
            got = body.get("signature") or ""
            expected = "sha256=" + hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            return Response(200, {"valid": hmac.compare_digest(got, expected)})

        return Response(404, {"message": "no such admin route", "path": path})


# ---------- base64 小工具 ----------


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode()).decode()


def _unb64(data: str) -> str:
    import base64

    # GitHub 允许内容里带换行，客户端常常按 60 字符折行
    cleaned = "".join(data.split())
    try:
        return base64.b64decode(cleaned).decode("utf-8", "replace")
    except Exception:
        return ""
