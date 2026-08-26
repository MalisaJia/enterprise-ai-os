// Command server 是 workflow-engine 的进程入口。
//
// 本文件只负责"装配"：读配置 → 建依赖 → 注册活动 → 起服务 → 优雅退出。
// 不含任何业务逻辑。保持入口薄的好处是启动顺序与依赖关系一目了然，
// 排查"为什么启动失败"时只需看这一个文件。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// 以下变量由构建时的 -ldflags 注入，见 Dockerfile。
// 不在代码里硬编码版本号，避免"改了代码忘记改版本"。
var (
	version   = "dev"
	gitCommit = "unknown"
	buildTime = "unknown"
)

func main() {
	var (
		configPath  = flag.String("config", envOr("WORKFLOW_CONFIG_PATH", "config/config.yaml"), "配置文件路径")
		showVersion = flag.Bool("version", false, "打印版本后退出")
		migrateOnly = flag.Bool("migrate-only", false, "只执行数据库迁移后退出")
		validate    = flag.String("validate", "", "校验指定的 DAG 定义文件后退出（不启动服务）")
	)
	flag.Parse()

	if *showVersion {
		fmt.Printf("workflow-engine %s (commit %s, built %s)\n", version, gitCommit, buildTime)
		return
	}

	if err := run(*configPath, *migrateOnly, *validate); err != nil {
		// 用 stderr 而非 logger：启动早期 logger 可能还没建起来，
		// 而这条信息是排查启动失败的唯一线索，绝不能丢。
		fmt.Fprintf(os.Stderr, "fatal: %v\n", err)
		os.Exit(1)
	}
}

// run 是可测试的主流程。
//
// 把逻辑从 main 抽出来是为了让 main 里只剩 os.Exit——
// 有 os.Exit 的函数无法被测试覆盖（它会直接终止测试进程）。
func run(configPath string, migrateOnly bool, validateFile string) error {
	// ----------------------------------------------------------------
	// 1. 配置
	// ----------------------------------------------------------------
	// TODO: 用 viper 加载 YAML 并展开 ${VAR:-default}。
	//   环境变量优先级高于文件——容器化部署时靠环境变量覆盖是标准做法。
	//   加载后必须做完整性校验（见 config 包），配置错误应当**启动即失败**，
	//   而不是运行到用上那个字段时才崩。后者可能是上线几小时后的事。
	var cfg *Config
	_ = cfg

	// ----------------------------------------------------------------
	// 2. 日志与可观测性
	// ----------------------------------------------------------------
	// TODO: 建 zap logger（按 cfg.Logging.Format 选 json/console）。
	// TODO: cfg.Observability.Tracing.Enabled 时初始化 OTel exporter，
	//   并把 shutdown 函数注册到退出链——不 flush 会丢掉最后一批 span。

	// ----------------------------------------------------------------
	// 3. 数据库
	// ----------------------------------------------------------------
	// TODO: pgxpool 建连接池。
	// TODO: cfg.Database.AutoMigrate 时执行迁移（golang-migrate + //go:embed）。
	//   多副本同时启动靠 golang-migrate 的建议锁串行化，无需额外协调。
	//   迁移失败必须终止启动：带着旧 schema 跑新代码会产生难以诊断的错误。
	if migrateOnly {
		// TODO: 执行迁移后返回。用于 K8s 的 initContainer 或独立 Job 模式。
		return nil
	}

	// ----------------------------------------------------------------
	// 4. Redis
	// ----------------------------------------------------------------
	// TODO: go-redis 客户端。用途仅两项：leader 选举、审批通知去重。
	//   Redis 不可用时**不应阻止启动**——降级为单副本模式（本地锁）仍能工作，
	//   而拒绝启动会让 Redis 抖动直接变成引擎整体不可用。

	// ----------------------------------------------------------------
	// 5. 活动注册表
	// ----------------------------------------------------------------
	// TODO: activity.BuildBuiltins(deps, enabledMap)。
	//   注意 http.request 的 AddressGuard 必须在此处构造并校验配置：
	//   allowlist 模式但白名单为空 → 该活动实际不可用，应当打 WARN 日志
	//   明确告知，而不是让使用者在运行时收到一个费解的 blocked 错误。

	// ----------------------------------------------------------------
	// 6. 审批模块
	// ----------------------------------------------------------------
	// TODO: approval.NewHandler。
	//   CallbackSecret 为空时：dev 环境打 WARN 继续，prod 环境**拒绝启动**。
	//   审批回调是唯一能从外部改变业务结果的入口，不验签等于把审批权公开。

	// ----------------------------------------------------------------
	// 7. 引擎
	// ----------------------------------------------------------------
	// TODO: engine.NewExecutor → engine.NewScheduler → scheduler.Start(ctx)。
	//   NewScheduler 内部会校验 HeartbeatTimeout ≥ 5×HeartbeatInterval，
	//   配错则拒绝启动（原因见 scheduler.go 注释：会导致节点被并发执行）。

	// ----------------------------------------------------------------
	// 8. 校验模式（--validate）
	// ----------------------------------------------------------------
	if validateFile != "" {
		// TODO: 读文件 → workflow.Parse(data, limits) → 打印结果后退出。
		//   这个模式的价值：CI 里可以在不连数据库、不起服务的前提下
		//   校验所有流程定义，把环、笔误、未知活动类型拦在合并之前。
		return nil
	}

	// ----------------------------------------------------------------
	// 9. HTTP 服务
	// ----------------------------------------------------------------
	// TODO: api.NewHandler(...).Routes() 装配路由。
	srv := &http.Server{
		// TODO: Addr / Handler 从 cfg 与 api.Handler 取。
		//
		// ReadHeaderTimeout 必须设：不设的话慢速 header 攻击
		// 可以用极少的资源耗尽服务端连接。这是 Go HTTP 服务
		// 最常被忽略的一项加固。
		ReadHeaderTimeout: 10 * time.Second,
		// WriteTimeout 刻意留 0（不限制）：
		// 事件日志的 SSE 端点是长连接，设了写超时会被强制切断。
		// 单请求超时由中间件按路径粒度控制，比全局一刀切更合适。
		ReadTimeout: 60 * time.Second,
		IdleTimeout: 120 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- fmt.Errorf("http server: %w", err)
		}
	}()

	// ----------------------------------------------------------------
	// 10. 等待退出信号
	// ----------------------------------------------------------------
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		// 收到 SIGTERM。K8s 的 terminationGracePeriodSeconds
		// 必须大于这里的 shutdown_timeout，否则会被 SIGKILL 打断收尾。
	}

	// ----------------------------------------------------------------
	// 11. 优雅退出
	// ----------------------------------------------------------------
	// 顺序是反向的，且每一步的位置都有理由：
	//
	//  1. HTTP 先停 —— 不再接收新请求。放在最前面是因为它最"外层"，
	//     先停它才能让内部状态稳定下来
	//  2. Scheduler 停 —— 不再领新任务，等 in-flight 节点收尾。
	//     这是最耗时的一步，也是 shutdown_timeout 主要花费的地方
	//  3. Resign 让出领导权 —— 让新 leader 立即接手维护任务，
	//     不然要等租约自然过期（最长 leader_lock_ttl）才有人接管
	//  4. Kafka producer flush —— 保证 outbox 已发出的事件不丢
	//  5. DB / Redis 连接池关闭 —— 最后关，前面几步都还要用
	//  6. OTel flush —— 真正的最后一步，否则前面的收尾 span 全丢
	//
	// 特别注意第 2 步超时后的处理：**不要强杀 in-flight 节点**。
	// 让租约自然过期由其他副本回收重试，比留下一个
	// "状态是 running 但没人在跑"的记录要好——后者要等到租约超时
	// 才会被回收，中间的时间完全浪费，且期间该节点看起来是正常运行的，
	// 掩盖了问题。
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		// 关闭失败只记日志，继续执行后续清理——
		// 提前 return 会跳过 Kafka flush，丢事件的代价比 HTTP 没关干净大得多。
		fmt.Fprintf(os.Stderr, "warn: http shutdown: %v\n", err)
	}

	// TODO: scheduler.Stop(shutdownCtx)
	// TODO: elector.Resign / kafka.Close / pool.Close / redis.Close / otel.Shutdown

	return nil
}

// envOr 读环境变量，为空时返回默认值。
func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// Config 是配置的顶层结构，与 config/config.yaml.example 一一对应。
//
// 为什么放在 main 包而不是单独的 config 包：脚手架阶段先集中，
// 待字段稳定后再抽出。过早拆包会让每次调整字段都要改两个地方。
//
// TODO: 字段稳定后移到 internal/config，并补 Validate 方法。
type Config struct {
	Service struct {
		Name            string        `mapstructure:"name"`
		Env             string        `mapstructure:"env"`
		Version         string        `mapstructure:"version"`
		Host            string        `mapstructure:"host"`
		Port            int           `mapstructure:"port"`
		ShutdownTimeout time.Duration `mapstructure:"shutdown_timeout"`
	} `mapstructure:"service"`

	Logging struct {
		Level  string `mapstructure:"level"`
		Format string `mapstructure:"format"`
	} `mapstructure:"logging"`

	Database struct {
		DSN              string        `mapstructure:"dsn"`
		Schema           string        `mapstructure:"schema"`
		MaxConns         int32         `mapstructure:"max_conns"`
		MinConns         int32         `mapstructure:"min_conns"`
		AutoMigrate      bool          `mapstructure:"auto_migrate"`
		StatementTimeout time.Duration `mapstructure:"statement_timeout"`
	} `mapstructure:"database"`

	Redis struct {
		URL                 string        `mapstructure:"url"`
		KeyPrefix           string        `mapstructure:"key_prefix"`
		LeaderLockTTL       time.Duration `mapstructure:"leader_lock_ttl"`
		LeaderRenewInterval time.Duration `mapstructure:"leader_renew_interval"`
	} `mapstructure:"redis"`

	Scheduler struct {
		WorkerPoolSize           int           `mapstructure:"worker_pool_size"`
		PollInterval             time.Duration `mapstructure:"poll_interval"`
		PollBatchSize            int           `mapstructure:"poll_batch_size"`
		NotifyEnabled            bool          `mapstructure:"notify_enabled"`
		NotifyChannel            string        `mapstructure:"notify_channel"`
		HeartbeatInterval        time.Duration `mapstructure:"heartbeat_interval"`
		HeartbeatTimeout         time.Duration `mapstructure:"heartbeat_timeout"`
		MaxConcurrentNodesPerRun int           `mapstructure:"max_concurrent_nodes_per_run"`
		MaxConcurrentRuns        int           `mapstructure:"max_concurrent_runs"`
	} `mapstructure:"scheduler"`

	Executor struct {
		DefaultNodeTimeout time.Duration `mapstructure:"default_node_timeout"`
	} `mapstructure:"executor"`

	Workflow struct {
		MaxNodes            int  `mapstructure:"max_nodes"`
		MaxDepth            int  `mapstructure:"max_depth"`
		MaxParallelBranches int  `mapstructure:"max_parallel_branches"`
		AllowLoops          bool `mapstructure:"allow_loops"`
		MaxLoopIterations   int  `mapstructure:"max_loop_iterations"`
	} `mapstructure:"workflow"`

	Approval struct {
		DefaultTimeout   time.Duration `mapstructure:"default_timeout"`
		DefaultOnTimeout string        `mapstructure:"default_on_timeout"`
		DefaultMode      string        `mapstructure:"default_mode"`
	} `mapstructure:"approval"`

	Auth struct {
		Enabled     bool   `mapstructure:"enabled"`
		OctoAuthURL string `mapstructure:"octo_auth_url"`
	} `mapstructure:"auth"`

	Events struct {
		Enabled bool   `mapstructure:"enabled"`
		Brokers string `mapstructure:"brokers"`
	} `mapstructure:"events"`
}
