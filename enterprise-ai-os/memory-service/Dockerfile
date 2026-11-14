# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 构建期依赖：psycopg 的 C 扩展需要 libpq 与编译器。
# 装在 builder 阶段，运行阶段只留 libpq 运行库。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 用 venv 而非 --user：整个目录可以原样 COPY 到运行阶段，
# 路径不依赖用户 home，权限处理更简单。
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime ----------
FROM python:3.12-slim

# libpq5 是 psycopg 的运行时依赖；curl 用于 HEALTHCHECK。
# 与 rag-pipeline 的镜像不同，这里**不装** torch/sentence-transformers ——
# 记忆服务的 embedding 一律经 model-gateway 获取，不在本进程做推理。
# 这让镜像从 ~6GB 降到 ~200MB，也让本服务可以轻量地多副本部署。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 aios \
    && useradd -u 10001 -g aios -s /usr/sbin/nologin -M aios

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    MEMORY_CONFIG_PATH=/app/config/config.yaml \
    MEMORY_PORT=8400

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=aios:aios src/ /app/src/
COPY --chown=aios:aios config/config.yaml.example /app/config/config.yaml.example

USER 10001:10001

EXPOSE 8400

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8400/healthz || exit 1

LABEL org.opencontainers.image.title="Enterprise AI OS · Memory Service" \
      org.opencontainers.image.description="Long-term memory with personal/team/org scoping and consolidation" \
      org.opencontainers.image.vendor="Mininglamp" \
      org.opencontainers.image.licenses="Apache-2.0"

# workers 数从环境变量取。
# 注意：记忆服务的整合任务（consolidation）是后台周期任务，
# 多 worker 会导致重复执行。因此整合任务必须靠 Redis 锁做单实例保证，
# 或通过 MEMORY_CONSOLIDATION_ENABLED=false 关掉后由独立的 CronJob 执行。
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${MEMORY_PORT} --workers ${MEMORY_WORKERS:-2}"]
