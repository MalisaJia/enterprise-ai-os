# Enterprise AI OS — 自研组件

> 与 [Mininglamp-OSS / OCTO](https://github.com/Mininglamp-OSS) 生态**互补**的自研部分。
>
> OCTO 生态提供了 Enterprise AI OS 约 **70%** 的现成能力（接入层、Agent 调度、Agent 桥接、MCP 工具生态、基础设施）。
> 本仓库只做剩下的 **30%**——OCTO 明确缺失、且无法通过配置复用解决的能力。

## 1. 为什么是这五个组件

依据 `OCTO-Research-Report.md`「核心缺口」章节，OCTO 生态存在 5 处硬缺口：

| 缺失能力 | OCTO 现状 | 本仓库组件 | 优先级 |
| --- | --- | --- | --- |
| Vector Store / RAG | 无 embedding、无向量检索，`octo-search-indexer` 仅 OpenSearch 全文检索 | `rag-pipeline` | **P0** |
| 独立 Model Gateway | LLM 调用分散在各 Go 服务内部，无统一路由/限流/降级 | `model-gateway` | **P0** |
| DAG Workflow | `multica` 是 issue-driven 任务分配，不是 DAG 编排，无人工审批节点 | `workflow-engine` | **P1** |
| 长期记忆 | `octo-spec` 是文件级项目记忆，`octo-smart-summary` 只做单次对话摘要 | `memory-service` | **P1** |
| 会议 Agent（完整版） | `octo-speech` 只有 ASR，无摘要/决策提取/行动项分发 | `meeting-agent` | **P2** |

**原则**：能复用 OCTO 的绝不自研。本仓库不实现 IM、认证、RBAC、多端 UI、MCP 目录、Agent 桥接——这些全部直接用 OCTO。

## 2. 组件清单

| 组件 | 优先级 | 语言 | 端口 | 定位 |
| --- | --- | --- | --- | --- |
| [`rag-pipeline`](./rag-pipeline) | P0 | Python / FastAPI | 8100 | 文档摄入、分块、embedding、混合检索、重排 |
| [`model-gateway`](./model-gateway) | P0 | Python / FastAPI | 8200 | OpenAI 兼容统一入口，多模型路由 + 限流 + 熔断 + 成本核算 |
| [`workflow-engine`](./workflow-engine) | P1 | Go | 8300 | DAG 工作流编排，参考 Temporal，支持人工审批节点 |
| [`memory-service`](./memory-service) | P1 | Python / FastAPI | 8400 | 个人 / 团队 / 组织三层长期记忆，参考 mem0 |
| [`meeting-agent`](./meeting-agent) | P2 | Python / FastAPI | 8500 | 转录 → 摘要 → 决策/行动项提取 → 分发闭环 |

## 3. 整体架构

自研组件（`★` 标记）如何嵌入 OCTO 生态：

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ L1 接入层                          【100% 复用 OCTO】                          │
│  octo-web (React/Electron) · octo-android · octo-ios · octo-admin · octo-cli  │
└───────────────────────────────────┬───────────────────────────────────────────┘
                                    │ REST + WebSocket
┌───────────────────────────────────▼───────────────────────────────────────────┐
│ L2 智能调度层                                                                  │
│  ┌─────────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐ │
│  │ multica             │  │ octo-server      │  │ ★ workflow-engine   :8300│ │
│  │ Squads/Leader 路由  │  │ Lobster 调度     │  │ DAG 编排 · 人工审批       │ │
│  │ Autopilot 定时触发  │  │ IM 控制面/RBAC   │  │ 补:确定性多步流程        │ │
│  │ (issue-driven)      │  │                  │  │                          │ │
│  └─────────┬───────────┘  └────────┬─────────┘  └────────────┬─────────────┘ │
└────────────┼───────────────────────┼─────────────────────────┼───────────────┘
             │                       │                         │
┌────────────▼───────────────────────▼─────────────────────────▼───────────────┐
│ L3 Agent 执行层                    【100% 复用 OCTO】                          │
│  codex-channel-octo · cc-channel-octo · openclaw-channel-octo                 │
│  hermes-channel-octo · octo-adapters (Slack/Discord/飞书/Telegram)            │
└──────┬──────────────────────────────────────────────────────┬─────────────────┘
       │ 检索上下文                                            │ 读写记忆
┌──────▼──────────────────────────────┐  ┌────────────────────▼─────────────────┐
│ L4 上下文与记忆层                    │  │ ★ memory-service            :8400   │
│  ┌───────────────────────────────┐  │  │  personal / team / org 三层记忆      │
│  │ ★ rag-pipeline        :8100   │  │  │  提取 · 冲突消解 · 衰减 · 整合        │
│  │  ingest → chunk → embed →     │  │  │  复用: octo-smart-summary 摘要模式   │
│  │  hybrid retrieve → rerank     │  │  └────────────────────┬─────────────────┘
│  └───────────────────────────────┘  │                       │
│  复用: octo-spec(项目记忆)          │  ┌────────────────────▼─────────────────┐
│        octo-search-indexer(BM25)    │──▶ pgvector / Milvus · Neo4j(可选)      │
└─────────────────────────────────────┘  └──────────────────────────────────────┘
       ▲                                          ▲
       │ 所有 LLM / embedding 调用统一走网关         │
┌──────┴──────────────────────────────────────────┴────────────────────────────┐
│ L5 模型服务层                                                                  │
│  ★ model-gateway                                                      :8200  │
│   OpenAI 兼容 API · 按任务类型/负载/成本路由 · 限流 · 熔断 · fallback · 计量    │
│   Providers: Qwen · DeepSeek · GLM · Yi · (vLLM 私有化)                        │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ▲
┌───────────────────────────────────┴──────────────────────────────────────────┐
│ L6 工具与连接器层                  【100% 复用 OCTO】                          │
│  octo-marketplace (MCP/Skill 目录) · octo-skills · octo-adapters              │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ 业务 Agent 层                                                                 │
│  ★ meeting-agent                                                      :8500  │
│   octo-speech(ASR) ─▶ 转录 ─▶ 摘要 ─▶ 决策/行动项提取 ─▶ 分发                 │
│                                            └─▶ octo-matter (任务/待办)        │
│                                            └─▶ octo-adapters (飞书/IM)        │
│                                            └─▶ workflow-engine (审批流)       │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ L7 基础设施层                                                                  │
│  【复用 OCTO】octo-lib · octo-im/WuKongIM · octo-auth · octo-deployment       │
│  【本仓库依赖】PostgreSQL+pgvector · Redis · Milvus(可选) · Kafka · MinIO      │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 关键集成契约

1. **认证统一走 `octo-auth`**：所有自研服务不自建用户体系，通过 `octo-auth` SDK 校验 `session` / `bot-key` / `user-key`，并透传 `tenant_id` / `space_id` 做数据隔离。
2. **事件统一走 Kafka**：OCTO 消息事件（`octo.message.created` 等）由 `rag-pipeline` / `memory-service` 消费；自研组件产出事件（`rag.document.indexed`、`meeting.actions.extracted`）回写 Kafka 供 OCTO 侧消费。
3. **模型调用单一出口**：`rag-pipeline` / `memory-service` / `meeting-agent` / `workflow-engine` 一律不直连模型厂商，统一调 `model-gateway`，实现集中限流与成本归集。

## 4. 快速开始

```bash
cd enterprise-ai-os
cp .env.example .env          # 填入模型 API Key 等
docker compose up -d postgres redis kafka minio    # 只起基础设施
docker compose ps
```

启动全部（基础设施 + 自研服务）：

```bash
docker compose up -d --build
```

可选：使用 Milvus 替代 pgvector（二选一，见 `.env` 的 `VECTOR_BACKEND`）：

```bash
docker compose --profile milvus up -d
```

健康检查：

```bash
curl localhost:8100/healthz   # rag-pipeline
curl localhost:8200/healthz   # model-gateway
curl localhost:8300/healthz   # workflow-engine
curl localhost:8400/healthz   # memory-service
curl localhost:8500/healthz   # meeting-agent
```

单组件本地开发见各组件 README 的「开发指南」。

## 5. 当前状态

**脚手架阶段**。所有组件已完成目录结构、接口定义、配置模板、编排文件；核心业务逻辑标记为 `TODO`，待各 owner 认领实现。

实施顺序建议：`model-gateway` → `rag-pipeline` → `memory-service` → `workflow-engine` → `meeting-agent`
（后四者都依赖 `model-gateway` 作为模型出口，故它先行。）
