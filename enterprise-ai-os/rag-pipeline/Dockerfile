# =============================================================================
# rag-pipeline —— 多阶段构建
# 构建: docker build -t aios/rag-pipeline:dev .
# =============================================================================

# ---------- builder ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 文档解析（unstructured/pypdf）与部分科学计算包需要编译工具链
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libmagic1 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------- runtime ----------
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="aios-rag-pipeline" \
      org.opencontainers.image.description="Enterprise AI OS - RAG & vector retrieval pipeline" \
      org.opencontainers.image.source="https://github.com/your-org/enterprise-ai-os"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8100

# 运行期依赖：libmagic 用于文件类型嗅探，poppler 用于 PDF 文本抽取
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 aios

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=aios:aios src ./src
COPY --chown=aios:aios config ./config

USER aios
EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

# 单进程 + 多 worker 由编排层水平扩容控制；摄入 worker 通过 --workers 1 保证幂等
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "2", "--proxy-headers"]
