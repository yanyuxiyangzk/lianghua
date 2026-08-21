# QSYS(QuantSys) 自研看板：只读 RD-Agent/Qlib 产出，不承担任何因子生成逻辑
FROM python:3.11-slim

RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# rdagent 与主容器同版本：反序列化 log/*.pkl 中的 Hypothesis/Experiment 对象
# pyqlib：K线读数 + 回测产物解析；apscheduler：进程内定时调度；tables：h5 读写
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir \
        streamlit==1.61.1 \
        plotly==6.9.0 \
        pyqlib==0.9.7 \
        rdagent==0.8.0 \
        "pydantic-ai-slim[mcp,openai,prefect]==1.66.0" \
        apscheduler==3.11.0 \
        akshare==1.18.87 \
        streamlit-autorefresh==1.0.1 \
        easy-tdx==1.20.6 \
        tables

WORKDIR /app
COPY . /app
RUN chmod +x /app/entrypoint.sh

EXPOSE 8501
CMD ["bash", "/app/entrypoint.sh"]
