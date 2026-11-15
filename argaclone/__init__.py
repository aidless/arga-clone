"""Arga 复刻品：真实世界沙箱的最小可用实现。

模块地图：
  store.py      run 生命周期 / 快照 reset / 共享实体 / stub 观测
  gateway.py    Host → (provider, run_id) 解析
  twins/        各 SaaS 的仿真器（stripe / slack）
  scenarios.py  seed + 能力收窄
  runner.py     动作执行 + trace（provider 感知的成功判定）
  graders.py    三态判分（PASS / FAIL / UNSAFE）
  server.py     admin API + 全部 twin 单端口
"""

__version__ = "0.1.0"
