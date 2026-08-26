package engine

import (
	"context"
	"sync"
	"time"

	"github.com/mininglamp/enterprise-ai-os/workflow-engine/internal/workflow"
)

// SchedulerConfig 是调度器参数。
type SchedulerConfig struct {
	WorkerPoolSize int

	// PollInterval 是 ready 队列的轮询间隔。
	// 配合 NotifyEnabled 时这只是兜底：正常路径由 PG NOTIFY 即时唤醒。
	PollInterval  time.Duration
	PollBatchSize int

	// NotifyEnabled 开启 PG LISTEN/NOTIFY 即时唤醒。
	//
	// 为什么不能只靠 NOTIFY：NOTIFY 不保证送达——连接断开期间的通知
	// 会永久丢失，导致任务滞留在 ready 但没人来取。轮询必须保留。
	NotifyEnabled bool
	NotifyChannel string

	HeartbeatInterval time.Duration
	HeartbeatTimeout  time.Duration
	LeaseTTL          time.Duration

	// MaxConcurrentNodesPerRun 防止单个宽 DAG 独占 worker 池。
	MaxConcurrentNodesPerRun int
	MaxConcurrentRuns        int

	// ReclaimInterval 是过期租约扫描周期。只有 leader 执行。
	ReclaimInterval time.Duration
	// ApprovalTimeoutScanInterval 是审批超时扫描周期。只有 leader 执行。
	ApprovalTimeoutScanInterval time.Duration

	ShutdownTimeout time.Duration
}

// LeaderElector 提供 leader 选举，保证周期性维护任务只在一个副本执行。
//
// 为什么需要：回收过期租约、扫描审批超时、清理历史数据这三类任务
// 若在每个副本都跑，会互相竞争行锁并产生重复的副作用
// （比如同一个审批超时被处理两次，发出两条超时通知）。
type LeaderElector interface {
	// Campaign 阻塞直到成为 leader 或 ctx 取消。
	Campaign(ctx context.Context) error
	// IsLeader 返回当前是否持有领导权。
	//
	// 实现必须在租约续约失败时立即返回 false。
	// 注意这里存在固有的脑裂窗口（旧 leader 尚未发现自己失去领导权），
	// 因此所有 leader-only 任务本身也必须是幂等的——
	// 不能把正确性完全建立在选举之上。
	IsLeader() bool
	// Resign 主动让出领导权，用于优雅退出。
	Resign(ctx context.Context) error
}

// Notifier 提供任务入队通知，用于把轮询延迟降到接近 0。
type Notifier interface {
	// Listen 返回一个通道，有新就绪任务时收到信号。
	Listen(ctx context.Context, channel string) (<-chan struct{}, error)
}

// Scheduler 是引擎的主循环：从 ready 队列取任务，分发给 worker 池。
//
// 并发模型：一个 dispatch goroutine 负责取任务并投递到 taskCh，
// WorkerPoolSize 个 worker goroutine 从 taskCh 消费。
//
// 为什么用固定大小的 worker 池而非 "每任务一 goroutine"：
// goroutine 本身很便宜，但每个节点执行会持有一条 DB 连接与若干下游 HTTP 连接。
// 无界并发会瞬间打满连接池，并把下游服务压垮。池大小是这些资源的实际闸门。
type Scheduler struct {
	cfg      SchedulerConfig
	store    Store
	executor *Executor
	elector  LeaderElector
	notifier Notifier
	workerID string

	taskCh chan *workflow.NodeRun

	// runNodeCount 跟踪每个 run 当前 in-flight 的节点数，
	// 用于执行 MaxConcurrentNodesPerRun。
	mu           sync.Mutex
	runNodeCount map[string]int

	wg       sync.WaitGroup
	stopOnce sync.Once
	stopCh   chan struct{}
}

// NewScheduler 构造调度器。
//
// TODO:
//  1. 校验配置：HeartbeatTimeout 必须显著大于 HeartbeatInterval（建议 ≥5 倍），
//     否则 GC 停顿或网络抖动就会让活着的 worker 被判死，
//     同一节点被两个 worker 并发执行——对非幂等活动是灾难。
//     这个校验必须在启动时做，配错了就拒绝启动
//  2. workerID 用 hostname + pid + 随机后缀，保证跨副本唯一且可读
//     （日志里能一眼看出是哪个 Pod）
func NewScheduler(
	cfg SchedulerConfig,
	store Store,
	executor *Executor,
	elector LeaderElector,
	notifier Notifier,
) (*Scheduler, error) {
	return nil, ErrNotImplemented
}

// Start 启动调度器。非阻塞，返回后各协程已在运行。
//
// 启动的协程清单：
//   - 1 × dispatchLoop     ：取任务并投递
//   - N × workerLoop       ：执行任务
//   - 1 × leaderLoop       ：参选 leader
//   - 1 × maintenanceLoop  ：leader-only 的回收与超时扫描
//   - 1 × notifyLoop       ：监听 PG NOTIFY（若启用）
//
// TODO: 按上述清单起协程，全部纳入 s.wg 以便 Stop 时等待。
func (s *Scheduler) Start(ctx context.Context) error {
	return ErrNotImplemented
}

// Stop 优雅停止。
//
// 停止顺序（顺序错了会丢任务或产生重复执行）：
//  1. 停止 dispatchLoop —— 不再领取新任务
//  2. 关闭 taskCh —— worker 处理完手上的任务后自然退出
//  3. 等待 wg，最多 ShutdownTimeout
//  4. 超时仍有 in-flight 任务时：**不要**强杀。
//     让租约自然过期，由其他副本回收重试。强杀会让节点停在 running 状态，
//     且本副本已无机会写入结果，只能等租约超时——不如直接让它超时
//  5. Resign 让出领导权，让新 leader 立即接手维护任务
//
// TODO: 按上述顺序实现。
func (s *Scheduler) Stop(ctx context.Context) error {
	return ErrNotImplemented
}

// dispatchLoop 是取任务主循环。
//
// TODO:
//  1. select { <-ticker.C, <-notifyCh, <-stopCh }
//  2. 计算本轮可领取数量 = min(PollBatchSize, 空闲 worker 数)。
//     不做这个约束会导致领了任务但没 worker 执行，租约在队列里空转到期
//  3. store.ClaimReadyNodes 领取
//  4. 按 MaxConcurrentNodesPerRun 过滤：超限的 run 的节点先不投递
//     （注意要把它们的租约释放回 ready，否则会空等到租约超时）
//  5. 投递到 taskCh
//  6. 领到 0 条时可做指数退避，降低空转时的 DB 压力
func (s *Scheduler) dispatchLoop(ctx context.Context) {
}

// workerLoop 是单个 worker 的消费循环。
//
// TODO:
//  1. for nodeRun := range s.taskCh
//  2. 增加 runNodeCount，defer 减少
//  3. executor.Execute(ctx, nodeRun)
//  4. **错误必须只记日志不 return**。一个节点执行失败不能让 worker 退出，
//     否则连续几个失败就把 worker 池耗光，引擎彻底停摆。
//     Execute 内部已负责把失败落库
//  5. recover panic：活动实现可能 panic（如类型断言失败），
//     不 recover 会连带整个进程崩溃，把其他副本的负载也顶上去
func (s *Scheduler) workerLoop(ctx context.Context, id int) {
}

// leaderLoop 持续参选 leader。
//
// TODO: Campaign 阻塞等待当选；当选后不做别的，只维持租约。
// 失去领导权后重新参选（不要退出循环）。
func (s *Scheduler) leaderLoop(ctx context.Context) {
}

// maintenanceLoop 执行 leader-only 的周期性维护。
//
// 三项任务：
//  1. ReclaimExpiredLeases —— 回收僵死节点。
//     这是引擎自愈能力的核心：Pod 被驱逐、进程 OOM、网络分区，
//     都靠它把任务捞回来。没有它，一次 Pod 重启就会有若干流程永久卡住
//  2. 审批超时扫描 —— waiting 节点超过 deadline 时按 on_timeout 处理
//  3. run 级超时 —— 超过 max_run_duration 的 run 强制 canceled
//
// TODO: 每项任务前检查 IsLeader()；非 leader 直接跳过本轮。
// 由于选举存在脑裂窗口，各项任务本身也要幂等
// （例如超时处理前用 CAS 校验节点状态仍是 waiting）。
func (s *Scheduler) maintenanceLoop(ctx context.Context) {
}

// StartRun 提交一个新的工作流实例。由 API 层调用。
//
// TODO:
//  1. 校验 IdempotencyKey：已存在则返回既有 run（不报错）。
//     这是事件触发场景的必需品——Kafka 至少一次投递会重复消费
//  2. 载入定义（未指定版本则取最新），JSON Schema 校验 input
//  3. 检查租户配额与 MaxConcurrentRuns
//  4. **单事务内**：插 run、为所有节点插 node_run（pending）、
//     把入口节点置 ready、追加 run.created 事件、写 outbox
//  5. 若启用 NOTIFY，事务提交后发通知（提交前发会让监听方读到未提交数据）
func (s *Scheduler) StartRun(ctx context.Context, req StartRunRequest) (*workflow.Run, error) {
	return nil, ErrNotImplemented
}

// StartRunRequest 是启动实例的入参。
type StartRunRequest struct {
	TenantID       string
	SpaceID        string
	DefinitionName string
	// Version 为 0 表示使用最新版本。
	Version        int
	Input          map[string]any
	IdempotencyKey string
	Priority       int
	TriggeredBy    string
	TraceID        string
}

// PauseRun 暂停实例。
//
// 语义：已 running 的节点跑完，不再调度新节点。
// **不是**中断正在执行的节点——那会产生"已发生副作用但状态是未完成"的记录。
//
// TODO: run 状态置 paused；ready 节点退回 pending。
func (s *Scheduler) PauseRun(ctx context.Context, runID string) error {
	return ErrNotImplemented
}

// ResumeRun 恢复暂停的实例。
//
// TODO: 状态置 running，重新计算哪些节点依赖已满足并置 ready。
// 注意要重新计算而不是简单地把 pending 全置 ready——
// 暂停期间可能有节点完成，依赖关系已变化。
func (s *Scheduler) ResumeRun(ctx context.Context, runID string) error {
	return ErrNotImplemented
}

// CancelRun 取消实例。
//
// TODO:
//  1. run 置 canceled，未开始的节点置 canceled
//  2. running 节点：通过 context 取消尽力中断，但**不等待**——
//     取消操作应当立即返回，否则一个卡在 30 分钟超时的 Agent 调用
//     会让取消请求也卡 30 分钟
//  3. waiting 的审批单标记为已取消并撤回 IM 卡片
//     （不撤回会让审批人对着一个已作废的流程做决策）
func (s *Scheduler) CancelRun(ctx context.Context, runID string, reason string) error {
	return ErrNotImplemented
}

// RetryRun 从失败处重跑。
//
// 关键语义：**只重跑失败及其下游节点，已成功的节点保留输出不重跑。**
// 全量重跑会重复产生副作用（重复发消息、重复扣费），
// 且浪费已经花掉的 LLM 调用成本。
//
// TODO:
//  1. 校验 run 处于 failed 状态
//  2. 找到失败节点，将其与全部下游节点重置为 pending（attempt 归零）
//  3. 失败节点若依赖已满足则置 ready
//  4. run 状态回到 running，追加事件记录本次重试的发起人
func (s *Scheduler) RetryRun(ctx context.Context, runID string, actor string) error {
	return ErrNotImplemented
}

// Stats 返回调度器运行时指标，供 /metrics 与 /readyz 使用。
type Stats struct {
	WorkerPoolSize  int   `json:"worker_pool_size"`
	BusyWorkers     int   `json:"busy_workers"`
	QueuedTasks     int   `json:"queued_tasks"`
	ActiveRuns      int   `json:"active_runs"`
	ReadyQueueDepth int64 `json:"ready_queue_depth"`
	IsLeader        bool  `json:"is_leader"`

	// LeaseExpiredTotal 非 0 说明有 worker 失联过。
	// 这个指标持续增长是明确的告警信号——
	// 要么 worker 被 OOM kill，要么 HeartbeatTimeout 配得太紧。
	LeaseExpiredTotal int64 `json:"lease_expired_total"`
}

// Stats 采集当前指标。
//
// TODO: 组合内存计数与一次 DB 聚合查询（ready 队列深度必须查库，
// 因为它是跨副本的全局值）。
func (s *Scheduler) Stats(ctx context.Context) (*Stats, error) {
	return nil, ErrNotImplemented
}
