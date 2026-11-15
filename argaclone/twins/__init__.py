"""各 SaaS 的仿真器。新增一个 twin = 新建一个文件 + 在 server.build_registry() 注册。"""

from .base import Request, Response, Route, Twin, TwinRegistry
from .slack_twin import SlackTwin
from .stripe_twin import StripeTwin

__all__ = [
    "Request",
    "Response",
    "Route",
    "Twin",
    "TwinRegistry",
    "SlackTwin",
    "StripeTwin",
]
