module github.com/mininglamp/enterprise-ai-os/workflow-engine

go 1.22

require (
	// HTTP 路由。选 chi 而非 gin：chi 基于标准库 http.Handler，
	// 中间件可与 octo-server 侧的既有中间件直接复用，无需适配层。
	github.com/go-chi/chi/v5 v5.0.12
	github.com/go-chi/cors v1.2.1

	// PostgreSQL。用 pgx 而非 database/sql + lib/pq：
	// 需要 FOR UPDATE SKIP LOCKED 的批量抢任务与 COPY 批量写事件日志，
	// pgx 的原生协议支持在这两处的性能差距明显。
	github.com/jackc/pgx/v5 v5.5.5

	// 数据库迁移。嵌入式执行（启动时自动 migrate），避免额外的部署步骤。
	github.com/golang-migrate/migrate/v4 v4.17.1

	// Redis：分布式锁（防止多副本同时调度同一 run）与审批通知去重。
	github.com/redis/go-redis/v9 v9.5.1

	// Kafka：工作流状态事件外发。segmentio 的实现无 CGO 依赖，
	// 便于构建 scratch/distroless 镜像。
	github.com/segmentio/kafka-go v0.4.47

	// 配置。viper 支持 YAML + 环境变量覆盖，与其他组件的
	// ${VAR:-default} 约定行为一致。
	github.com/spf13/viper v1.18.2

	// 结构化日志。用标准库 log/slog 的话 handler 生态还不够，
	// zap 与 octo-server 保持一致。
	go.uber.org/zap v1.27.0

	// 可观测性
	go.opentelemetry.io/otel v1.25.0
	go.opentelemetry.io/otel/trace v1.25.0
	github.com/prometheus/client_golang v1.19.0

	// DAG 节点的输入表达式求值（{{ .nodes.x.output.y }}）。
	// 直接用 text/template 而非引入表达式引擎：
	// 模板只做取值与简单条件，不需要图灵完备，
	// 越弱的表达能力越容易保证重放确定性。
	// 因此这里只需标准库，无额外依赖。

	// JSON Schema 校验 workflow input
	github.com/santhosh-tekuri/jsonschema/v5 v5.3.1

	// 重试退避
	github.com/cenkalti/backoff/v4 v4.3.0

	// 测试
	github.com/stretchr/testify v1.9.0
)
