# Mininglamp-OSS "章鱼"（OCTO）系统调研报告

## 一、组织概览

**Mininglamp-OSS**（明略科技开源组织）围绕 **OCTO** 平台构建开源生态。OCTO 定位是 **AI-Native 团队协作平台**（Open Workplace for Humans x AI Agents），核心理念是让 AI Agent（称为 "Lobster"，基于 OpenClaw 框架的数字分身）作为一等公民参与团队协作。

GitHub 组织共 33 个仓库，整体活跃度高，大部分在 2026 年 8 月仍在持续更新。

---

## 二、仓库总览表

| # | 仓库名 | 描述 | Stars | 语言 | 许可证 |
|---|--------|------|-------|------|--------|
| 1 | **octo-server** | Go 后端核心：REST/WS API + Lobster Agent 调度 + WuKongIM 控制面 | 745 | Go | Apache-2.0 |
| 2 | **octo-web** | Web & PC (Electron) 客户端，React+TS | 865 | TypeScript | Apache-2.0 |
| 3 | **octo-android** | Android 原生客户端 | 621 | Java | Apache-2.0 |
| 4 | **octo-ios** | iOS 原生客户端 | 639 | Obj-C | Apache-2.0 |
| 5 | **octo-admin** | 管理后台（租户/用户/空间/频道/Agent 管理） | 37 | TypeScript | Apache-2.0 |
| 6 | **octo-cli** | 元数据驱动 CLI，48 操作 7 域，JSON envelope I/O | 600 | Go | Apache-2.0 |
| 7 | **octo-lib** | Go 基础库：协议类型、加密、存储适配器、HTTP 框架、事件总线 | 27 | Go | Apache-2.0 |
| 8 | **octo-smart-summary** | LLM 驱动的对话摘要服务 | 32 | Go | Apache-2.0 |
| 9 | **octo-adapters** | 第三方适配器（Slack/Discord/飞书/Telegram/AI 提供商桥接） | 40 | TypeScript | Apache-2.0 |
| 10 | **octo-speech** | 多引擎 ASR 微服务（Gemini/GPT/Qwen） | 26 | Go | Apache-2.0 |
| 11 | **octo-marketplace** | Skills & MCP 服务器目录发布服务 | 1 | Go | Apache-2.0 |
| 12 | **octo-matter** | 任务/待办微服务 + LLM 提取 | 31 | Go | Apache-2.0 |
| 13 | **octo-search-indexer** | Kafka + OpenSearch 消息搜索索引 | 26 | Go | Apache-2.0 |
| 14 | **octo-deployment** | K8s 部署清单（Kustomize） | 52 | Shell | Apache-2.0 |
| 15 | **octo-auth** | 统一认证 SDK（Go/TS），验证 session/bot/user-key | 17 | Go | Apache-2.0 |
| 16 | **octo-version-sync** | 上游版本聚合器，驱动 daemon 远程升级 | 17 | Go | Apache-2.0 |
| 17 | **octo-mail** | 多租户邮件服务器内核（IMAP/JMAP/SMTP/REST） | 3 | Go | Apache-2.0 |
| 18 | **octo-message-export-api** | 异步批量消息导出 API | 3 | Go | Apache-2.0 |
| 19 | **octo-im** | WuKongIM 即时通讯引擎（fork，分布式 IM） | 21 | Go | Apache-2.0 |
| 20 | **octo-spec** | Git-native AI coding 工程标准（规范+任务+项目记忆） | 19 | Shell | Apache-2.0 |
| 21 | **octo-skills** | OpenClaw/Octo AgentSkill 集合 | 20 | Shell | MIT |
| 22 | **multica** | 开源 Managed Agents 平台（coding agents 变队友） | 20 | Go | Other |
| 23 | **codex-channel-octo** | Codex (OpenAI) → Octo IM 桥接网关 | 12 | TypeScript | Apache-2.0 |
| 24 | **cc-channel-octo** | Claude Code → Octo IM 桥接网关 | 31 | TypeScript | Apache-2.0 |
| 25 | **openclaw-channel-octo** | OpenClaw → Octo 频道插件 | 24 | TypeScript | Apache-2.0 |
| 26 | **hermes-channel-octo** | Hermes Agent → Octo 频道插件 | 23 | Python | MIT |
| 27 | **octo-plugin-lib** | 插件库（早期） | 0 | — | — |
| 28 | **octo-docs** | 文档站 (MDX) | 0 | MDX | MIT |
| 29 | **openclaw-octo-mail-plugin** | OpenClaw 邮件插件 | 0 | TypeScript | — |
| 30 | **.github** | 组织级模板和社区文件 | 34 | — | Other |

---

## 三、章鱼系统整体架构还原

```
┌──────────────────────────────────────────────────────────────────┐
│                        接入层 (Clients)                           │
│  octo-web (React/Electron)  │  octo-android  │  octo-ios        │
│  octo-admin (管理台)         │  octo-cli (CLI操控)               │
└──────────────────────────────┬───────────────────────────────────┘
                               │ REST + WebSocket
┌──────────────────────────────▼───────────────────────────────────┐
│                    核心服务层 (octo-server)                        │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────────┐       │
│  │ REST/WS API  │ │ RBAC + Auth  │ │ Lobster Agent 调度  │       │
│  └──────────────┘ └──────────────┘ └────────────────────┘       │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────────┐       │
│  │ IM 控制面    │ │ Adapter 分发  │ │ Webhook/Event      │       │
│  └──────────────┘ └──────────────┘ └────────────────────┘       │
└─────┬──────────┬───────────┬──────────┬──────────┬───────────────┘
      │          │           │          │          │
┌─────▼────┐ ┌──▼─────┐ ┌──▼────┐ ┌───▼───┐ ┌───▼──────────┐
│octo-matter│ │summary │ │speech │ │search │ │octo-mail     │
│(任务/待办)│ │(AI摘要)│ │(ASR)  │ │indexer│ │(邮件服务)    │
└──────────┘ └────────┘ └───────┘ └───────┘ └──────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    Agent 连接层 (Adapters/Channels)                │
│  codex-channel-octo │ cc-channel-octo │ openclaw-channel-octo    │
│  hermes-channel-octo│ octo-adapters (Slack/Discord/飞书/Telegram) │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    Agent 管理平台 (Multica)                        │
│  任务分配 │ Squads 团队路由 │ 自主执行 │ Autopilot │ Skills 复用   │
│  支持: Codex, Claude Code, OpenClaw, Hermes, Cursor, Kimi 等     │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    基础设施层                                      │
│  WuKongIM (分布式IM引擎) │ MySQL │ Redis │ Kafka │ OpenSearch    │
│  MinIO/S3 │ K8s (octo-deployment)                                │
│  octo-lib (协议/加密/存储适配器/事件总线/Worker Pool)             │
│  octo-auth (统一认证) │ octo-version-sync (版本管理)              │
└──────────────────────────────────────────────────────────────────┘
```

---

## 四、与 Enterprise AI OS 架构层映射

| Enterprise AI OS 层 | 对应 OCTO 组件 | 复用度 | 说明 |
|---------------------|---------------|--------|------|
| **L1 接入层** | octo-web, octo-admin, octo-android, octo-ios, octo-cli | **高** | 完整的多端 IM+Agent 交互 UI，支持 Lobster agent 对话渲染、streaming、tool-call 预览 |
| **L2 智能调度层** | **multica** (Squads/Leader 路由, Autopilot, 任务生命周期) + octo-server Lobster 调度 | **高** | Multica 是专门的 managed-agents 平台，支持 Squad Leader 委派、Autopilot 定时触发 |
| **L3 Agent 执行层** | codex-channel-octo, cc-channel-octo, openclaw-channel-octo, hermes-channel-octo | **高** | 已有 Codex 专用桥接器；多 Agent 运行时统一纳入 IM 协作 |
| **L4 上下文与记忆层** | octo-spec (项目记忆), octo-smart-summary (对话摘要), octo-matter (任务时间线) | **中** | 有基础能力，但缺少向量数据库 RAG 和长期记忆服务 |
| **L5 模型服务层** | 各服务内置 OpenAI-compatible LLM client + 多 provider 切换 | **中** | 无独立 Model Gateway 服务 |
| **L6 工具与连接器层** | octo-marketplace (MCP catalog), octo-adapters, octo-skills | **高** | Marketplace 提供完整的 MCP/Skill 生态 |
| **L7 基础设施层** | octo-lib, octo-im/WuKongIM, octo-deployment, octo-search-indexer, octo-auth | **高** | 完整的基础设施栈 |

---

## 五、重点能力分析

### 5.1 Agent 调度/编排层（Multica — 最核心）

- **Squads 机制**：一组 Agent（含 Leader）作为稳定路由单元，Leader 决定谁执行任务
- **Autopilot**：cron/webhook/手动触发 → 自动创建 Issue 并分配给 Agent
- **多 Agent 运行时**：本地 daemon 自动检测已安装的 agent CLI（支持 14+ 种 agent）
- **任务全生命周期**：enqueue → claim → start → complete/fail，WebSocket 实时进度流
- **Reusable Skills**：解决方案自动沉淀为团队可复用技能
- **架构**：Next.js 前端 + Go 后端（Chi + sqlc） + PostgreSQL (pgvector)
- **复用评估**：**可直接复用**

### 5.2 Codex 集成方式（已有现成方案）

**codex-channel-octo** 已实现完整桥接：
- 注册为 Octo Bot → WebSocket 接收消息 → 调用本地 Codex → 回复
- 支持 DM/群组(@mention)/社区主题、多轮对话
- IM ↔ Terminal 互通：bot 对话可在终端 `codex exec resume <threadId>` 恢复
- 沙箱安全模型（默认 read-only，可配 workspace-write）

### 5.3 MCP/Tool 生态

**octo-marketplace** 提供：
- 完整的 MCP 服务器目录管理
- Skill 上架/下载/版本管理
- 远程 MCP 探测
- Space 隔离

### 5.4 核心缺口（需要自建）

| 缺失能力 | 原因 | 建议方案 |
|----------|------|----------|
| Vector Store / RAG Pipeline | OCTO 无 embedding 和向量检索 | 自建：pgvector + embedding service + chunking pipeline |
| 独立 Model Gateway | 各服务分散的 LLM 调用 | 引入 LiteLLM / vLLM + 自建路由层 |
| DAG Workflow Engine | Multica 是 issue-driven，非 DAG | 引入 Temporal 或自建 |
| 长期记忆服务 | octo-spec 是文件级 | 自建 Memory Service（参考 mem0） |
| 会议 Agent（完整版） | octo-speech 只有 ASR | 需自建转录+摘要+决策提取的完整管线 |

---

## 六、推荐复用方案

### 直接复用（fork + 配置部署）

| 组件 | 用途 | 工作量 |
|------|------|--------|
| multica | L2 Agent 调度/编排平台 | 低 |
| octo-server | 核心 API + Agent 路由 + 认证 | 低-中 |
| octo-web | 人机协作 Web UI | 低 |
| octo-admin | 管理后台 | 低 |
| octo-deployment | K8s 部署 | 低 |
| octo-marketplace | MCP/Skill 目录 | 低 |
| codex-channel-octo | Codex 桥接 | 低 |
| octo-auth | 统一认证 | 低 |
| octo-search-indexer | 全文搜索 | 低 |

### 参考设计（取架构思路，二次开发）

| 组件 | 参考价值 | 二次开发量 |
|------|----------|-----------|
| octo-adapters | Adapter 生命周期模型、统一 envelope 设计 | 中 |
| octo-smart-summary | LLM 调用模式、结构化输出 | 中 |
| octo-spec | 项目记忆与规范注入机制 | 中 |
| octo-lib | 事件总线、Worker Pool、加密原语 | 中 |
| octo-speech | 多引擎 ASR 切换 | 中 |

### 需要自研

| 能力 | 建议方案 | 优先级 |
|------|----------|--------|
| RAG Pipeline + Vector Store | pgvector/Milvus + 自建 embedding 服务 | P0 |
| Model Gateway | LiteLLM/OneAPI + 路由/限流/fallback | P0 |
| DAG Workflow Engine | Temporal + 自定义 Activity | P1 |
| 长期记忆服务 | 参考 mem0，自建 Memory API | P1 |
| Meeting Agent 全流程 | ASR + LLM 摘要 + 决策提取 + 行动项分发 | P2 |

---

## 七、风险与注意事项

### 许可证
- 核心组件均为 **Apache-2.0**：商业友好，可私有化部署和修改
- multica 标注 "Other" 许可证，需要确认具体条款
- 少数组件为 MIT

### 上游依赖
- octo-server/web/lib 衍生自 **TangSengDaoDao**（唐僧叨叨）开源 IM
- IM 引擎为 **WuKongIM**（悟空 IM），独立开源，分布式架构
- 底层 IM 能力经过长期验证

### 技术栈
- 后端统一 **Go**，前端统一 **TypeScript/React**
- 存储：MySQL + Redis + MinIO/S3 + OpenSearch + Kafka
- 如果团队主力是 Python/Java，有一定学习成本

### 社区活跃度
- 非常活跃，持续维护
- octo-server/web/cli 为头部项目

---

## 八、总结与建议

OCTO 生态为构建 Enterprise AI OS 提供了**约 70% 的现成能力**：

**已覆盖**：接入层（完整）、调度层（Multica 杀手级）、执行层（Codex 桥接已有）、工具层（MCP 生态完善）、基础设施（K8s+认证+事件齐全）

**核心缺口**：RAG Pipeline、Model Gateway、DAG Workflow、长期记忆

**建议路线**：以 OCTO + Multica 为基础骨架 → 补建 Model Gateway 和 RAG Pipeline → 增强 Meeting Agent → 接入国产模型 → 完成 Enterprise AI OS
