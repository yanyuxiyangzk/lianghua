# RD-Agent 宿主侧容器：RD-Agent 本体 + docker CLI
# RD-Agent 进程在此容器内运行，通过 /var/run/docker.sock 驱动宿主机 dockerd
# 启动 local_qlib 子容器执行 qrun 回测。
FROM python:3.11-slim

# 国内网络：Debian + PyPI 均走清华镜像
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        docker-cli \
    && rm -rf /var/lib/apt/lists/*

# 固定版本，保证与 qsys 看板解析端一致（pickle 反序列化依赖同类定义）
# pydantic-ai-slim 必须按 RD-Agent 上游要求钉在 1.66.0：
# rdagent 0.8.0 未设上限，pip 会装到 2.x（MCPServerStreamableHTTP 已移除）导致 CLI 起不来
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir rdagent==0.8.0 "pydantic-ai-slim[mcp,openai,prefect]==1.66.0"

CMD ["tail", "-f", "/dev/null"]
