// Package engine 实现工作流的运行时：调度器与执行器。
//
// 职责划分（这条边界很重要，混在一起会导致两者都难测）：
//   - Scheduler：决定"接下来执行哪个节点"。只碰状态与队列，不碰业务逻辑。
//   - Executor：决定"这个节点怎么执行"。只处理单个节点，不关心全局。
//
// 这样 Scheduler 可以用 fake Executor 测调度正确性，
// Executor 可以脱离调度器单独测活动执行与重试。
package engine

import (
	"context"
	"errors"
	"time"

	"github.com/mininglamp/enterprise-ai-os/workflow-engine/internal/workflow"
)

var (
	ErrNotImplemented = errors.New("engine: not implemented")
	// ErrLeaseLost 表示执行期间租约被调度器回收（本 worker 被判定为失联）。
	// 出现此错误时**必须立即放弃写入结果**——另一个 worker 可能已在执行同一节点，
	// 两边都写会导致输出互相覆盖。
	ErrLeaseLost = errors.New("engine: node lease lost")
	// ErrNodeTimeout 节点执行超过 timeout。
	ErrNodeTimeout = errors.New("engine: node execution timeout")
	// ErrRunCanceled run 在节点执行期间被取消。
	ErrRunCanceled = errors.New("engine: run canceled")
	// ErrNonIdempotentRetry 非幂等节点在不确定是否已执行的情况下拒绝重试。
	ErrNonIdempotentRetry = errors.New("engine: refusing to retry non-idempotent node")
)

// Store 是执行器与调度器对持久化层的依赖抽象。
//
// 定义成接口而非直接依赖具体实现，主要动机是测试：
// 引擎的核心逻辑（依赖判定、重试决策、状态迁移）不该需要一个真实的 PostgreSQL
// 才能验证。次要动机是把 SQL 集中在一处，便于统一加租户过滤与超时。
type Store interface {
	// ClaimReadyNodes 原子地领取一批就绪节点并写入租约。
	//
	// 实现必须用 UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
	// 的单语句形式。分成"先 SELECT 再 UPDATE"两步会有竞态：
	// 两个副本可能 SELECT 到同一批节点。SKIP LOCKED 是这里的关键，
	// 它让多副本抢任务天然无冲突且不互相阻塞。
	ClaimReadyNodes(ctx context.Context, workerID string, limit int, leaseTTL time.Duration) ([]*workflow.NodeRun, error)

	// RenewLease 续约。返回 false 表示租约已被回收，调用方应放弃执行。
	RenewLease(ctx context.Context, nodeRunID, workerID string, ttl time.Duration) (bool, error)

	// ReclaimExpiredLeases 回收过期租约，把节点退回 ready。
	// 只由 leader 副本执行，避免多副本重复扫描。
	ReclaimExpiredLeases(ctx context.Context, limit int) (int, error)

	// CompleteNode 写入节点结果并追加事件、唤醒后继。
	//
	// **必须在单个事务内完成**：更新 node_run 状态、追加 event、
	// 把满足依赖的后继节点置为 ready、写 outbox。
	// 拆成多个事务会出现"节点成功了但后继没被唤醒"，流程永久卡住——
	// 这是分布式工作流引擎最常见的线上故障形态。
	CompleteNode(ctx context.Context, nodeRun *workflow.NodeRun, next []string) error

	// ScheduleRetry 安排重试（写 next_retry_at，状态退回 pending）。
	ScheduleRetry(ctx context.Context, nodeRunID string, at time.Time, attempt int, errKind, errMsg string) error

	// GetRun / GetDefinition 读取上下文。
	GetRun(ctx context.Context, runID string) (*workflow.Run, error)
	GetDefinition(ctx context.Context, name string, version int) (*workflow.Definition, error)

	// ListNodeRuns 返回一个 run 的全部节点记录，用于依赖判定与表达式求值。
	ListNodeRuns(ctx context.Context, runID string) ([]*workflow.NodeRun, error)

	// AppendEvent 追加事件日志。
	AppendEvent(ctx context.Context, event *workflow.Event) error

	// FinalizeRun 在所有终端节点完成后结算 run 状态与输出。
	FinalizeRun(ctx context.Context, runID string, status workflow.RunStatus, output map[string]any, errMsg, failedNodeID string) error
}

// ActivityRunner 是执行器对活动注册表的依赖。
// 具体实现见 internal/activity。
type ActivityRunner interface {
	// Run 执行一个活动。input 是模板求值后的实参。
	Run(ctx context.Context, activityType string, input map[string]any) (map[string]any, error)
	// Supports 判断活动类型是否已注册。
	Supports(activityType string) bool
	// IsIdempotent 报告活动是否幂等。执行器据此决定失联后能否自动重试。
	IsIdempotent(activityType string) bool
}

// ApprovalGateway 是执行器对审批模块的依赖。
type ApprovalGateway interface {
	// Request 创建审批单并送达。节点随后进入 waiting，不再占用 worker。
	Request(ctx context.Context, runID, nodeRunID string, spec *workflow.ApprovalSpec, ctxData map[string]any) (approvalID string, err error)
}

// Evaluator 是表达式求值器。
//
// 抽象成接口是为了保留替换空间：当前用 text/template，
// 若将来发现表达能力不足，可换成受限的表达式引擎而不动执行器。
type Evaluator interface {
	// Render 渲染 Config 中的模板，返回实参。
	Render(ctx context.Context, tmpl map[string]any, data *EvalData) (map[string]any, error)
	// Bool 求值 condition。
	Bool(ctx context.Context, expr string, data *EvalData) (bool, error)
}

// EvalData 是表达式可访问的数据视图。
//
// 刻意只暴露这几个字段（而不是把整个 Run 结构体塞进去）：
// 模板能访问的越多，流程定义对引擎内部结构的耦合就越深，
// 将来重构 Run 结构会破坏所有存量定义。
type EvalData struct {
	// Input 是 run 的入参，模板中以 .input.xxx 访问。
	Input map[string]any
	// Nodes 是各已完成节点的输出，以 .nodes.<id>.output.xxx 访问。
	// 同时暴露 .nodes.<id>.status 供 condition 判断上游是成功还是跳过。
	Nodes map[string]NodeView
	// Loop 是循环变量（仅循环体内有值）。
	Loop map[string]any
	// Meta 提供 run_id / tenant_id / now 等元信息。
	Meta map[string]any
}

// NodeView 是模板中可见的节点视图。
type NodeView struct {
	Status workflow.NodeStatus `json:"status"`
	Output map[string]any      `json:"output"`
}

// EventPublisher 把状态变化外发到 Kafka（经 outbox）。
type EventPublisher interface {
	Publish(ctx context.Context, topic string, key string, payload map[string]any) error
}

// ExecutorConfig 是执行器参数。
type ExecutorConfig struct {
	DefaultNodeTimeout time.Duration
	DefaultRetry       *workflow.RetryPolicy
	HeartbeatInterval  time.Duration
	LeaseTTL           time.Duration

	// MaxInlineOutputBytes 超过则溢出到对象存储。
	MaxInlineOutputBytes int

	// StrictExpression 为真时模板缺键即失败。
	// 应保持为真：静默渲染成空字符串会让错误在下游若干节点后才暴露。
	StrictExpression bool
}

// Executor 执行单个节点。无状态，可被多个 worker goroutine 共享。
type Executor struct {
	cfg       ExecutorConfig
	store     Store
	runner    ActivityRunner
	approval  ApprovalGateway
	eval      Evaluator
	publisher EventPublisher
	workerID  string
}

// NewExecutor 构造执行器。
//
// TODO: 校验必填依赖非 nil（缺 store 或 runner 时应当启动即失败，
// 而不是等到执行第一个节点才 panic）。
func NewExecutor(
	cfg ExecutorConfig,
	store Store,
	runner ActivityRunner,
	approval ApprovalGateway,
	eval Evaluator,
	publisher EventPublisher,
	workerID string,
) (*Executor, error) {
	return nil, ErrNotImplemented
}

// Execute 执行一个节点的完整生命周期。
//
// 完整流程（顺序即正确性，实现时勿调整）：
//
//  1. 载入 run 与 definition，取出 Node 定义
//  2. 组装 EvalData（读该 run 的全部 node_runs 输出）
//  3. 求值 Condition：为假 → 标记 skipped，唤醒后继，直接返回
//     （注意 skipped 也要唤醒后继，否则汇合节点死锁）
//  4. 渲染 Config 得到实参，落 Input 快照
//  5. 审批节点分支：调 ApprovalGateway.Request，节点转 waiting 后**立即返回**，
//     释放 worker。后续由审批回调驱动
//  6. 普通节点：起心跳协程（每 HeartbeatInterval 续约一次）
//  7. 带 timeout 的 context 调 ActivityRunner.Run
//  8. 成功 → 落 Output（超限则溢出到对象存储）→ CompleteNode(succeeded, next)
//  9. 失败 → 分类错误 → 查 RetryPolicy.ShouldRetry：
//     - 可重试且未耗尽 → ScheduleRetry
//     - 否则 → CompleteNode(failed)；ContinueOnFailure 为真时仍唤醒后继
//  10. defer 里停心跳；若期间 RenewLease 返回 false（ErrLeaseLost），
//     **丢弃结果不写库**
//
// TODO: 按上述流程实现。
func (e *Executor) Execute(ctx context.Context, nodeRun *workflow.NodeRun) error {
	return ErrNotImplemented
}

// ResumeApproval 在审批决策到达后恢复节点。
//
// 由 approval 模块回调触发，而非调度器轮询——审批的响应应当是即时的。
//
// TODO:
//  1. 校验 nodeRun 当前确为 waiting（幂等：已完成则直接返回，
//     IM 侧重试或用户连点会产生重复回调）
//  2. 把 decision/comment/approver 作为节点 Output
//  3. decision=approve → succeeded；reject → succeeded 但 output.decision=reject
//     （注意：**驳回不等于节点失败**。驳回是一个合法业务结果，
//     后继节点靠 condition 分流。把驳回当失败会让整个 run 变成 failed，
//     无法执行"驳回后通知提交人"这样的分支）
//  4. CompleteNode 唤醒后继
func (e *Executor) ResumeApproval(ctx context.Context, nodeRunID string, decision string, approver string, comment string) error {
	return ErrNotImplemented
}

// resolveNext 判定哪些后继节点的依赖已满足，应转为 ready。
//
// 这是引擎的核心判定逻辑，也是最容易出错的地方。要点：
//   - DependAll：所有前驱都 IsTerminal 且都 IsSuccessful
//   - DependAny：任一前驱 IsSuccessful 即可（但要防止重复唤醒同一节点）
//   - 前驱中有 failed 且该前驱未设 ContinueOnFailure → 后继转 skipped，
//     而非留在 pending。留 pending 会让 run 永远无法进入终态
//
// TODO: 按上述规则实现，并对每条规则写单测。
func (e *Executor) resolveNext(def *workflow.Definition, nodeID string, all []*workflow.NodeRun) ([]string, error) {
	return nil, ErrNotImplemented
}

// classifyError 把活动返回的错误归类为 error_kind 字符串。
//
// 分类口径与 model-gateway 对齐（timeout / connection_error / server_error /
// rate_limited / invalid_request / auth_error / content_filter / validation_error），
// 让重试策略配置在两个组件间可以用同一套词汇表达。
//
// TODO: 用 errors.Is / errors.As 识别；无法识别时归为 "unknown"
// （并打日志——出现大量 unknown 说明分类规则需要补充）。
func classifyError(err error) string {
	return "unknown"
}

// finalizeRunIfDone 检查 run 是否已达终态并结算。
//
// 判定规则：所有终端节点均 IsTerminal。
// 有任一节点 failed 且未设 ContinueOnFailure → run failed，
// 否则 succeeded。
//
// 注意 waiting 状态的节点**不算**终态，因此有审批挂起时 run 保持 running。
//
// TODO: 实现判定 + 组装 OutputMapping + FinalizeRun + 发 Kafka 事件。
func (e *Executor) finalizeRunIfDone(ctx context.Context, runID string) error {
	return ErrNotImplemented
}
