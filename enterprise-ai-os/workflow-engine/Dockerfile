# ---------- build ----------
# 多阶段构建。Go 的静态编译让运行阶段可以极小，
# 这对工作流引擎尤其有价值：引擎需要多副本部署，镜像每小 100MB，
# 集群整体的拉取时间与磁盘占用都成倍受益。
FROM golang:1.22-alpine AS builder

WORKDIR /build

# 先只拷 go.mod/go.sum 并下载依赖，让依赖层可被缓存。
# 业务代码变动不会导致重新拉取整个模块树。
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO_ENABLED=0：纯静态二进制，才能跑在 distroless/scratch 上。
# -trimpath：去掉构建机的绝对路径，让构建可复现。
# -ldflags "-s -w"：去符号表与调试信息，二进制体积减半。
# 版本信息通过 ldflags 注入，避免代码里硬编码版本号。
ARG VERSION=0.1.0
ARG GIT_COMMIT=unknown
RUN CGO_ENABLED=0 GOOS=linux go build \
        -trimpath \
        -ldflags "-s -w -X main.version=${VERSION} -X main.gitCommit=${GIT_COMMIT}" \
        -o /out/workflow-engine \
        ./cmd/server

# ---------- runtime ----------
# 用 alpine 而非 scratch/distroless：引擎需要
# 1) CA 证书（调 octo-server / model-gateway 的 HTTPS）
# 2) tzdata（审批超时按业务时区计算，UTC 会算错"次日 18:00 截止"）
# 3) 一个 shell 用于线上排障（wget 探针、临时 curl）
# 这三点值得多付 ~8MB。
FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata wget \
    && addgroup -g 10001 -S aios \
    && adduser -u 10001 -S aios -G aios

WORKDIR /app

COPY --from=builder /out/workflow-engine /app/workflow-engine
# 注意：数据库迁移脚本通过 //go:embed 编译进二进制（见 internal/store），
# 不作为独立文件分发。这样镜像与迁移版本天然一致，
# 不会出现"二进制是新的、挂载的迁移脚本是旧的"这种最难排查的状况。

# 配置通过挂载提供，镜像里只放 example 供参考
COPY --chown=aios:aios config/config.yaml.example /app/config/config.yaml.example

USER 10001:10001

ENV TZ=Asia/Shanghai \
    WORKFLOW_CONFIG_PATH=/app/config/config.yaml \
    WORKFLOW_PORT=8300

EXPOSE 8300

# start-period 给 30s：启动时要跑数据库迁移，冷启动可能较慢。
# 设得太短会导致迁移期间被反复重启，永远起不来。
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8300/healthz || exit 1

LABEL org.opencontainers.image.title="Enterprise AI OS · Workflow Engine" \
      org.opencontainers.image.description="DAG workflow engine with durable state and human approval" \
      org.opencontainers.image.vendor="Mininglamp" \
      org.opencontainers.image.licenses="Apache-2.0"

ENTRYPOINT ["/app/workflow-engine"]
