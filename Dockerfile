# arga-clone：一个能自证的 SaaS 环境模拟器
#
# 为什么需要镜像：这套东西的判据全部依赖"我这台机器上的 python3 与网关"。
# 换一台机器，"能跑"就成了口头承诺；而任何一次 docker build 都能把承诺变成读数。
#
# 故意只装标准库：依赖是 0 的，所以镜像小、构建快、也不需要锁文件。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARGACLONE_DATA_DIR=/data

WORKDIR /app

# 先拷元数据再拷源码：改一行业务代码不该让 pip 那层缓存失效。
COPY pyproject.toml README.md ./
COPY argaclone ./argaclone
COPY scenarios ./scenarios
COPY scripts ./scripts
COPY demo ./demo
COPY tests ./tests
COPY pytest.ini Makefile ./

RUN pip install --no-cache-dir -e .

RUN mkdir -p /data

EXPOSE 8787

# 默认不挂 admin token：镜像给的是"本机单人调试"的默认姿势，
# 对外暴露时用 -e ARGACLONE_ADMIN_TOKEN=... 打开（README §21.1）。
CMD ["arga-clone", "serve", "--host", "0.0.0.0", "--port", "8787"]
