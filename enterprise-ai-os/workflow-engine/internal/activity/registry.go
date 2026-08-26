// Package activity 提供活动（Activity）注册表与内置活动实现。
//
// 活动是工作流的原子执行单元——DAG 的一个节点对应一次活动调用。
//
// 设计原则：**活动必须是"薄"的**。它只做参数转换 + 一次外部调用 + 结果整形，
// 不含业务逻辑与控制流。理由是活动无法被工作流引擎观测内部状态：
// 一个活动里塞了 5 个步骤，失败时只能整体重试，无法定位到哪一步。
// 想要多步骤就应该拆成多个节点，让引擎去编排——那才是引擎存在的意义。
package activity

import (
	"context"
	"errors"
	"time"
)

var (
	ErrNotImplemented = errors.New("activity: not implemented")
	// ErrUnknownType 活动类型未注册。
	ErrUnknownType = errors.New("activity: unknown type")
	// ErrInvalidConfig 节点配置不合法（缺必填字段、类型错误）。
	// 归类为 validation_error，不重试。
	ErrInvalidConfig = errors.New("activity: invalid config")
	// ErrDisabled 活动类型已在配置中禁用。
	ErrDisabled = errors.New("activity: type disabled")
	// ErrBlockedTarget http.request 的目标地址被 SSRF 防护拦截。
	ErrBlockedTarget = errors.New("activity: target address blocked")
)

// Activity 是所有活动的统一接口。
type Activity interface {
	// Type 返回活动类型标识，如 "llm.complete"。
	// 必须与 DAG 定义里的 node.type 完全一致。
	Type() string

	// Idempotent 报告本活动是否幂等。
	//
	// 这个属性直接决定引擎在 worker 失联后能否自动重试：
	// 幂等活动（rag.search、llm.complete）可以放心重试；
	// 非幂等活动（notify.send 发消息、task.create 建待办）
	// 在"不确定是否已执行"时不能重试，只能转人工确认。
	//
	// 实现时务必诚实：把非幂等活动标成幂等，
	// 后果是用户偶发收到重复消息，且极难复现定位。
	Idempotent() bool

	// Validate 在**定义注册阶段**校验 config 的静态部分。
	//
	// 为什么要有这一步而不是留到运行时：一个流程可能第 7 个节点才用到
	// 某个配错的活动。运行时才发现意味着前 6 个节点白跑，
	// 而且其中可能已经花钱调了 LLM、已经发出了消息。
	//
	// 注意 config 中含未求值的模板，因此只能校验结构（必填键是否存在、
	// 枚举值是否合法），不能校验值。
	Validate(config map[string]any) error

	// Execute 执行活动。input 是模板求值后的实参。
	//
	// 约定：
	//   - 必须尊重 ctx 的取消与超时
	//   - 返回的错误应可被 engine.classifyError 识别（用哨兵错误或包装类型）
	//   - 输出为 map 而非结构体，因为下游节点靠模板按键取值
	Execute(ctx context.Context, input map[string]any) (map[string]any, error)
}

// Registry 是活动注册表。
//
// 为什么需要一个显式的注册表而不是 switch-case 分派：
//  1. DAG 定义校验时需要知道"有哪些合法的 type"（见 workflow.Limits.KnownActivities）
//  2. 活动可按配置启用/禁用（http.request 在高安全环境应关掉）
//  3. 便于测试时替换为 mock 活动
type Registry struct {
	activities map[string]Activity
	// enabled 记录配置中的启用状态。已注册但禁用的类型在
	// Validate 阶段就应报错，让使用者立刻知道"这个活动在本环境不可用"，
	// 而不是运行到那一步才失败。
	enabled map[string]bool
}

// NewRegistry 创建空注册表。
func NewRegistry() *Registry {
	return &Registry{
		activities: make(map[string]Activity),
		enabled:    make(map[string]bool),
	}
}

// Register 注册一个活动。
//
// TODO: 重复注册同一 Type 应返回错误而非静默覆盖——
// 静默覆盖会导致"我明明注册了自定义实现却没生效"这类难查的问题。
func (r *Registry) Register(a Activity, enabled bool) error {
	return ErrNotImplemented
}

// Get 按类型取活动。
func (r *Registry) Get(activityType string) (Activity, error) {
	return nil, ErrNotImplemented
}

// Supports 判断类型是否已注册且启用。
func (r *Registry) Supports(activityType string) bool {
	return false
}

// IsIdempotent 报告活动是否幂等。engine.ActivityRunner 接口的实现。
func (r *Registry) IsIdempotent(activityType string) bool {
	return false
}

// Run 执行活动。engine.ActivityRunner 接口的实现。
//
// TODO:
//  1. Get 取活动，未注册返回 ErrUnknownType
//  2. 禁用返回 ErrDisabled
//  3. 埋点：记录 activity_type / duration / status 到 metrics 与 tracing span
//  4. recover panic 转为 error——活动实现里的类型断言失败很常见，
//     不 recover 会崩掉整个 worker 进程
func (r *Registry) Run(ctx context.Context, activityType string, input map[string]any) (map[string]any, error) {
	return nil, ErrNotImplemented
}

// KnownTypes 返回所有已注册且启用的类型，供 DAG 校验使用。
func (r *Registry) KnownTypes() map[string]bool {
	return nil
}

// Deps 是构造内置活动所需的外部依赖。
//
// 集中在一个结构体里传递（而非每个活动构造函数各要一堆参数），
// 是为了让 BuildBuiltins 的签名稳定：新增一个活动需要新依赖时，
// 只改这个结构体，不改所有调用点。
type Deps struct {
	// HTTPClient 是共享的 HTTP 客户端。
	// 共享而非每活动一个：连接池复用能显著降低对下游的握手开销，
	// 且连接数上限可以集中控制。
	HTTPClient HTTPDoer

	// InternalToken 用于内部服务间调用的鉴权。
	InternalToken string

	// 各下游服务地址
	OctoServerURL    string
	OctoAdaptersURL  string
	OctoMatterURL    string
	ModelGatewayURL  string
	RAGPipelineURL   string
	MemoryServiceURL string

	// SSRF 防护配置，仅 http.request 使用。
	HTTPGuard *AddressGuard
}

// HTTPDoer 抽象 http.Client，便于测试打桩。
type HTTPDoer interface {
	Do(req any) (any, error)
}

// AddressGuard 是 http.request 活动的 SSRF 防护。
//
// 为什么这是本组件最重要的安全边界：DAG 定义由业务方编写并存在数据库里，
// 一个 http.request 节点若不限制目标地址，就等于给了任意人
// 从集群内部发起请求的能力——包括访问云元数据服务（169.254.169.254）
// 窃取实例凭据，以及扫描内网服务。
//
// 这不是"加固项"，而是开启 http.request 活动的前提条件。
type AddressGuard struct {
	// Mode: allowlist | denylist | off。默认 allowlist（最保守）。
	Mode         string
	AllowedHosts []string
	DeniedCIDRs  []string
	// FollowRedirects 必须默认 false。
	// 允许跟随重定向会让上面所有校验失效——
	// 目标返回 302 指向 169.254.169.254 即可绕过。
	FollowRedirects  bool
	MaxResponseBytes int64
}

// Check 校验目标 URL 是否允许访问。
//
// TODO:
//  1. 解析 URL，只允许 http/https（拒绝 file:// gopher:// 等）
//  2. **必须先 DNS 解析再校验 IP**，不能只校验域名。
//     攻击者可以把自己域名的 A 记录指向 127.0.0.1（DNS rebinding）
//  3. 逐个解析出的 IP 校验 DeniedCIDRs
//  4. allowlist 模式下校验 host 在白名单中
//  5. 理想实现还要在建连时用 DialContext 再校验一次实际连接的 IP，
//     防止 DNS 在校验与建连之间发生变化（TOCTOU）。
//     只在校验阶段做一次 DNS 是有窗口的
func (g *AddressGuard) Check(rawURL string) error {
	return ErrNotImplemented
}

// BuildBuiltins 按配置构造并注册全部内置活动。
//
// 内置活动清单及其定位：
//
//	agent.invoke      调 octo-server 起 Agent 会话。最重要的一个——
//	                  它让 workflow 成为 Lobster Agent 的上层编排者
//	llm.complete      调 model-gateway 做单次生成
//	rag.search        调 rag-pipeline 检索
//	memory.read       调 memory-service 读记忆
//	memory.write      调 memory-service 写记忆
//	notify.send       经 octo-adapters 发 IM 消息（非幂等）
//	task.create       在 octo-matter 建待办（非幂等）
//	approval.request  由 approval 模块处理，此处仅占位注册以通过校验
//	http.request      任意 HTTP 调用（逃生舱，受 AddressGuard 约束）
//	script.eval       沙箱表达式求值，做数据整形（纯函数，无副作用）
//	control.branch    显式分支节点（也可用 node.condition 实现）
//	control.loop      有界循环（需 allow_loops）
//
// TODO: 逐个构造并 Register，enabled 取自 config.activities.<name>.enabled。
func BuildBuiltins(deps Deps, enabled map[string]bool) (*Registry, error) {
	return nil, ErrNotImplemented
}

// ---------------------------------------------------------------------------
// 内置活动的配置结构
//
// 每个活动定义自己的 config 结构体并提供 decode 函数。
// 这样类型安全在活动内部得到保证，而 Node.Config 对外保持
// map[string]any 的灵活性（定义来自 YAML/DB，活动类型可扩展）。
// ---------------------------------------------------------------------------

// AgentInvokeConfig 是 agent.invoke 的配置。
type AgentInvokeConfig struct {
	// Agent 是 octo-server 中的 Agent 标识。
	Agent string `json:"agent"`
	// Prompt 是首轮输入，支持模板。
	Prompt string `json:"prompt"`
	// SpaceID 指定会话所属空间，影响 Agent 可访问的上下文范围。
	SpaceID string `json:"space_id,omitempty"`
	// Tools 限制本次会话可用的工具，为空则用 Agent 默认配置。
	// 收窄工具集是控制 Agent 行为边界的主要手段。
	Tools []string `json:"tools,omitempty"`
	// MaxTurns 限制会话轮数，防止 Agent 陷入工具调用循环烧钱。
	MaxTurns int `json:"max_turns,omitempty"`
	// ExpectSchema 声明期望的输出结构，用于让 Agent 返回结构化结果
	// 而非自由文本——后继节点需要按键取值。
	ExpectSchema map[string]any `json:"expect_schema,omitempty"`
	Timeout      time.Duration  `json:"timeout,omitempty"`
}

// LLMCompleteConfig 是 llm.complete 的配置。
type LLMCompleteConfig struct {
	// Model 可以是具体模型 id 或路由别名（auto:chat / auto:reasoning）。
	// 推荐用别名：流程定义不该硬编码具体型号，
	// 换模型时不必逐个改流程。
	Model       string  `json:"model,omitempty"`
	System      string  `json:"system,omitempty"`
	Prompt      string  `json:"prompt"`
	Temperature float64 `json:"temperature,omitempty"`
	MaxTokens   int     `json:"max_tokens,omitempty"`
	// JSONMode 要求模型返回 JSON，输出直接作为 map 供下游取值。
	// 不开时输出放在 output.text 里。
	JSONMode bool `json:"json_mode,omitempty"`
	// OutputSchema 配合 JSONMode 做结构约束与校验。
	OutputSchema map[string]any `json:"output_schema,omitempty"`
	TaskType     string         `json:"task_type,omitempty"` // 透传给网关做路由
}

// RAGSearchConfig 是 rag.search 的配置。
type RAGSearchConfig struct {
	Collection string         `json:"collection"`
	Query      string         `json:"query"`
	TopK       int            `json:"top_k,omitempty"`
	Filters    map[string]any `json:"filters,omitempty"`
	Rerank     *bool          `json:"rerank,omitempty"`
	// MinScore 过滤低相关结果。
	// 不设阈值时检索总会返回 top_k 条，即使全都不相关——
	// 这些噪音进入下游 prompt 会显著降低生成质量。
	MinScore float64 `json:"min_score,omitempty"`
}

// NotifySendConfig 是 notify.send 的配置。
type NotifySendConfig struct {
	// Target 支持 user:<id> / channel:<id> / role:<name>。
	Target  []string `json:"target"`
	Channel string   `json:"channel,omitempty"` // feishu / slack / discord
	Title   string   `json:"title,omitempty"`
	Content string   `json:"content"`
	// Card 为真时发送互动卡片而非纯文本。
	Card bool `json:"card,omitempty"`
	// DedupKey 让重试不产生重复消息。
	//
	// 这是把非幂等活动"尽量做成幂等"的手段：
	// 由活动生成 nodeRunID+attempt 的稳定 key 传给 adapters，
	// 由后者做去重。不能完全消除重复（adapters 也可能失败重试），
	// 但能覆盖绝大多数场景。
	DedupKey string `json:"dedup_key,omitempty"`
}

// HTTPRequestConfig 是 http.request 的配置。
type HTTPRequestConfig struct {
	Method  string            `json:"method,omitempty"`
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers,omitempty"`
	Body    any               `json:"body,omitempty"`
	// ExpectStatus 声明期望的状态码，不匹配则节点失败。
	// 为空时按 2xx 判定成功。
	ExpectStatus []int         `json:"expect_status,omitempty"`
	Timeout      time.Duration `json:"timeout,omitempty"`
	// ParseJSON 为真时把响应体解析为 map 供下游取值。
	ParseJSON bool `json:"parse_json,omitempty"`
}

// ScriptEvalConfig 是 script.eval 的配置。
type ScriptEvalConfig struct {
	// Expr 是求值表达式。只能做取值、映射、过滤、字符串处理，
	// 无 IO、无网络、无文件访问。
	//
	// 这个限制不是实现偷懒，而是刻意的：一旦脚本能产生副作用，
	// 事件溯源的重放就不再安全，引擎最核心的可审计性即丧失。
	Expr string `json:"expr"`
	// Output 指定结果的键名，默认 "result"。
	Output string `json:"output,omitempty"`
}
