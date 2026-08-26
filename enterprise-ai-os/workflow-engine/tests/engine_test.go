// Package tests 是 workflow-engine 的集成与行为测试。
//
// 为什么放在独立的 tests 包而不是各包内的 _test.go：
// 这里的测试跨包验证行为（DAG 定义 → 调度 → 执行 → 审批），
// 放在 internal/engine 里会形成 engine → workflow → engine 的测试期循环依赖。
// 各包内部的纯单测仍应就近放在对应包里。
package tests

import (
	"testing"
)

// TestDetectCycle 验证环检测能正确报出环路径。
//
// 这是本组件最关键的一个测试。环检测漏判的后果是调度器在运行时
// 无限循环唤醒同一组节点，事件日志暴涨、CPU 打满、且没有任何
// 明显的错误信息——是最难排查的故障形态之一。
//
// TODO: 实现以下用例
//
//	自环：      a → a                              期望报错且指出 a
//	二元环：    a → b → a                          期望环路径含 a,b
//	长环：      a → b → c → d → b                  期望识别出 b,c,d 而非 a
//	多个独立环：a → b → a, c → d → c               任一被报出即可
//	菱形（无环）：a → b,c → d                       期望通过
//	大规模无环：200 节点随机 DAG                    期望通过且耗时 <10ms
//
// 特别注意"长环"用例：入口 a 不在环上。用简单的"访问过就报错"
// 实现会错误地把 a 也算进环里，让使用者去改一个其实没问题的节点。
func TestDetectCycle(t *testing.T) {
	t.Skip("脚手架阶段：workflow.DetectCycle 尚未实现")
}

// TestValidateDefinition 验证静态校验的完整性。
//
// TODO: 覆盖 dag.go 中每一个导出的哨兵错误，确保
//   - 每种畸形定义都被拦下
//   - 错误信息里包含出问题的节点 id（这决定了报错是否可修复）
//
// 关键用例：
//
//	空 DAG                        → ErrEmptyDAG
//	重复节点 id                   → ErrDuplicateNodeID
//	依赖不存在的节点              → ErrUnknownDependency
//	所有节点都有依赖（无入口）    → ErrNoEntryNode
//	孤立的不可达节点              → ErrUnreachableNode
//	node.type 未注册              → ErrUnknownActivity
//	节点数超限                    → ErrTooManyNodes
//	approval 节点 approvers 为空  → 校验失败
func TestValidateDefinition(t *testing.T) {
	t.Skip("脚手架阶段：workflow.Validate 尚未实现")
}

// TestResolveNext 验证依赖满足判定。
//
// 这是仅次于环检测的高危逻辑。判错的两种后果：
//   - 过于严格 → 节点永远不 ready，流程静默卡死（最难发现）
//   - 过于宽松 → 节点在前驱未完成时就跑，读到空输入
//
// TODO: 覆盖以下矩阵
//
//	DependAll + 全部 succeeded              → 后继 ready
//	DependAll + 一个仍 running              → 后继保持 pending
//	DependAll + 一个 skipped 其余 succeeded → 后继 ready
//	  （skipped 必须算满足依赖，否则任何条件分支的汇合点都会死锁）
//	DependAll + 一个 failed（无 ContinueOnFailure） → 后继 skipped
//	  （必须是 skipped 而非留在 pending，否则 run 永远进不了终态）
//	DependAll + 一个 failed（有 ContinueOnFailure） → 后继 ready
//	DependAny + 任一 succeeded              → 后继 ready
//	DependAny + 重复触发                    → 后继只被唤醒一次
func TestResolveNext(t *testing.T) {
	t.Skip("脚手架阶段：engine.resolveNext 尚未实现")
}

// TestRetryPolicy 验证退避计算与重试判定。
//
// TODO:
//   - exponential 的间隔序列符合 initial × multiplier^(n-1) 并被 MaxInterval 截断
//   - jitter 开启时结果落在 [0.5×, 1.0×] 区间且多次调用不相同
//   - NonRetryableErrors 优先于 RetryableErrors（显式禁止胜过隐式允许）
//   - 未知错误类型默认**不**重试
func TestRetryPolicy(t *testing.T) {
	t.Skip("脚手架阶段：workflow.RetryPolicy 尚未实现")
}

// TestSchedulerClaimConcurrency 验证多副本抢任务不重复。
//
// TODO: 用 testcontainers 起真实 PostgreSQL（这个测试**必须**用真库，
// 因为验证的正是 FOR UPDATE SKIP LOCKED 的数据库行为，
// 用内存 fake 等于什么都没测）。
//
// 用例：插入 100 个 ready 节点，起 5 个 Scheduler 并发 Claim，
// 断言每个节点恰好被领取一次、总数为 100、无遗漏无重复。
func TestSchedulerClaimConcurrency(t *testing.T) {
	t.Skip("脚手架阶段：需要 testcontainers 与 Store 实现")
}

// TestLeaseReclaim 验证 worker 失联后任务被回收。
//
// 这是引擎自愈能力的核心。没有它，一次 Pod 驱逐就会让若干流程永久卡住。
//
// TODO: 领取节点后不续约，等租约过期，断言 ReclaimExpiredLeases
// 把节点退回 ready 且 attempt 未被错误地递增
// （心跳超时不是业务失败，不应消耗重试次数——
// 否则一次 Pod 重启就白白吃掉一次重试机会）。
func TestLeaseReclaim(t *testing.T) {
	t.Skip("脚手架阶段：需要 Store 实现")
}

// TestApprovalAggregate 验证多人审批的决策聚合。
//
// TODO: 逐一验证四种模式
//
//	any        1 人 approve → approved；全部 reject → rejected
//	           （注意 any 的 reject 条件是"全部"而非"任一"，
//	             写成任一会让 any 退化成 all 的驳回语义）
//	all        任一 reject → 立即 rejected（不等其他人）
//	quorum     approve 占比达阈值 → approved；abstain 不计入分子分母
//	sequential 越级审批被拒绝；任一 reject 立即终止
func TestApprovalAggregate(t *testing.T) {
	t.Skip("脚手架阶段：approval.aggregate 尚未实现")
}

// TestApprovalIdempotency 验证重复决策回调是幂等的。
//
// 重复回调在生产环境是常态（IM 平台投递重试 + 用户连点）。
//
// TODO: 同一 (approvalID, userID, decision) 连续提交 3 次，
// 断言只产生一次状态变更、只调用一次 ResumeApproval、
// 三次调用返回相同结果（而非第二次开始报错）。
func TestApprovalIdempotency(t *testing.T) {
	t.Skip("脚手架阶段：approval.Decide 尚未实现")
}

// TestApprovalCallbackSecurity 验证回调的安全防护。
//
// 审批回调是唯一能从外部改变业务结果的入口，安全测试不可省。
//
// TODO:
//   - 签名错误 → ErrInvalidSignature，且**不产生任何状态变更**
//   - 时间戳超出 ±5 分钟 → 拒绝（防重放：截获一次合法请求就能无限重放）
//   - 非审批人的合法签名请求 → ErrNotAuthorized
func TestApprovalCallbackSecurity(t *testing.T) {
	t.Skip("脚手架阶段：approval.DecideByCallback 尚未实现")
}

// TestAddressGuard 验证 http.request 活动的 SSRF 防护。
//
// TODO:
//   - 直接访问 169.254.169.254（云元数据）→ 拒绝
//   - 内网段 10./172.16./192.168./127. → 拒绝
//   - 域名 A 记录指向 127.0.0.1（DNS rebinding）→ 拒绝
//     （只校验域名字符串的实现会漏掉这一条，这是最容易被忽略的绕过方式）
//   - 302 重定向到内网 → 拒绝（FollowRedirects 必须默认 false）
//   - allowlist 模式下白名单外的公网地址 → 拒绝
func TestAddressGuard(t *testing.T) {
	t.Skip("脚手架阶段：activity.AddressGuard 尚未实现")
}

// TestEndToEndSimpleFlow 端到端跑通一条简单流程。
//
// TODO: 起真实 PG + Redis，活动全部打桩。流程定义：
//
//	a(llm.complete) → b(rag.search) ┐
//	                                ├→ d(script.eval)
//	a               → c(http.request)┘
//
// 断言：b 与 c 并行执行（开始时间重叠）、d 在两者都完成后才开始、
// run 最终 succeeded、事件日志的 sequence 严格递增无空洞。
func TestEndToEndSimpleFlow(t *testing.T) {
	t.Skip("脚手架阶段：需要完整实现")
}

// TestEndToEndApprovalFlow 端到端跑通含审批的流程。
//
// TODO: 断言
//   - 审批节点进入 waiting 后**不占用 worker**
//     （验证方式：把 worker 池设为 1，起 3 个含审批的 run，
//     断言三者都能推进到审批节点——若审批占 worker 则只有 1 个能推进）
//   - 决策为 reject 时节点是 succeeded 而非 failed，
//     且 output.decision == "reject"（驳回是合法业务结果，不是失败）
//   - 后继节点能通过 condition 正确分流到"驳回通知"分支
func TestEndToEndApprovalFlow(t *testing.T) {
	t.Skip("脚手架阶段：需要完整实现")
}

// TestRetryRunPartial 验证重跑只重跑失败及下游节点。
//
// TODO: 构造 a(成功) → b(失败) → c 的流程，重跑后断言
// a 的 Output 与 FinishedAt 未变（未被重新执行）、b 的 attempt 归零。
//
// 全量重跑会重复产生副作用并浪费已花掉的 LLM 成本，
// 这个测试是防止后续重构时把语义改错的护栏。
func TestRetryRunPartial(t *testing.T) {
	t.Skip("脚手架阶段：Scheduler.RetryRun 尚未实现")
}
