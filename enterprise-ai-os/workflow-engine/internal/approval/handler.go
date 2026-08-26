// Package approval 实现人工审批：审批单生命周期、送达、决策收敛、超时。
//
// 为什么审批需要独立成一个包而不是做成一个普通活动：
// 普通活动是"调用—返回"的同步模型，而审批是**跨进程、跨天、由外部事件驱动**的。
// 它需要自己的持久化状态、送达重试、超时扫描、多人决策聚合逻辑。
// 塞进 activity 包会让那里的"薄活动"原则破产。
package approval

import (
	"context"
	"errors"
	"time"

	"github.com/mininglamp/enterprise-ai-os/workflow-engine/internal/workflow"
)

var (
	ErrNotImplemented = errors.New("approval: not implemented")
	// ErrNoApprovers 审批人解析结果为空。
	//
	// 这里必须报错而非自动通过。配错了角色名却让流程静默通过，
	// 是风控意义上最危险的失败模式——它会在很久之后才被发现，
	// 期间所有走这条流程的单子都等于没审批。
	ErrNoApprovers = errors.New("approval: no approvers resolved")
	// ErrNotAuthorized 决策人不在审批人列表中。
	ErrNotAuthorized = errors.New("approval: actor is not an approver")
	// ErrAlreadyDecided 审批单已有终局结论。
	// 返回它而非报错是幂等设计的一部分（见 Decide 的说明）。
	ErrAlreadyDecided = errors.New("approval: already decided")
	// ErrInvalidSignature 回调验签失败。
	ErrInvalidSignature = errors.New("approval: invalid callback signature")
	// ErrCommentRequired 驳回时未填意见。
	ErrCommentRequired = errors.New("approval: comment required on reject")
)

// Decision 是单个审批人的决策。
type Decision string

const (
	DecisionApprove Decision = "approve"
	DecisionReject  Decision = "reject"
	// DecisionAbstain 弃权。在 quorum 模式下不计入分子也不计入分母。
	DecisionAbstain Decision = "abstain"
	// DecisionDelegate 转他人审批。
	DecisionDelegate Decision = "delegate"
)

// Status 是审批单状态。
type Status string

const (
	StatusPending   Status = "pending"   // 已创建，等待决策
	StatusApproved  Status = "approved"  // 达到通过条件
	StatusRejected  Status = "rejected"  // 达到驳回条件
	StatusTimedOut  Status = "timed_out" // 超时，按 on_timeout 处理
	StatusCanceled  Status = "canceled"  // run 被取消
	StatusEscalated Status = "escalated" // 已上报更高层级
)

// IsTerminal 判断是否已有终局结论。
func (s Status) IsTerminal() bool {
	switch s {
	case StatusApproved, StatusRejected, StatusTimedOut, StatusCanceled:
		return true
	default:
		return false
	}
}

// Approver 是一个待决策/已决策的审批人。
type Approver struct {
	UserID string `json:"user_id"`
	Name   string `json:"name,omitempty"`
	// Source 记录该审批人是怎么解析出来的（role:legal / user:u123），
	// 便于事后回答"为什么是他来审"。
	Source string `json:"source,omitempty"`
	// Order 在 sequential 模式下决定审批顺序。
	Order int `json:"order"`

	Decision   Decision   `json:"decision,omitempty"`
	Comment    string     `json:"comment,omitempty"`
	DecidedAt  *time.Time `json:"decided_at,omitempty"`
	DelegateTo string     `json:"delegate_to,omitempty"`

	// NotifiedAt / NotifyAttempts 用于送达重试与"他到底收到了没"的排查。
	NotifiedAt     *time.Time `json:"notified_at,omitempty"`
	NotifyAttempts int        `json:"notify_attempts"`
	MessageID      string     `json:"message_id,omitempty"` // IM 侧消息 id，用于更新/撤回卡片
}

// Request 是一张审批单。
type Request struct {
	ID       string `json:"id"`
	TenantID string `json:"tenant_id"`

	RunID     string `json:"run_id"`
	NodeRunID string `json:"node_run_id"`
	NodeID    string `json:"node_id"`

	// DefinitionName 冗余存一份，让审批列表页不必 join 就能展示流程名。
	DefinitionName string `json:"definition_name,omitempty"`

	Status Status `json:"status"`

	Mode        workflow.ApprovalMode `json:"mode"`
	QuorumRatio float64               `json:"quorum_ratio,omitempty"`
	Approvers   []Approver            `json:"approvers"`

	Title   string `json:"title,omitempty"`
	Summary string `json:"summary,omitempty"`
	// Fields 是渲染后的展示字段。
	//
	// 为什么要在创建时渲染并存下来，而不是展示时实时求值：
	// 审批人看到的内容必须与他做决策那一刻的上下文一致。
	// 若实时求值，上游数据变了会导致"他批的是 A，记录显示批的是 B"，
	// 在合规场景这是不可接受的。
	Fields map[string]string `json:"fields,omitempty"`

	Channel string `json:"channel,omitempty"`

	OnTimeout  workflow.TimeoutAction `json:"on_timeout"`
	EscalateTo []string               `json:"escalate_to,omitempty"`

	RequireCommentOnReject bool `json:"require_comment_on_reject,omitempty"`

	CreatedAt  time.Time  `json:"created_at"`
	DeadlineAt *time.Time `json:"deadline_at,omitempty"`
	DecidedAt  *time.Time `json:"decided_at,omitempty"`

	// RemindersSent 记录已发出的提醒档位，防止重复提醒。
	RemindersSent []string `json:"reminders_sent,omitempty"`

	// FinalDecision 是聚合后的最终结论。
	FinalDecision Decision `json:"final_decision,omitempty"`
	FinalComment  string   `json:"final_comment,omitempty"`
}

// PendingApprovers 返回尚未决策的审批人。
//
// TODO: 过滤 Decision 为空的项。sequential 模式下只返回当前轮次的那一位
// （返回全部会让后面的人提前收到卡片，破坏逐级审批的语义）。
func (r *Request) PendingApprovers() []Approver {
	return nil
}

// Store 是审批模块的持久化依赖。
type Store interface {
	Create(ctx context.Context, req *Request) error
	Get(ctx context.Context, id string) (*Request, error)
	GetByNodeRun(ctx context.Context, nodeRunID string) (*Request, error)

	// UpdateDecision 记录单个审批人的决策并可能更新单据状态。
	//
	// 实现必须用乐观锁或 SELECT FOR UPDATE：多个审批人可能同时点按钮，
	// 并发写会导致其中一票丢失。在 quorum 模式下丢一票可能直接改变结果。
	UpdateDecision(ctx context.Context, id, userID string, d Decision, comment string) (*Request, error)

	// UpdateStatus 更新终局状态。
	UpdateStatus(ctx context.Context, id string, status Status, final Decision, comment string) error

	// ListPending 查待办列表（按审批人过滤）。
	ListPending(ctx context.Context, tenantID, userID string, limit, offset int) ([]*Request, error)

	// ListDueForTimeout 查已过期待处理的单据，供超时扫描使用。
	ListDueForTimeout(ctx context.Context, now time.Time, limit int) ([]*Request, error)

	// ListDueForReminder 查需要提醒的单据。
	ListDueForReminder(ctx context.Context, now time.Time, limit int) ([]*Request, error)

	// MarkNotified 记录送达结果。
	MarkNotified(ctx context.Context, id, userID, messageID string, err error) error

	// MarkReminderSent 记录已发提醒档位。
	MarkReminderSent(ctx context.Context, id, tier string) error
}

// Resolver 把 approvers 表达式解析为具体用户。
type Resolver interface {
	// Resolve 解析 user:/role:/group:/expr: 形式的审批人声明。
	//
	// 结果需缓存（角色成员查询会打 octo-auth），但缓存 TTL 不宜过长：
	// 人员调岗后应尽快生效，否则会把审批发给已经离开该职能的人。
	Resolve(ctx context.Context, tenantID string, specs []string, evalData map[string]any) ([]Approver, error)
}

// Notifier 负责把审批卡片送达 IM。
type Notifier interface {
	// Send 发送审批卡片，返回 IM 侧消息 id。
	Send(ctx context.Context, req *Request, approver Approver) (messageID string, err error)
	// Update 更新已发出的卡片（如把按钮换成"已由张三批准"）。
	//
	// 这一步容易被省略，但很重要：不更新的话，其他审批人手上
	// 仍是可点击的卡片，点了之后才被告知"已处理"，体验很差；
	// 在 any 模式下还会让人误以为自己的决策生效了。
	Update(ctx context.Context, req *Request, approver Approver) error
	// Revoke 撤回卡片（run 被取消时）。
	Revoke(ctx context.Context, req *Request, approver Approver) error
	// Remind 发送催办提醒。
	Remind(ctx context.Context, req *Request, approver Approver, tier string) error
}

// Resumer 是审批完成后回调引擎的接口。
// 实现方是 engine.Executor.ResumeApproval。
type Resumer interface {
	ResumeApproval(ctx context.Context, nodeRunID string, decision string, approver string, comment string) error
}

// Config 是审批模块参数。
type Config struct {
	DefaultTimeout     time.Duration
	DefaultOnTimeout   workflow.TimeoutAction
	DefaultMode        workflow.ApprovalMode
	DefaultQuorumRatio float64
	DefaultChannel     string

	RemindersEnabled bool
	ReminderTiers    []time.Duration

	CallbackBaseURL string
	// CallbackSecret 用于验签。
	//
	// 审批回调是唯一能从外部改变业务结果的入口。不验签意味着
	// 任何能访问该端点的人都可以"批准"任意合同。
	// 因此空值时应当拒绝启动而不是降级为不验签。
	CallbackSecret string

	MaxDeliveryAttempts int
	IdempotencyTTL      time.Duration

	// OnEmptyApprovers: fail | auto_approve。默认且应保持 fail。
	OnEmptyApprovers string
}

// Handler 是审批模块的门面，实现 engine.ApprovalGateway。
type Handler struct {
	cfg      Config
	store    Store
	resolver Resolver
	notifier Notifier
	resumer  Resumer
}

// NewHandler 构造 Handler。
//
// TODO: 校验 CallbackSecret 非空（生产环境必须），
// 校验 DefaultOnTimeout 是合法枚举值。
func NewHandler(cfg Config, store Store, resolver Resolver, notifier Notifier, resumer Resumer) (*Handler, error) {
	return nil, ErrNotImplemented
}

// Request 创建审批单并送达。实现 engine.ApprovalGateway。
//
// 流程：
//  1. 解析审批人；为空则按 OnEmptyApprovers 处理（默认 fail）
//  2. 渲染 Title / Summary / Fields 并**固化存库**（理由见 Request.Fields 注释）
//  3. 计算 DeadlineAt = now + timeout
//  4. 插入审批单（与节点状态转 waiting **在同一事务**——
//     分开会出现"节点在等审批但审批单没建成"，流程永久挂死）
//  5. 事务提交后异步送达。
//     送达放在事务外是必须的：调 IM 是外部 IO，可能耗时数秒，
//     持有事务会长时间占用连接并放大锁竞争
//  6. 送达失败不影响审批单存在——它会被提醒任务重新推送。
//     这也是为什么送达状态要单独记录（MarkNotified）
//
// TODO: 按上述流程实现。
func (h *Handler) Request(
	ctx context.Context,
	runID, nodeRunID string,
	spec *workflow.ApprovalSpec,
	ctxData map[string]any,
) (string, error) {
	return "", ErrNotImplemented
}

// Decide 处理一次审批决策。
//
// **幂等性是这里的第一要求。** 重复回调的来源至少有三种：
// IM 平台的投递重试、用户手抖连点两次、网络超时后客户端重发。
// 处理方式：已有终局结论时返回当前状态（而非报错），
// 让调用方看到的结果一致。
//
// TODO:
//  1. 幂等检查：Redis 上以 (approvalID, userID, decision) 为键占位，
//     TTL = IdempotencyTTL。命中则直接返回既有结果
//  2. 校验 actor 在 Approvers 中（不在则 ErrNotAuthorized——
//     这是权限边界，必须严格）
//  3. sequential 模式下校验轮到该人了（越级审批要拒绝）
//  4. RequireCommentOnReject 且 decision=reject 且 comment 空 → 报错
//  5. Store.UpdateDecision（内部加锁防并发丢票）
//  6. 调 aggregate 判定是否已有终局结论
//  7. 有结论 → UpdateStatus + Notifier.Update 刷新所有人的卡片
//     + Resumer.ResumeApproval 恢复工作流
//  8. 无结论 → 只更新该审批人的卡片，继续等待
func (h *Handler) Decide(ctx context.Context, approvalID, actorUserID string, d Decision, comment string) (*Request, error) {
	return nil, ErrNotImplemented
}

// DecideByCallback 处理来自 IM 的回调，先验签再走 Decide。
//
// TODO:
//  1. 验签（HMAC-SHA256 over timestamp + body，用 CallbackSecret）
//  2. 校验 timestamp 在 ±5 分钟内——只验签不校验时间戳等于允许重放攻击，
//     攻击者截获一次合法的"批准"请求就能无限重放
//  3. 从 payload 解析 approvalID / userID / decision
//  4. 转调 Decide
func (h *Handler) DecideByCallback(ctx context.Context, signature, timestamp string, body []byte) (*Request, error) {
	return nil, ErrNotImplemented
}

// aggregate 按 Mode 聚合各审批人决策，判定是否已有终局结论。
//
// 各模式规则：
//   - any：任一 approve → approved；**全部** reject → rejected
//   - all：任一 reject → rejected（快速失败，不必等其他人）；
//     全部 approve → approved
//   - quorum：approve 数 / 有效票数 ≥ QuorumRatio → approved；
//     reject 数 / 有效票数 > 1-QuorumRatio → rejected。
//     abstain 不计入分子分母
//   - sequential：按 Order 逐个；任一 reject 立即 rejected；
//     全部通过 → approved
//
// 返回 (status, final, comment)。status 为 StatusPending 表示尚无结论。
//
// TODO: 实现上述规则，每条都要有对应单测。
// 特别注意 any 模式的 reject 条件是"全部 reject"而非"任一 reject"——
// 写成任一会让 any 模式实际变成 all 模式的驳回语义，
// 这是很容易犯且不易发现的错误。
func (h *Handler) aggregate(req *Request) (Status, Decision, string) {
	return StatusPending, "", ""
}

// HandleTimeouts 扫描并处理超时审批。由 scheduler 的 leader 副本周期调用。
//
// TODO:
//  1. ListDueForTimeout
//  2. 逐个按 OnTimeout 处理：
//     - reject   → UpdateStatus(timed_out, reject) + ResumeApproval
//     - approve  → UpdateStatus(timed_out, approve) + ResumeApproval
//     - escalate → 追加 EscalateTo 的审批人、延长 deadline、重新送达。
//     注意要限制上报层数，否则配置成环会无限上报
//     - fail     → 让节点失败，交给 DAG 的 on_failure 处理
//  3. 处理前用 CAS 校验状态仍是 pending（脑裂窗口内可能有另一个
//     副本已经处理过，或审批人刚好在此刻点了按钮）
//  4. 撤回或更新 IM 卡片，告知审批人"已超时处理"——
//     不做这步会留下一堆可点击的僵尸卡片
func (h *Handler) HandleTimeouts(ctx context.Context, now time.Time, limit int) (int, error) {
	return 0, ErrNotImplemented
}

// SendReminders 发送催办提醒。由 leader 副本周期调用。
//
// TODO:
//  1. ListDueForReminder（deadline - now 落入某个提醒档位且该档位未发过）
//  2. Notifier.Remind + MarkReminderSent
//  3. MarkReminderSent 必须在发送后立即执行，否则扫描周期内会重复催办，
//     审批人会被同一条提醒轰炸
func (h *Handler) SendReminders(ctx context.Context, now time.Time, limit int) (int, error) {
	return 0, ErrNotImplemented
}

// CancelByRun 取消某个 run 下的所有待审批单。
//
// TODO: UpdateStatus(canceled) + Notifier.Revoke。
// 撤回卡片很重要：不撤回会让审批人对着一个已作废的流程做决策，
// 而他的决策不会有任何效果——这种"操作了但没用"的体验会摧毁对系统的信任。
func (h *Handler) CancelByRun(ctx context.Context, runID, reason string) error {
	return ErrNotImplemented
}

// ListPending 查询待办列表。供 API 层与 IM 侧的"我的待办"使用。
//
// TODO: Store.ListPending；按 DeadlineAt 升序（最急的在前）。
func (h *Handler) ListPending(ctx context.Context, tenantID, userID string, limit, offset int) ([]*Request, error) {
	return nil, ErrNotImplemented
}
