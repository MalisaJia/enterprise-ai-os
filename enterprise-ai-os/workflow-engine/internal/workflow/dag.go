// Package workflow 定义工作流的静态结构：DAG 定义、节点、校验规则。
//
// 本包是**纯数据 + 纯函数**，不含任何 IO。这是刻意的设计约束：
// DAG 的解析与校验是整个引擎最需要单元测试覆盖的部分（环检测写错的后果是
// 调度器无限循环），把它隔离在无依赖的包里，测试可以毫无成本地穷举各种畸形定义。
package workflow

import (
	"errors"
	"time"
)

// 定义与校验相关的错误。
//
// 全部导出为哨兵错误而非临时构造，是为了让 API 层能用 errors.Is 精确映射
// HTTP 状态码：ErrCycleDetected 应该是 400（用户输入问题），
// 而不是笼统的 500。
var (
	ErrNotImplemented    = errors.New("workflow: not implemented")
	ErrEmptyDAG          = errors.New("workflow: dag has no nodes")
	ErrDuplicateNodeID   = errors.New("workflow: duplicate node id")
	ErrUnknownDependency = errors.New("workflow: node depends on unknown node")
	ErrCycleDetected     = errors.New("workflow: dependency cycle detected")
	ErrNoEntryNode       = errors.New("workflow: no entry node (every node has dependencies)")
	ErrUnreachableNode   = errors.New("workflow: node is unreachable from any entry")
	ErrTooManyNodes      = errors.New("workflow: node count exceeds limit")
	ErrDepthExceeded     = errors.New("workflow: dag depth exceeds limit")
	ErrUnknownActivity   = errors.New("workflow: unknown activity type")
	ErrInvalidExpression = errors.New("workflow: invalid expression")
	ErrLoopNotAllowed    = errors.New("workflow: loop nodes are disabled")
	ErrSelfDependency    = errors.New("workflow: node depends on itself")
)

// RunStatus 是工作流实例的状态。
type RunStatus string

const (
	RunPending   RunStatus = "pending"   // 已提交，尚未开始调度
	RunRunning   RunStatus = "running"   // 至少一个节点在执行或就绪
	RunPaused    RunStatus = "paused"    // 人工暂停：in-flight 节点跑完，不再调度新节点
	RunSucceeded RunStatus = "succeeded" // 所有终端节点成功
	RunFailed    RunStatus = "failed"    // 有节点最终失败且无法继续
	RunCanceled  RunStatus = "canceled"  // 人工取消或超过 max_run_duration
)

// IsTerminal 判断状态是否为终态。终态的 run 不再被调度器拾取。
func (s RunStatus) IsTerminal() bool {
	switch s {
	case RunSucceeded, RunFailed, RunCanceled:
		return true
	default:
		return false
	}
}

// Definition 是一个版本化的 DAG 定义。
//
// 为什么 Version 是定义的一部分而非外挂元数据：Run 会引用
// (Name, Version) 二元组并在整个生命周期内锁定它。把版本嵌进结构体，
// 任何持有 Definition 的代码都不可能"忘记"自己在处理哪一版。
type Definition struct {
	Name        string `json:"name"`
	Version     int    `json:"version"`
	DisplayName string `json:"display_name,omitempty"`
	Description string `json:"description,omitempty"`

	// TenantID 决定定义的可见范围。空值表示平台级公共定义。
	TenantID string `json:"tenant_id,omitempty"`

	// InputSchema 是 JSON Schema，用于在启动实例前校验入参。
	//
	// 为什么值得做：工作流可能在第 7 个节点才用到某个入参。
	// 不做前置校验的话，前 6 个节点白跑（其中可能已经花了钱调了 LLM、
	// 已经发了消息），到第 7 个节点才因为缺字段失败。
	InputSchema map[string]any `json:"input_schema,omitempty"`

	// OutputMapping 定义 run 的最终输出如何从各节点输出组装。
	OutputMapping map[string]string `json:"output_mapping,omitempty"`

	Nodes []Node `json:"nodes"`

	// Defaults 是节点级配置的默认值，节点可逐项覆盖。
	Defaults NodeDefaults `json:"defaults,omitempty"`

	// OnFailure 定义整个流程失败时的补偿动作。
	// 注意这**不是**事务回滚——已发出的消息无法撤回。
	// 它的实际语义是"失败通知 + 尽力而为的清理"。
	OnFailure []Node `json:"on_failure,omitempty"`

	Labels    map[string]string `json:"labels,omitempty"`
	CreatedBy string            `json:"created_by,omitempty"`
	CreatedAt time.Time         `json:"created_at,omitempty"`

	// 以下为解析后的派生数据，不参与序列化。
	// 缓存它们是因为调度器在每次判定后继时都要用到，
	// 每次重新计算邻接表在高频调度下是可观的开销。
	nodeIndex map[string]*Node
	children  map[string][]string
	entries   []string
	topoOrder []string
}

// NodeDefaults 是流程级的节点默认配置。
type NodeDefaults struct {
	Timeout Duration     `json:"timeout,omitempty"`
	Retry   *RetryPolicy `json:"retry,omitempty"`
}

// Parse 从 YAML/JSON 字节流解析定义并完成全部校验。
//
// 校验与解析合一（而非提供单独的 Validate）是刻意的：
// 一个"解析成功但没校验"的 Definition 是危险的中间态，
// 它会被误传给调度器。让非法定义永远无法构造出 *Definition 是更安全的 API。
//
// TODO:
//  1. 支持 YAML 与 JSON 两种输入（探测首字符判断）
//  2. 反序列化到 Definition
//  3. 应用 Defaults 到各节点未显式设置的字段
//  4. 调 Validate 完成结构校验
//  5. 构建 nodeIndex / children / entries / topoOrder 派生数据
func Parse(data []byte, limits Limits) (*Definition, error) {
	return nil, ErrNotImplemented
}

// Limits 是 DAG 规模护栏，来自 config.yaml 的 workflow 段。
type Limits struct {
	MaxNodes            int
	MaxDepth            int
	MaxParallelBranches int
	AllowLoops          bool
	MaxLoopIterations   int

	// KnownActivities 用于校验 node.type 是否是已注册的活动类型。
	// 由 activity.Registry 提供。把未知类型拦在注册阶段，
	// 而不是等到运行到那个节点才失败——后者可能是几天以后的事。
	KnownActivities map[string]bool
}

// Validate 执行全部静态校验。
//
// 校验顺序有讲究：先做便宜且能定位到具体节点的检查（重复 ID、未知依赖），
// 再做全局检查（环、可达性）。这样报错信息对用户最有用——
// "节点 foo 依赖了不存在的 bar" 远比 "图中存在环" 容易修。
//
// TODO:
//  1. 非空 + 节点数不超 MaxNodes
//  2. 节点 ID 唯一、格式合法（[a-z0-9_-]{1,64}）
//  3. depends_on 全部指向存在的节点，且不自依赖
//  4. node.type 在 KnownActivities 中
//  5. DetectCycle
//  6. 存在至少一个入口节点（depends_on 为空）
//  7. 所有节点从入口可达（不可达节点必是笔误，直接报错而非静默跳过）
//  8. 深度不超 MaxDepth
//  9. condition / config 中的表达式可编译
//  10. approval 节点的 approvers 非空、mode 合法
func (d *Definition) Validate(limits Limits) error {
	return ErrNotImplemented
}

// DetectCycle 检测依赖环，返回构成环的节点 ID 序列。
//
// 用带三色标记的 DFS（白=未访问，灰=在栈上，黑=已完成）而非
// Kahn 拓扑排序：Kahn 只能告诉你"有环"，三色 DFS 能把环上的节点原样吐出来。
// 对使用者来说，"a → b → c → a" 和 "图中有环" 的可修复性差距巨大。
//
// TODO: 实现三色 DFS，遇到灰色节点即回溯出环路径。
func (d *Definition) DetectCycle() ([]string, error) {
	return nil, ErrNotImplemented
}

// TopoOrder 返回拓扑序。仅用于可视化与深度计算，
// **不用于调度**——调度是事件驱动的（前驱完成时唤醒后继），
// 按拓扑序串行执行会丢掉并行能力。
//
// TODO: 缓存 d.topoOrder，未构建时用 Kahn 算法生成。
func (d *Definition) TopoOrder() ([]string, error) {
	return nil, ErrNotImplemented
}

// Node 按 ID 查节点。
//
// TODO: 从 nodeIndex 查；未构建索引时线性扫描并顺手建索引。
func (d *Definition) Node(id string) (*Node, bool) {
	return nil, false
}

// EntryNodes 返回无依赖的入口节点 ID。
//
// TODO: 返回缓存的 entries。
func (d *Definition) EntryNodes() []string {
	return nil
}

// Children 返回直接依赖 nodeID 的后继节点。
//
// 这是调度器的热路径：每个节点完成时都要调用一次来找后继。
// 因此必须走预建的反向邻接表，不能每次遍历全图。
//
// TODO: 返回 d.children[nodeID]。
func (d *Definition) Children(nodeID string) []string {
	return nil
}

// TerminalNodes 返回没有后继的节点。
// run 的成功判定 = 所有终端节点进入终态且无失败。
//
// TODO: children 为空的节点即终端节点。
func (d *Definition) TerminalNodes() []string {
	return nil
}

// Depth 返回 DAG 的最长路径长度。用于校验 MaxDepth 与前端布局。
//
// TODO: 按拓扑序做一次 DP。
func (d *Definition) Depth() int {
	return 0
}

// Fingerprint 返回定义内容的稳定哈希。
//
// 用途：注册定义时若指纹与最新版本相同，则**不产生新版本号**。
// 没有这个机制的话，CI 每次部署都会 POST 一遍定义，
// 版本号一天涨几十个，keep_versions 的窗口迅速被无意义的重复版本占满。
//
// TODO: 对规范化（键排序、去掉 CreatedAt 等易变字段）后的 JSON 求 SHA-256。
func (d *Definition) Fingerprint() (string, error) {
	return "", ErrNotImplemented
}

// Run 是工作流的一次执行实例。
type Run struct {
	ID       string `json:"id"`
	TenantID string `json:"tenant_id"`
	SpaceID  string `json:"space_id,omitempty"`

	// DefinitionName + DefinitionVersion 锁定实例使用的定义版本。
	// 中途改定义不影响已启动的实例——这是保证状态可收敛的前提。
	DefinitionName    string `json:"definition_name"`
	DefinitionVersion int    `json:"definition_version"`

	Status RunStatus      `json:"status"`
	Input  map[string]any `json:"input,omitempty"`
	Output map[string]any `json:"output,omitempty"`

	// Error 是失败原因的用户可读描述。
	Error string `json:"error,omitempty"`
	// FailedNodeID 指出是哪个节点导致 run 失败，便于直接跳转排查。
	FailedNodeID string `json:"failed_node_id,omitempty"`

	// IdempotencyKey 让重复提交同一逻辑任务不会启动两个实例。
	// 这在事件触发场景是必需的：Kafka 至少一次投递，
	// 同一事件可能被消费两次。
	IdempotencyKey string `json:"idempotency_key,omitempty"`

	// TraceID 贯穿整个 run 的所有节点。持久化在这里而非只存在于
	// 内存 context 中，是因为 run 可能跨越数天与多次进程重启。
	TraceID string `json:"trace_id,omitempty"`

	// Priority 影响 ready 队列的取任务顺序。数值大者优先。
	Priority int `json:"priority"`

	TriggeredBy string     `json:"triggered_by,omitempty"` // user:xxx | event:xxx | schedule:xxx
	CreatedAt   time.Time  `json:"created_at"`
	StartedAt   *time.Time `json:"started_at,omitempty"`
	FinishedAt  *time.Time `json:"finished_at,omitempty"`
	DeadlineAt  *time.Time `json:"deadline_at,omitempty"`
}

// Duration 是 time.Duration 的包装，支持 "300s" / "48h" 形式的
// YAML/JSON 反序列化。
//
// 为什么不直接用 time.Duration：它的 JSON 序列化是纳秒整数，
// 配置文件里写 300000000000 完全不可读，且极易写错数量级。
type Duration time.Duration

// UnmarshalJSON 解析字符串形式的时长。
//
// TODO: 支持 "300s" / "5m" / "48h" 以及裸数字（视为秒）。
// 裸数字要支持，因为 YAML 里 `timeout: 300` 是很自然的写法，
// 直接报错对使用者不友好。
func (d *Duration) UnmarshalJSON(b []byte) error {
	return ErrNotImplemented
}

// MarshalJSON 输出字符串形式，保证配置回显的可读性。
//
// TODO: time.Duration.String()。
func (d Duration) MarshalJSON() ([]byte, error) {
	return nil, ErrNotImplemented
}

// Std 转回标准库类型。
func (d Duration) Std() time.Duration { return time.Duration(d) }

// Or 返回自身，零值时返回 fallback。用于逐层应用默认值。
func (d Duration) Or(fallback time.Duration) time.Duration {
	if d == 0 {
		return fallback
	}
	return time.Duration(d)
}
