# model-gateway

> **组件定位**：全平台唯一的模型出口——一个 OpenAI 兼容端点，背后按任务类型、负载与成本自动路由到 Qwen / DeepSeek / GLM / Yi 及私有化 vLLM。

## 为什么需要

OCTO 生态里，`octo-smart-summary`、`octo-speech`、`octo-matter`、各 channel 桥接器**各自内置 LLM client**，各自读自己的 API Key。这带来四个必然会爆的问题：

1. **成本不可见**：没人能回答"上个月哪个业务线烧了多少 token"。
2. **限流无处施加**：某个服务突然放量，直接把整个租户的厂商配额打满，其他服务集体 429。
3. **故障无法隔离**：厂商单点抖动 → 所有依赖该模型的功能同时不可用，没有 fallback。
4. **换模型要改 N 处代码**：新模型上线、旧模型下线都是跨仓库的散弹式修改。

**我们补的部分**：统一网关（路由 + 限流 + 熔断 + fallback + 计量 + 审计）。
**为什么不直接用 LiteLLM/OneAPI**：调研阶段评估过——它们的 provider 适配层可以借鉴，但企业场景需要的**按租户配额、按任务类型路由、与 octo-auth 打通的身份透传、成本归集到组织架构**都需要深度定制，且这三项恰好是网关的核心价值。因此选择自研路由层，provider 适配保持轻量。

## 技术选型

| 关注点 | 选型 | 理由 |
| --- | --- | --- |
| 语言 / 框架 | Python 3.11 + FastAPI | 流式响应（SSE）处理成熟；provider SDK 生态最全 |
| 对外协议 | **OpenAI 兼容**（`/v1/chat/completions` 等） | 存量代码、LangChain、各 Agent CLI 都默认支持，零改造接入 |
| 上游模型 | Qwen（阿里百炼）、DeepSeek、GLM（智谱）、Yi（零一万物）、vLLM（私有化） | 国产模型合规可控；DeepSeek 性价比高、Qwen 综合均衡、GLM 长文本强、Yi 备份 |
| 限流 | Redis 令牌桶（分布式） | 多实例部署下本地计数器会失效，必须集中式 |
| 熔断 | 自研三态断路器（closed / open / half-open） | 与路由降级链联动，标准库无现成实现 |
| 流式 | SSE 透传 + 首 token 计时 | 首 token 延迟是体感关键指标，必须单独度量 |
| 计量 | Redis 实时累计 + PostgreSQL 落账 | 实时用于限额判定，落账用于对账与成本分摊 |

## 架构设计

```
  请求（OpenAI 格式）
        │
        ▼
 ┌─────────────────────────────────────────────────────────┐
 │ middleware/  按顺序执行，任一环拒绝则快速失败            │
 │  ① auth        octo-auth 校验 → tenant_id / user_id     │
 │  ② rate_limiter  Redis 令牌桶（租户 / 用户 / 模型 三级） │
 │  ③ quota       月度预算校验，超支拒绝或降级到廉价模型     │
 │  ④ audit       记录请求指纹（不落原文，仅哈希+长度）      │
 └───────────────────────┬─────────────────────────────────┘
                         ▼
 ┌─────────────────────────────────────────────────────────┐
 │ router/strategy.py   选择目标模型                        │
 │  · 显式指定 model → 直连（尊重调用方意图）                │
 │  · 指定别名/任务类型 → 按策略选：                         │
 │      task_type   代码→deepseek-coder 长文→glm-4-long     │
 │      cost        同等能力选单价低的                       │
 │      latency     选 P95 最低的                            │
 │      load        最少活跃连接数                           │
 │  · 结合 registry 的健康状态与 circuit_breaker 状态过滤    │
 └───────────────────────┬─────────────────────────────────┘
                         ▼
 ┌─────────────────────────────────────────────────────────┐
 │ providers/    厂商适配：请求/响应格式归一化               │
 │  qwen.py  deepseek.py  glm.py  yi.py                     │
 │  统一处理：参数映射、错误码归一、流式分块解析、token 统计  │
 └───────────────────────┬─────────────────────────────────┘
                         ▼
 ┌─────────────────────────────────────────────────────────┐
 │ circuit_breaker 记录成败 → 连续失败开断路器               │
 │ 失败时按 fallback_chain 换下一个 provider 重试            │
 └───────────────────────┬─────────────────────────────────┘
                         ▼
                  OpenAI 格式响应 + usage + 路由决策头
```

核心模块：

| 模块 | 职责 | 关键抽象 |
| --- | --- | --- |
| `registry/models.py` | 模型注册中心：能力标签、单价、限额、健康状态 | `ModelSpec`、`ModelRegistry`、`ModelCapability` |
| `router/strategy.py` | 路由决策与 fallback 链 | `RoutingStrategy` 基类 + 五种实现、`RoutingDecision` |
| `providers/*.py` | 厂商 SDK 适配与格式归一 | `BaseProvider` → `QwenProvider` / `DeepSeekProvider` / `GLMProvider` / `YiProvider` |
| `middleware/rate_limiter.py` | Redis 分布式令牌桶，三级限流 | `RateLimiter`、`RateLimitRule` |
| `middleware/circuit_breaker.py` | 三态断路器 | `CircuitBreaker`、`CircuitState` |
| `api/routes.py` | OpenAI 兼容端点 | `router` |

## API 设计

### OpenAI 兼容（存量代码零改造）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | 对话补全，支持 `stream=true`、function/tool calling |
| `POST` | `/v1/completions` | 文本补全（兼容旧客户端） |
| `POST` | `/v1/embeddings` | 向量化（`rag-pipeline` / `memory-service` 的上游） |
| `POST` | `/v1/rerank` | 重排（非 OpenAI 标准，对齐 Cohere 格式） |
| `GET` | `/v1/models` | 列出可用模型（含别名与能力标签） |

### 网关管理面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/admin/models` | 模型注册详情（单价、限额、健康、熔断状态） |
| `PUT` | `/v1/admin/models/{id}` | 热更新模型配置（改权重、临时下线，不重启） |
| `GET` | `/v1/admin/usage` | 用量与成本查询（按租户/用户/模型/时间聚合） |
| `GET` | `/v1/admin/circuit-breakers` | 断路器状态，运维排障入口 |
| `POST` | `/v1/admin/circuit-breakers/{id}/reset` | 手动半开，厂商恢复后免等冷却 |

请求示例——**用别名让网关自己挑模型**：

```jsonc
// POST /v1/chat/completions
{
  "model": "auto:code",            // 别名而非具体模型；也可直接写 "deepseek-coder"
  "messages": [{"role": "user", "content": "重构这段函数"}],
  "stream": true,
  "max_tokens": 2048,
  // 网关扩展字段（非 OpenAI 标准，可选）
  "x_routing": { "strategy": "cost", "exclude": ["yi-large"], "max_latency_ms": 3000 }
}
```

响应头透出路由决策，便于排障与成本归因：

```
X-Model-Selected: deepseek-coder
X-Model-Provider: deepseek
X-Routing-Strategy: task_type
X-Fallback-Depth: 0
X-Request-Cost-Usd: 0.00042
X-RateLimit-Remaining: 4871
```

## 与 OCTO 的集成方式

1. **反向替换 OCTO 内部 LLM 配置**：`octo-smart-summary`、`octo-matter`、`octo-speech` 及各 channel 桥接器均支持自定义 `OPENAI_BASE_URL`。把它们指向 `http://model-gateway:8200/v1` 即完成接入——**零代码改动**，这是选 OpenAI 兼容协议的最大回报。
2. **身份与配额**：请求经 `octo-auth` 校验后拿到 `tenant_id` / `user_id`，限流与计费按此维度统计；OCTO 侧的 Space 映射为成本中心。
3. **凭证收口**：厂商 API Key **只**注入 model-gateway 容器（见 `.env.example` 的分区注释），其他任何服务不得持有。撤换 Key 只需重启网关。
4. **可观测性**：指标暴露给 OCTO 侧已有的 Prometheus / OTel Collector，不另建监控栈。

## 开发指南

```bash
cd model-gateway
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config/config.yaml.example config/config.yaml
# 至少填一个厂商的 Key，否则所有路由候选都不可用
export DEEPSEEK_API_KEY=sk-xxx

docker compose up -d redis postgres          # 限流与计量依赖

uvicorn src.main:app --reload --host 0.0.0.0 --port 8200
```

冒烟验证（用任意 OpenAI 客户端）：

```bash
curl localhost:8200/v1/models

curl localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto:chat","messages":[{"role":"user","content":"ping"}]}'
```

测试与检查：

```bash
pytest -q                    # provider 用 respx 打桩，不真实出网
ruff check src tests
mypy src
```

## 当前状态

**脚手架阶段，待实现。** 已完成：目录结构、路由策略与 provider 抽象、限流/熔断骨架、模型注册表结构、完整配置模板。

实现顺序建议（本组件是其余四个的前置依赖，优先级最高）：

- [ ] `registry/models.py` — 模型注册表与配置热加载
- [ ] `providers/base` + `deepseek.py` — 打通一条链路（非流式 → 流式）
- [ ] `api/routes.py` — `/v1/chat/completions` 与 `/v1/models`
- [ ] `middleware/rate_limiter.py` — Redis 令牌桶
- [ ] `router/strategy.py` — 先 `explicit` + `priority`，再补 cost/latency/load
- [ ] `middleware/circuit_breaker.py` + fallback 链
- [ ] 其余 provider（qwen / glm / yi）
- [ ] 用量落账与 `/v1/admin/usage`
