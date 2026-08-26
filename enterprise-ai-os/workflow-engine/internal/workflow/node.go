package workflow

import "time"

// NodeStatus 是单个节点在一次 run 中的执行状态。
type NodeStatus string

const (
	// NodePending 前驱未全部完成。
	NodePending NodeStatus = "pending"
	// NodeReady 依赖已满足，等待 worker 领取。
	NodeReady NodeStatus = "ready"
	// NodeRunning worker 正在执行。占用 worker 槽位，受节点超时约束。
	NodeRunning NodeStatus = "running"
	// NodeWaiting 等待外部事件（人工审批、外部回调）。
	//
	// 与 NodeRunning 的关键区别：**不占用 worker 槽位，不计入执行超时**。
	// 审批可能等三天，若按 running 处理，worker 池会在几个审批流之后
	// 被全部占满，整个引擎饥饿。这个状态的存在是引擎能支撑长周期
	// 人工流程的根本原因。
	NodeWaiting NodeStatus = "waiting"
	// NodeSucceeded 执行成功，输出已落库。
	NodeSucceeded NodeStatus = "succeeded"
	// NodeFailed 重试耗尽后仍失败。
	NodeFailed NodeStatus = "failed"
	// NodeSkipped condition 求值为假，或前驱失败导致本节点无需执行。
	//
	// skipped 与 succeeded 必须区分：后继节点的 condition 里可能要判断
	// "上游到底是通过了还是被跳过了"。合并成一个状态会丢失这个信息。
	NodeSkipped NodeStatus = "skipped"
	// NodeCanceled run 被取消时，未开始的节点转入此状态。
	NodeCanceled NodeStatus = "canceled"
)

// IsTerminal 判断节点是否已到终态。后继节点的依赖判定只看终态。
func (s NodeStatus) IsTerminal() bool {
	switch s {
	case NodeSucceeded, NodeFailed, NodeSkipped, NodeCanceled:
		return true
	default:
		return false
	}
}

// IsSuccessful 判断是否算"满足依赖"。
//
// Skipped 也算满足：一条被跳过的分支不应该永久阻塞其汇合节点。
// 这是 DAG 引擎里最容易搞错的一处语义——若 skipped 不算满足，
// 任何带条件分支的流程都会在汇合点死锁。
func (s NodeStatus) IsSuccessful() bool {
	return s == NodeSucceeded || s == NodeSkipped
}

// BackoffKind 是重试退避方式。
type BackoffKind string

const (
	BackoffFixed       BackoffKind = "fixed"
	BackoffLinear      BackoffKind = "linear"
	BackoffExponential BackoffKind = "exponential"
)

// DependencyMode 决定多个前驱时的依赖满足条件。
type DependencyMode string

const (
	// DependAll 所有前驱都成功（默认，也是绝大多数场景）。
	DependAll DependencyMode = "all"
	// DependAny 任一前驱成功即触发。
	//
	// 用途：多路召回后取最快返回的那一路。
	// 注意语义陷阱：any 模式下节点可能在其他前驱仍在执行时就开始跑，
	// 因此它的表达式里**不能**引用那些未完成前驱的输出。
	// 校验阶段应当检查这一点。
	DependAny DependencyMode = "any"
)

// RetryPolicy 是节点级重试策略。
type RetryPolicy struct {
	MaxAttempts     int         `json:"max_attempts"`
	Backoff         BackoffKind `json:"backoff,omitempty"`
	InitialInterval Duration    `json:"initial_interval,omitempty"`
	MaxInterval     Duration    `json:"max_interval,omitempty"`
	Multiplier      float64     `json:"multiplier,omitempty"`

	// Jitter 默认应为 true。
	// 关掉抖动时，下游抖动导致的批量失败会在同一时刻齐齐重试，
	// 形成周期性尖峰把刚恢复的下游再次打死（惊群）。
	Jitter bool `json:"jitter,omitempty"`

	// RetryableErrors 为空时使用配置里的全局默认。
	RetryableErrors    []string `json:"retryable_errors,omitempty"`
	NonRetryableErrors []string `json:"non_retryable_errors,omitempty"`
}

// NextInterval 计算第 attempt 次重试（attempt 从 1 开始）的等待时长。
//
// TODO:
//  1. 按 Backoff 类型计算基础间隔
//  2. 截断到 MaxInterval
//  3. Jitter 为真时在 [0.5×, 1.0×] 区间随机（full jitter 的变体，
//     既打散又不会让首次重试等太久）
func (r *RetryPolicy) NextInterval(attempt int) time.Duration {
	return 0
}

// ShouldRetry 判定某类错误是否应重试。
//
// TODO: NonRetryableErrors 优先（显式禁止胜过隐式允许），
// 再查 RetryableErrors。都未命中时返回 false——
// 未知错误默认不重试，因为重试有副作用风险，
// 而"漏重试"只是需要人工介入，代价小得多。
func (r *RetryPolicy) ShouldRetry(errKind string, attempt int) bool {
	return false
}

// ApprovalMode 是审批的通过条件。
type ApprovalMode string

const (
	ApprovalAny        ApprovalMode = "any"        // 任一审批人通过
	ApprovalAll        ApprovalMode = "all"        // 全部通过
	ApprovalQuorum     ApprovalMode = "quorum"     // 达到比例
	ApprovalSequential ApprovalMode = "sequential" // 按序逐级
)

// TimeoutAction 是审批超时后的动作。
type TimeoutAction string

const (
	// TimeoutReject 超时视为驳回。**这是默认值，且应当保持默认。**
	// 超时自动通过会让审批退化成一个延时器，风控完全失效。
	TimeoutReject TimeoutAction = "reject"
	// TimeoutApprove 超时视为通过。仅用于"无人反对即通过"的场景，
	// 必须在 DAG 里显式声明。
	TimeoutApprove TimeoutAction = "approve"
	// TimeoutEscalate 超时上报给更高层级审批人。
	TimeoutEscalate TimeoutAction = "escalate"
	// TimeoutFail 超时直接让节点失败，交给 on_failure 处理。
	TimeoutFail TimeoutAction = "fail"
)

// ApprovalSpec 是审批节点的专有配置。
type ApprovalSpec struct {
	// Approvers 支持 user:<id> / role:<name> / group:<id> / expr:<模板>。
	// expr 形式可以做到"提交人的直属上级"这类动态审批人。
	Approvers []string `json:"approvers"`

	Mode        ApprovalMode `json:"mode,omitempty"`
	QuorumRatio float64      `json:"quorum_ratio,omitempty"`

	Timeout   Duration      `json:"timeout,omitempty"`
	OnTimeout TimeoutAction `json:"on_timeout,omitempty"`
	Escalate  []string      `json:"escalate_to,omitempty"`

	// Channel 指定送达渠道（feishu / slack / discord / email）。
	Channel string `json:"channel,omitempty"`

	// Title / Summary 支持模板，用于渲染 IM 卡片。
	Title   string `json:"title,omitempty"`
	Summary string `json:"summary,omitempty"`

	// Fields 是要在卡片上展示给审批人的字段（模板表达式）。
	// 让审批人不必点进系统就能看到关键信息——
	// 这直接决定审批的实际响应速度。
	Fields map[string]string `json:"fields,omitempty"`

	// AllowComment 是否要求填写审批意见。
	// 驳回时通常应强制填写，否则提交人不知道该改什么。
	RequireCommentOnReject bool `json:"require_comment_on_reject,omitempty"`
}

// LoopSpec 是循环节点配置（需 config 开启 allow_loops）。
type LoopSpec struct {
	// Over 是要遍历的集合表达式，如 "{{ .nodes.split.output.items }}"。
	Over string `json:"over"`
	// ItemVar 是循环变量名，在子节点表达式里以 .loop.<ItemVar> 访问。
	ItemVar string `json:"item_var,omitempty"`
	// MaxIterations 必填且有硬上限。无界循环会让事件日志无限增长。
	MaxIterations int `json:"max_iterations"`
	// Parallelism 并发度。1 表示串行。
	Parallelism int `json:"parallelism,omitempty"`
	// ContinueOnError 单次迭代失败是否继续后续迭代。
	ContinueOnError bool `json:"continue_on_error,omitempty"`
}

// Node 是 DAG 中的一个节点，也就是一个原子执行单元。
type Node struct {
	ID          string `json:"id"`
	Name        string `json:"name,omitempty"`
	Description string `json:"description,omitempty"`

	// Type 是活动类型，必须在 activity.Registry 中注册。
	// 如 agent.invoke / llm.complete / rag.search / approval.request。
	Type string `json:"type"`

	DependsOn      []string       `json:"depends_on,omitempty"`
	DependencyMode DependencyMode `json:"dependency_mode,omitempty"`

	// Condition 是布尔表达式，求值为假时节点转 skipped。
	// 空表示无条件执行。
	Condition string `json:"condition,omitempty"`

	// Config 是活动的参数，值中可含 {{ }} 模板表达式。
	// 用 map[string]any 而非各活动的强类型结构：定义来自数据库/YAML，
	// 活动类型可动态扩展，强类型会让每加一个活动都要改这个结构体。
	// 类型安全在活动自己的 DecodeConfig 里保证。
	Config map[string]any `json:"config,omitempty"`

	Timeout Duration     `json:"timeout,omitempty"`
	Retry   *RetryPolicy `json:"retry,omitempty"`

	// Approval 仅当 Type == "approval.request" 时有效。
	Approval *ApprovalSpec `json:"approval,omitempty"`
	// Loop 仅当 Type == "control.loop" 时有效。
	Loop *LoopSpec `json:"loop,omitempty"`

	// ContinueOnFailure 为真时，节点失败不阻断整个 run，
	// 后继节点仍会执行（可通过 condition 判断上游状态）。
	//
	// 典型用途："可选的富化步骤"——查外部数据源失败了也不该
	// 让整个合同审核流程作废。
	ContinueOnFailure bool `json:"continue_on_failure,omitempty"`

	// Idempotent 声明活动是否幂等。
	//
	// **这是安全重试的前提。** 非幂等活动（如 notify.send 发消息）
	// 在 worker 心跳失联后不能盲目重试，否则用户会收到重复消息。
	// 引擎对非幂等节点的处理策略是：记录"可能已执行"状态，
	// 转人工确认而非自动重试。
	Idempotent bool `json:"idempotent,omitempty"`

	// OutputSchema 可选，用于校验活动输出结构。
	// 配了它能让"上游输出结构变了"这类问题在当前节点就暴露，
	// 而不是在下游取值取到空之后才发现。
	OutputSchema map[string]any `json:"output_schema,omitempty"`

	Labels map[string]string `json:"labels,omitempty"`
}

// EffectiveTimeout 返回应用默认值后的超时。
//
// TODO: Node.Timeout → Definition.Defaults.Timeout → config.default_node_timeout。
func (n *Node) EffectiveTimeout(fallback time.Duration) time.Duration {
	return n.Timeout.Or(fallback)
}

// EffectiveRetry 返回应用默认值后的重试策略。
//
// TODO: Node.Retry → Definition.Defaults.Retry → config.default_retry。
// 审批节点应强制 MaxAttempts=1——重试一个审批毫无意义，
// 只会给审批人发第二张卡片。
func (n *Node) EffectiveRetry(fallback *RetryPolicy) *RetryPolicy {
	return nil
}

// IsApproval 判断是否为审批节点。审批节点走 waiting 状态而非 running。
func (n *Node) IsApproval() bool { return n.Type == "approval.request" }

// NodeRun 是节点在某次 run 中的执行记录。
//
// 它与 Node 的关系：Node 是定义（静态、可复用），NodeRun 是执行（动态、一次性）。
// 分开建模而不是在 Node 上挂状态字段，是因为同一 Node 在 loop 场景下
// 会产生多条 NodeRun。
type NodeRun struct {
	ID     string `json:"id"`
	RunID  string `json:"run_id"`
	NodeID string `json:"node_id"`

	// LoopIndex 在循环展开时区分同一 NodeID 的多次执行。非循环为 -1。
	LoopIndex int `json:"loop_index"`

	Status  NodeStatus `json:"status"`
	Attempt int        `json:"attempt"`

	// Input 是模板求值后的实际入参快照。
	//
	// 为什么必须存快照而不是运行时重新求值：出问题时要回答
	// "第 3 步到底收到了什么"。若只存模板，上游数据变了就永远复现不出来。
	// 这也是审计的核心证据。
	Input map[string]any `json:"input,omitempty"`
	// Output 是活动返回值。超过 max_inline_bytes 时存 MinIO 引用。
	Output map[string]any `json:"output,omitempty"`
	// OutputRef 是溢出到对象存储后的引用（s3://bucket/key）。
	OutputRef string `json:"output_ref,omitempty"`

	Error     string `json:"error,omitempty"`
	ErrorKind string `json:"error_kind,omitempty"` // timeout / server_error / validation_error ...

	// WorkerID 标识执行该节点的 worker 实例。
	// 用于排查"哪个副本上出的问题"，以及心跳超时后判定归属。
	WorkerID string `json:"worker_id,omitempty"`
	// LeaseExpiresAt 是当前 worker 的租约到期时间。
	// 调度器扫描过期租约来回收僵死任务。
	LeaseExpiresAt *time.Time `json:"lease_expires_at,omitempty"`

	// ApprovalID 关联审批单（仅审批节点）。
	ApprovalID string `json:"approval_id,omitempty"`

	// ModelUsed / TokensUsed / CostCents 用于成本归因。
	// 让"这个流程跑一次花多少钱"可回答——这是企业采纳 AI 流程的关键决策依据。
	ModelUsed  string  `json:"model_used,omitempty"`
	TokensUsed int     `json:"tokens_used,omitempty"`
	CostCents  float64 `json:"cost_cents,omitempty"`

	ScheduledAt *time.Time `json:"scheduled_at,omitempty"`
	StartedAt   *time.Time `json:"started_at,omitempty"`
	FinishedAt  *time.Time `json:"finished_at,omitempty"`
	NextRetryAt *time.Time `json:"next_retry_at,omitempty"`

	SpanID string `json:"span_id,omitempty"`
}

// DurationMs 返回执行耗时（毫秒）。未完成返回 0。
func (nr *NodeRun) DurationMs() int64 {
	if nr.StartedAt == nil || nr.FinishedAt == nil {
		return 0
	}
	return nr.FinishedAt.Sub(*nr.StartedAt).Milliseconds()
}

// EventType 是事件日志的类型。
//
// 事件日志是**唯一的真相来源**（single source of truth）：
// runs / node_runs 表里的状态本质上是事件流的物化视图，
// 任何时候都能从事件重建。这让"状态被并发写乱"这类问题可诊断、可修复。
type EventType string

const (
	EventRunCreated        EventType = "run.created"
	EventRunStarted        EventType = "run.started"
	EventRunPaused         EventType = "run.paused"
	EventRunResumed        EventType = "run.resumed"
	EventRunSucceeded      EventType = "run.succeeded"
	EventRunFailed         EventType = "run.failed"
	EventRunCanceled       EventType = "run.canceled"
	EventNodeScheduled     EventType = "node.scheduled"
	EventNodeStarted       EventType = "node.started"
	EventNodeSucceeded     EventType = "node.succeeded"
	EventNodeFailed        EventType = "node.failed"
	EventNodeSkipped       EventType = "node.skipped"
	EventNodeRetrying      EventType = "node.retrying"
	EventNodeLeaseExpired  EventType = "node.lease_expired"
	EventApprovalCreated   EventType = "approval.created"
	EventApprovalDelivered EventType = "approval.delivered"
	EventApprovalDecided   EventType = "approval.decided"
	EventApprovalTimedOut  EventType = "approval.timed_out"
)

// Event 是一条追加式事件记录。
//
// 表设计要点：(run_id, sequence) 唯一，sequence 由数据库序列生成。
// 严格递增的序号让重放顺序确定，也让"是否漏了事件"可校验。
type Event struct {
	ID       int64     `json:"id"`
	RunID    string    `json:"run_id"`
	Sequence int64     `json:"sequence"`
	Type     EventType `json:"type"`

	NodeID    string `json:"node_id,omitempty"`
	LoopIndex int    `json:"loop_index,omitempty"`

	// Payload 存该事件类型特有的数据。
	Payload map[string]any `json:"payload,omitempty"`

	// Actor 记录动作发起方：system / user:<id> / worker:<id>。
	// 审计要求能回答"谁做的"，system 与人工动作必须可区分。
	Actor string `json:"actor,omitempty"`

	TraceID   string    `json:"trace_id,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}
