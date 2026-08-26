# =============================================================================
# model-gateway —— 多阶段构建
# 网关是纯 IO 密集型服务，无需模型权重与编译工具链，镜像可以做得很小
# =============================================================================

# ---------- builder ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------- runtime ----------
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="aios-model-gateway" \
      org.opencontainers.image.description="Enterprise AI OS - unified OpenAI-compatible model gateway" \
      org.opencontainers.image.source="https://github.com/your-org/enterprise-ai-os"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8200

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 aios

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=aios:aios src ./src
COPY --chown=aios:aios config ./config

USER aios
EXPOSE 8200

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

# 网关全程 async IO，worker 数按 CPU 核心配；--timeout-keep-alive 拉长以适配流式长连接
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", "--port", "8200", \
     "--workers", "4", \
     "--timeout-keep-alive", "120", \
     "--proxy-headers"]
