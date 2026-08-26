# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime ----------
FROM python:3.12-slim

# ffmpeg 是本镜像里唯一"重"的运行时依赖（约 100MB），但省不掉：
# 会议音频格式五花八门（m4a / opus / webm / 各家会议软件的私有容器），
# 交给 octo-speech 之前必须统一转码，否则 ASR 引擎会拒收一半的输入。
#
# 与 rag-pipeline 一样，这里**不装** torch/whisper ——
# ASR 推理是 octo-speech 的职责，它需要 GPU 与模型缓存，
# 和本服务的伸缩特性完全不同，混在一个容器里两边都不好调度。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 aios \
    && useradd -u 10001 -g aios -s /usr/sbin/nologin -M aios

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    MEETING_CONFIG_PATH=/app/config/config.yaml \
    MEETING_PORT=8500

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=aios:aios src/ /app/src/
COPY --chown=aios:aios config/config.yaml.example /app/config/config.yaml.example

# 音频转码的临时目录。挂 emptyDir 或 tmpfs，不要用镜像层——
# 一场会议的中间文件可能上 GB，写进容器可写层会撑爆节点磁盘。
RUN mkdir -p /tmp/meeting-audio && chown aios:aios /tmp/meeting-audio
VOLUME ["/tmp/meeting-audio"]

USER 10001:10001

EXPOSE 8500

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8500/healthz || exit 1

LABEL org.opencontainers.image.title="Enterprise AI OS · Meeting Agent" \
      org.opencontainers.image.description="Meeting transcription, summarization, decision/action extraction and dispatch" \
      org.opencontainers.image.vendor="Mininglamp" \
      org.opencontainers.image.licenses="Apache-2.0"

# 注意 workers 与流式路径的关系：WebSocket 连接是有状态的
# （流式会话的分段缓冲在进程内），多 worker 下同一会议的重连可能落到
# 另一个进程而丢失上下文。要么把缓冲放 Redis，要么在网关做会话粘滞。
# 批量路径无此问题。
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${MEETING_PORT} --workers ${MEETING_WORKERS:-2}"]
