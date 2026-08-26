"""model-gateway 测试包。

测试分层与优先级（脚手架阶段先约定，避免后续各写各的）：

1. **registry / router 单测**（最高优先级，纯函数易测）
   - 别名展开、能力过滤、参数消毒、降级链解析
   - 关键用例：``deepseek-reasoner`` 传 temperature 必须被剔除而非报错；
     降级链上的模型必须能力等价，不等价要在加载期就失败

2. **middleware 单测**（需要 fakeredis）
   - 令牌桶的突发容量与恢复速率
   - 断路器三态迁移，尤其 HALF_OPEN 单次失败立即回 OPEN
   - 冷启动不误判：前 2 个请求失败不应触发失败率熔断

3. **provider 契约测试**（用 respx / httpx MockTransport 打桩）
   - 每家厂商的错误码映射正确
   - GLM 的 "HTTP 200 但 body 里是错误" 必须被识别为失败——
     这是最容易漏测、后果最严重的一条

4. **端到端**（testcontainers 起 redis + postgres，provider 仍打桩）
   - 主模型 500 → 自动降级到备选并返回成功，响应头带 X-Fallback-Reason
   - 超限返回 429 且带 Retry-After

不建议做的事：把真实厂商 API 纳入 CI。会产生费用、结果不稳定、
且厂商限流会让 CI 随机变红。真实连通性用独立的 smoke 任务定时跑。
"""
