// Package api 提供 HTTP 接口层。
//
// 本层只做三件事：请求解析与校验、调用内部服务、错误到 HTTP 状态码的映射。
// 不含任何业务逻辑——业务在 engine / approval / workflow 包里。
//
// 这条边界的实际价值：将来若要加 gRPC 或 CLI 入口，
// 只需再写一个薄适配层，不必重新实现任何逻辑。
package api

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/mininglamp/enterprise-ai-os/workflow-engine/internal/approval"
	"github.com/mininglamp/enterprise-ai-os/workflow-engine/internal/engine"
	"github.com/mininglamp/enterprise-ai-os/workflow-engine/internal/workflow"
)

var ErrNotImplemented = errors.New("api: not implemented")

// AuthContext 是请求身份，由鉴权中间件解析后放入 context。
type AuthContext struct {
	TenantID string
	SpaceID  string
	UserID   string
	Roles    []string

	// IsInternal 标识来自其他自研服务的调用（凭 INTERNAL_SERVICE_TOKEN）。
	IsInternal  bool
	ServiceName string
	IsAdmin     bool
}

// HasRole 判断是否具备某角色。
func (a *AuthContext) HasRole(role string) bool {
	for _, r := range a.Roles {
		if r == role {
			return true
		}
	}
	return false
}

// AuthProvider 校验凭据并解析身份。实现对接 octo-auth。
type AuthProvider interface {
	// Authenticate 解析 session / bot-key / user-key 三种凭据。
	//
	// 复用 octo-auth 而非自建用户体系，是整个 Enterprise AI OS
	// 的统一约定：审批人标识必须与 OCTO 侧一致，
	// 否则"给张三发审批"这件事在两个系统里指的不是同一个人。
	Authenticate(ctx context.Context, r *http.Request) (*AuthContext, error)
}

// DefinitionStore 是定义的持久化依赖。
type DefinitionStore interface {
	// Upsert 注册定义。
	//
	// 返回的 created 表示是否产生了新版本。
	// 若内容指纹与最新版相同则复用旧版本号——
	// 没有这个机制的话 CI 每次部署都会造一个新版本，
	// keep_versions 窗口会被无意义的重复版本迅速占满。
	Upsert(ctx context.Context, def *workflow.Definition) (version int, created bool, err error)
	Get(ctx context.Context, tenantID, name string, version int) (*workflow.Definition, error)
	List(ctx context.Context, tenantID string, limit, offset int) ([]*workflow.Definition, error)
	ListVersions(ctx context.Context, tenantID, name string) ([]int, error)
	Delete(ctx context.Context, tenantID, name string) error
}

// RunStore 是实例查询依赖。
type RunStore interface {
	Get(ctx context.Context, tenantID, runID string) (*workflow.Run, error)
	List(ctx context.Context, q RunQuery) ([]*workflow.Run, int, error)
	ListNodeRuns(ctx context.Context, runID string) ([]*workflow.NodeRun, error)
	ListEvents(ctx context.Context, runID string, afterSeq int64, limit int) ([]*workflow.Event, error)
}

// RunQuery 是实例列表的查询条件。
type RunQuery struct {
	TenantID       string
	DefinitionName string
	Status         []workflow.RunStatus
	TriggeredBy    string
	CreatedAfter   *time.Time
	CreatedBefore  *time.Time
	Limit          int
	Offset         int
}

// Handler 是 HTTP 处理器集合。
type Handler struct {
	auth      AuthProvider
	defs      DefinitionStore
	runs      RunStore
	scheduler *engine.Scheduler
	approvals *approval.Handler
	limits    workflow.Limits
}

// NewHandler 构造处理器。
func NewHandler(
	auth AuthProvider,
	defs DefinitionStore,
	runs RunStore,
	scheduler *engine.Scheduler,
	approvals *approval.Handler,
	limits workflow.Limits,
) *Handler {
	return &Handler{
		auth:      auth,
		defs:      defs,
		runs:      runs,
		scheduler: scheduler,
		approvals: approvals,
		limits:    limits,
	}
}

// Routes 装配路由。
//
// 中间件顺序（自外向内），每一层的位置都有理由：
//  1. RequestID   —— 生成 trace_id，后续所有日志都要带
//  2. Recoverer   —— 尽可能靠外，才能兜住内层所有 panic
//  3. Logger      —— 在 Recoverer 内侧，这样 panic 也会被记一条日志
//  4. Timeout     —— 全局请求超时兜底
//  5. CORS        —— 仅对需要浏览器直连的路径开启
//  6. Auth        —— 业务路由前，且**在**审批回调路由之外
//     （回调用的是签名验证而非 token，两套机制不能混）
//
// TODO: 用 chi 装配上述中间件与下列路由。
func (h *Handler) Routes() http.Handler {
	return nil
}

// ---------------------------------------------------------------------------
// 定义管理
// ---------------------------------------------------------------------------

// CreateDefinition 处理 POST /api/v1/definitions。
//
// 接受 YAML 或 JSON（按 Content-Type 判断）。
//
// TODO:
//  1. 鉴权：需要 workflow-admin 角色。内部服务**不能**改定义——
//     权限分离：服务可以提交 run，但不能改流程逻辑
//  2. workflow.Parse 完成解析与全部静态校验
//  3. 校验失败返回 400 并附带**具体节点 id 与原因**。
//     只回 "invalid definition" 会让用户在 200 个节点里瞎找
//  4. defs.Upsert，返回 {name, version, created}
func (h *Handler) CreateDefinition(w http.ResponseWriter, r *http.Request) {
}

// ValidateDefinition 处理 POST /api/v1/definitions/{name}/validate。
//
// 只校验不保存。存在的意义：让编辑器/CI 能在提交前拿到校验反馈，
// 而不必先污染定义库再回滚。
//
// TODO: Parse 后立即丢弃，返回校验结果（含警告——
// 例如"节点 x 不可达"、"未设置 timeout 将使用默认值 300s"这类
// 不阻断但值得提示的信息）。
func (h *Handler) ValidateDefinition(w http.ResponseWriter, r *http.Request) {
}

// GetDefinition 处理 GET /api/v1/definitions/{name}?version=N。
func (h *Handler) GetDefinition(w http.ResponseWriter, r *http.Request) {
}

// ListDefinitions 处理 GET /api/v1/definitions。
func (h *Handler) ListDefinitions(w http.ResponseWriter, r *http.Request) {
}

// DeleteDefinition 处理 DELETE /api/v1/definitions/{name}。
//
// TODO: 有进行中的 run 时**不能物理删除**——那些 run 需要读定义才能继续。
// 正确做法是标记为 archived：不能再启动新实例，但存量实例照常执行完。
func (h *Handler) DeleteDefinition(w http.ResponseWriter, r *http.Request) {
}

// ---------------------------------------------------------------------------
// 实例管理
// ---------------------------------------------------------------------------

// StartRunBody 是 POST /api/v1/runs 的请求体。
type StartRunBody struct {
	Definition string         `json:"definition"`
	Version    int            `json:"version,omitempty"`
	Input      map[string]any `json:"input,omitempty"`
	// IdempotencyKey 防重复提交。事件触发场景必填。
	IdempotencyKey string `json:"idempotency_key,omitempty"`
	Priority       int    `json:"priority,omitempty"`
	// Async 为 false 时同步等待 run 完成（有超时上限）。
	//
	// 提供同步模式是为了简化调用方代码（比如 octo-server 的 Skill
	// 想要"调一下拿结果"），但它只适用于短流程。
	// 含审批节点的流程用同步模式必然超时，实现时应当直接拒绝
	// 而不是让调用方等到超时——审批流程的时长是定义决定的、可预知的。
	Async       bool          `json:"async,omitempty"`
	WaitTimeout time.Duration `json:"wait_timeout,omitempty"`
}

// StartRun 处理 POST /api/v1/runs。
//
// TODO:
//  1. 解析并校验 body
//  2. scheduler.StartRun
//  3. Async=false 时轮询等待终态（上限 WaitTimeout，默认 60s）；
//     若定义中含 approval.request 节点则直接返回 400 提示改用异步
//  4. 返回 202（异步）或 200（同步完成）
func (h *Handler) StartRun(w http.ResponseWriter, r *http.Request) {
}

// GetRun 处理 GET /api/v1/runs/{run_id}。
//
// 返回 run 本身 + 各节点状态，让前端一次请求就能画出执行进度图。
//
// TODO: 节点的 Input/Output 快照可能很大，默认**不返回**，
// 由 ?include=io 显式请求。默认全返会让列表页拉几 MB 数据。
func (h *Handler) GetRun(w http.ResponseWriter, r *http.Request) {
}

// ListRuns 处理 GET /api/v1/runs。
func (h *Handler) ListRuns(w http.ResponseWriter, r *http.Request) {
}

// GetRunEvents 处理 GET /api/v1/runs/{run_id}/events。
//
// 事件日志是审计凭据，接口需支持 after_seq 增量拉取，
// 让前端可以持续追踪一个长流程而不必反复全量拉。
//
// TODO: 支持 ?after_seq= 与 ?limit=；考虑提供 SSE 变体做实时推送。
func (h *Handler) GetRunEvents(w http.ResponseWriter, r *http.Request) {
}

// PauseRun 处理 POST /api/v1/runs/{run_id}/pause。
func (h *Handler) PauseRun(w http.ResponseWriter, r *http.Request) {
}

// ResumeRun 处理 POST /api/v1/runs/{run_id}/resume。
func (h *Handler) ResumeRun(w http.ResponseWriter, r *http.Request) {
}

// CancelRun 处理 POST /api/v1/runs/{run_id}/cancel。
func (h *Handler) CancelRun(w http.ResponseWriter, r *http.Request) {
}

// RetryRun 处理 POST /api/v1/runs/{run_id}/retry。
//
// TODO: 只允许 failed 状态的 run 重试。
// 记录 actor 到事件日志——"谁重跑的"在事后复盘时是必要信息。
func (h *Handler) RetryRun(w http.ResponseWriter, r *http.Request) {
}

// ---------------------------------------------------------------------------
// 审批
// ---------------------------------------------------------------------------

// ListApprovals 处理 GET /api/v1/approvals。
//
// 默认只返回当前用户的待办。管理员可加 ?all=true 看全部。
//
// TODO: approvals.ListPending；按截止时间升序。
func (h *Handler) ListApprovals(w http.ResponseWriter, r *http.Request) {
}

// GetApproval 处理 GET /api/v1/approvals/{id}。
func (h *Handler) GetApproval(w http.ResponseWriter, r *http.Request) {
}

// DecideBody 是审批决策请求体。
type DecideBody struct {
	Decision string `json:"decision"` // approve | reject | abstain | delegate
	Comment  string `json:"comment,omitempty"`
	// DelegateTo 仅 decision=delegate 时使用。
	DelegateTo string `json:"delegate_to,omitempty"`
}

// DecideApproval 处理 POST /api/v1/approvals/{id}/decide。
//
// 这是走正常鉴权的路径（用户在 Web 端操作）。
// IM 侧的按钮点击走 ApprovalCallback，两者鉴权机制不同。
//
// TODO: approvals.Decide；ErrAlreadyDecided 返回 200 + 当前状态而非错误
// （幂等语义：用户连点两次不该看到报错）。
func (h *Handler) DecideApproval(w http.ResponseWriter, r *http.Request) {
}

// ApprovalCallback 处理 POST /api/v1/approvals/callback。
//
// 这是 IM 平台的 webhook 入口，**不走 token 鉴权而走签名验证**。
//
// 为什么必须单独一条路径：IM 平台无法携带我们的 session token，
// 它只能按自己的协议签名。把它塞进普通鉴权链会导致要么放行未鉴权请求，
// 要么回调永远 401。
//
// 安全要求（缺一不可）：
//  1. HMAC 验签
//  2. 时间戳窗口校验（防重放）
//  3. 幂等（IM 会重试投递）
//
// TODO: approvals.DecideByCallback。
// 注意返回体要符合 IM 平台的期望格式（飞书要求特定的 challenge 响应），
// 否则平台会认为投递失败并持续重试。
func (h *Handler) ApprovalCallback(w http.ResponseWriter, r *http.Request) {
}

// ---------------------------------------------------------------------------
// 探针与指标
// ---------------------------------------------------------------------------

// Healthz 处理 GET /healthz。存活探针，不检查依赖。
//
// 刻意不检查数据库：存活探针失败会导致 Pod 被重启。
// 数据库短暂不可用时重启 Pod 毫无帮助，只会让恢复更慢
// （重启后还要重跑迁移、重建连接池）。依赖检查属于 readyz。
func (h *Handler) Healthz(w http.ResponseWriter, r *http.Request) {
}

// Readyz 处理 GET /readyz。就绪探针，检查数据库与 worker 池。
//
// TODO: 检查 DB ping + scheduler.Stats()。
// 判定标准：DB 可用且 worker 池已启动。
// **不要**把"是否为 leader"纳入判定——非 leader 副本同样能处理请求，
// 把它们摘掉会让容量瞬间降到 1/N。
func (h *Handler) Readyz(w http.ResponseWriter, r *http.Request) {
}

// Metrics 处理 GET /metrics。Prometheus 格式。
func (h *Handler) Metrics(w http.ResponseWriter, r *http.Request) {
}

// ---------------------------------------------------------------------------
// 错误映射
// ---------------------------------------------------------------------------

// ErrorResponse 是统一错误响应体。
type ErrorResponse struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
		// NodeID 在定义校验失败时指出问题节点，让用户能直接定位。
		NodeID string `json:"node_id,omitempty"`
		// Details 承载结构化补充信息（如环路径）。
		Details map[string]any `json:"details,omitempty"`
	} `json:"error"`
	TraceID string `json:"trace_id,omitempty"`
}

// writeError 把内部错误映射为 HTTP 响应。
//
// 映射表（用 errors.Is 判定，不靠字符串匹配）：
//
//	workflow.ErrCycleDetected / ErrUnknownDependency / ErrDuplicateNodeID
//	  / ErrUnknownActivity / ErrTooManyNodes         → 400（用户输入问题）
//	approval.ErrNotAuthorized                        → 403
//	approval.ErrInvalidSignature                     → 401
//	approval.ErrCommentRequired                      → 400
//	engine.ErrRunCanceled                            → 409
//	未找到                                            → 404
//	engine.ErrNotImplemented                         → 501
//	其他                                              → 500
//
// 5xx **不得回显内部错误详情**（SQL、内部地址、堆栈），只回 trace_id，
// 详情落日志。回显内部信息既是信息泄露，也会让使用者据此写出
// 依赖内部实现的错误处理逻辑。
//
// TODO: 按上表实现。
func writeError(w http.ResponseWriter, r *http.Request, err error) {
}

// writeJSON 输出 JSON 响应。
func writeJSON(w http.ResponseWriter, status int, body any) {
}
