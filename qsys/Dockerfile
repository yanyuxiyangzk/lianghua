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

# 官方 iFinDPy Linux SDK 不在 PyPI，且不是 pip 包：从 quantapi.51ifind.com
# 下载的 tar.gz 解压后是 bin64/（iFinDPy.py + libShellExport.so 等原生库），
# 官方安装方式 = 解压 + 往 site-packages 写 iFinDPy.pth 指向 bin64（该 .pth
# 同时被 iFinDPy.py 用来定位 libShellExport.so）。把官方 tar.gz 放进
# qsys/ifind_sdk/（构建上下文内）再 build 即可；目录为空时跳过，镜像照常构建。
# 另：SDK 的 .so 链接了旧版 libidn.so.11，trixie 只有 libidn12——符号未实际
# 引用且版本标签一致，软链即可；libhcrypt-3.1.so 走 ldconfig 注册目录解决。
COPY ifind_sdk /tmp/ifind_sdk
RUN set -eux; \
    pkg=$(ls /tmp/ifind_sdk/*.tar.gz /tmp/ifind_sdk/*.tgz 2>/dev/null | head -1 || true); \
    if [ -n "$pkg" ]; then \
      sub=bin64; [ "$(uname -m)" = "x86_64" ] || sub=bin; \
      mkdir -p /opt/iFinD; \
      tar xzf "$pkg" -C /opt/iFinD; \
      chmod +x /opt/iFinD/$sub/*.so /opt/iFinD/$sub/hqdatafeed; \
      python -c "import sysconfig; open(sysconfig.get_paths()['purelib']+'/iFinDPy.pth','w').write('/opt/iFinD/$sub\n')"; \
      echo "/opt/iFinD/$sub" > /etc/ld.so.conf.d/ifind.conf; \
      apt-get update; \
      apt-get install -y --no-install-recommends libidn12; \
      rm -rf /var/lib/apt/lists/*; \
      [ -e /usr/lib/x86_64-linux-gnu/libidn.so.11 ] || \
        ln -s /usr/lib/x86_64-linux-gnu/libidn.so.12 /usr/lib/x86_64-linux-gnu/libidn.so.11; \
      ldconfig; \
      python -c "import iFinDPy; print('iFinDPy SDK bundled:', iFinDPy.__file__)"; \
    else \
      echo 'iFinDPy SDK not bundled (place official tar.gz in qsys/ifind_sdk/)'; \
    fi; \
    rm -rf /tmp/ifind_sdk

WORKDIR /app
COPY . /app
RUN chmod +x /app/entrypoint.sh

EXPOSE 8501
CMD ["bash", "/app/entrypoint.sh"]
