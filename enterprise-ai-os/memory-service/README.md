# memory-service

> **组件定位**：为 Agent 与人提供跨会话、跨时间的长期记忆——个人偏好、团队约定、组织知识三层分治，可写入、可召回、可整合、可遗忘。

## 一、为什么需要

OCTO 生态里最接近"记忆"的是 `octo-spec`，但它的定位是**文件级项目记忆**（把项目约定写成 spec 文件供 Agent 读取）。这解决的是"Agent 需要知道项目规范"，不是"系统记得你是谁"。

具体缺什么：

| 需求 | OCTO 现状 | 缺口 |
|---|---|---|
| "我上次说过我们用 pnpm 不用 npm" | 会话结束即遗忘 | 无跨会话的用户偏好存储 |
| "这个团队的代码评审规则是 X" | `octo-spec` 可写文件，但需人工维护 | 无从对话中**自动抽取**并沉淀的机制 |
| "组织内对'交付标准'的定义" | 无 | 无组织级知识的结构化沉淀 |
| "这条记忆和三个月前那条矛盾了" | 无 | 无冲突检测与整合 |
| "忘掉我上次说的那个偏好" | 无 | 无可控遗忘 |

**记忆与 RAG 是两件不同的事**，这一点必须说清楚，否则很容易被质疑"为什么不用 rag-pipeline 就够了"：

| | rag-pipeline（检索） | memory-service（记忆） |
|---|---|---|
| 数据来源 | 既有文档，被动导入 | 交互过程，主动抽取 |
| 粒度 | 文档分块（几百 token） | 单条陈述（一句话） |
| 生命周期 | 与源文档同生共死 | 独立演化：会被更新、合并、衰减、遗忘 |
| 冲突处理 | 无（都返回，让 LLM 判断） | **必须处理**——矛盾的记忆会让 Agent 行为不一致 |
| 写入方 | 数据管道 | Agent 与用户 |

两者是互补的：memory-service 复用 model-gateway 的 embedding 能力，但**不复用 rag-pipeline 的集合**——记忆需要独立的生命周期管理。

## 二、技术选型

| 维度 | 选择 | 理由 |
|---|---|---|
| 语言/框架 | **Python + FastAPI** | 记忆抽取与整合大量依赖 LLM 调用与 prompt 工程，Python 生态最顺手 |
| 向量存储 | **pgvector** | 记忆总量远小于文档（每用户数千条量级），pgvector 完全够用。选它的决定性理由是**记忆需要频繁 UPDATE/DELETE**（整合、衰减、遗忘），关系库的事务与索引更新远比专用向量库可靠 |
| 图存储 | **Neo4j（可选）** | 用于实体关系（"张三 属于 法务组"、"项目A 依赖 项目B"）。默认关闭：多数场景纯向量检索已够用，引入图库的运维成本需要有明确收益才值得 |
| 设计参考 | **mem0** | 借鉴其"抽取 → 判定操作（ADD/UPDATE/DELETE/NOOP）→ 执行"的写入流水线，以及记忆的原子化表述原则 |
| LLM 调用 | 一律经 **model-gateway** | 抽取与整合都要调 LLM，不直连厂商 |
| Embedding | **bge-m3（1024 维）** 经 model-gateway | 与 rag-pipeline 保持同一模型同一维度，便于将来做跨库联合召回 |

### 为什么参考 mem0 而不直接用

mem0 的核心思想（LLM 抽取 + LLM 判定操作）非常正确，我们直接采用。但它是单体库、无多租户隔离、无组织/团队分层、无审计。企业场景这三点都是硬要求，改造量大于重写。

## 三、架构设计

```
                       ┌─────────────────────────┐
                       │   API (src/api)         │
                       │  write / search / forget│
                       └───────────┬─────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
┌───────────────┐        ┌─────────────────┐        ┌─────────────────┐
│  personal.py  │        │     team.py     │        │      org.py     │
│  个人记忆      │        │   团队记忆       │        │   组织记忆       │
│               │        │                 │        │                 │
│ 偏好/习惯/     │        │ 约定/流程/       │        │ 制度/标准/       │
│ 私人上下文     │        │ 项目上下文       │        │ 业务定义         │
│               │        │                 │        │                 │
│ 写：自动抽取   │        │ 写：抽取+人工确认 │        │ 写：仅人工/审批   │
│ 读：仅本人     │        │ 读：团队成员      │        │ 读：全员          │
└───────┬───────┘        └────────┬────────┘        └────────┬────────┘
        │                         │                          │
        └─────────────┬───────────┴──────────────────────────┘
                      ▼
        ┌──────────────────────────────────────┐
        │   consolidation/merger.py            │
        │   ① 冲突检测（新旧记忆语义矛盾）      │
        │   ② 操作判定 ADD/UPDATE/DELETE/NOOP  │
        │   ③ 去重合并（同义表述归一）          │
        │   ④ 衰减与遗忘（时间/使用频次）       │
        └──────────────┬───────────────────────┘
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
┌──────────────────┐          ┌──────────────────┐
│ storage/vector.py│          │ storage/graph.py │
│    pgvector      │          │  Neo4j（可选）    │
│  语义检索 + CRUD  │          │  实体关系遍历     │
└──────────────────┘          └──────────────────┘
```

### 三层记忆的作用域设计（最关键的一处设计）

三层不是简单的"标签不同"，而是**权限、写入门槛、召回优先级都不同**：

| | 作用域键 | 写入方式 | 可见性 | 召回时的权重 |
|---|---|---|---|---|
| personal | `user_id` | Agent 自动抽取 | 仅本人 | 最高（最贴合当前用户） |
| team | `space_id` | 抽取后需成员确认 | 团队成员 | 中 |
| org | `tenant_id` | 仅管理员写入或走审批流 | 全员 | 最低但**不可被个人记忆否决** |

**为什么写入门槛递增**：个人记忆写错只影响一个人，改起来也容易。组织记忆写错会影响所有 Agent 的行为，且很难发现"是记忆导致的偏差"。因此组织层必须人工把关——这是把 LLM 抽取的不确定性限制在可控范围内的手段。

**为什么召回权重是这个顺序，但组织记忆不可被否决**：个人偏好应当优先（"我习惯用 pnpm"），但组织的硬约束不能被个人偏好覆盖（"生产发布必须双人复核"不能因为某人说"我一个人就够了"而失效）。实现上：org 层带 `mandatory` 标记的记忆在检索结果中强制保留且标注不可覆盖。

### 记忆的数据模型

```
memory.memories
  id            uuid
  tenant_id     text        必填，隔离边界
  scope         text        personal | team | org
  scope_key     text        user_id / space_id / tenant_id
  content       text        原子化的单条陈述
  embedding     vector(1024)
  category      text        preference | fact | rule | relation | event
  mandatory     bool        仅 org 层有意义，标记不可覆盖的硬约束
  confidence    float       抽取置信度
  source        jsonb       来源：会话 id、消息 id、抽取模型
  strength      float       记忆强度，随时间衰减、随命中回升
  version       int         每次 UPDATE 递增
  superseded_by uuid        被哪条记忆取代（软删除，保留可追溯性）
  created_at / updated_at / last_accessed_at
```

**为什么 content 必须是原子化的单条陈述**：一条记忆里塞三件事，将来其中一件过时了就无法单独更新——只能整条删掉，连带丢掉仍然有效的两件。这是 mem0 的核心洞见，也是记忆与文档分块最本质的区别。

**为什么用 superseded_by 软删除而非物理删除**：需要能回答"Agent 为什么以前那样做"。审计与调试都依赖历史。

## 四、API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/memories/extract` | 从对话片段抽取并写入记忆（主入口，Agent 调用） |
| `POST` | `/api/v1/memories` | 直接写入一条明确的记忆（人工或规则写入） |
| `POST` | `/api/v1/memories/search` | 语义检索。入参含 `scopes` 决定检索哪几层 |
| `GET` | `/api/v1/memories/{id}` | 单条详情（含版本历史） |
| `PATCH` | `/api/v1/memories/{id}` | 修改内容（产生新版本） |
| `DELETE` | `/api/v1/memories/{id}` | 遗忘（软删除） |
| `POST` | `/api/v1/memories/forget` | 按条件批量遗忘（"忘掉关于项目X的所有记忆"） |
| `GET` | `/api/v1/memories` | 列表，供"我的记忆"管理界面使用 |
| `POST` | `/api/v1/context` | **组合接口**：给定当前会话，返回应注入 prompt 的记忆集合 |
| `POST` | `/api/v1/memories/{id}/confirm` | 确认待审记忆（team 层写入流程） |
| `GET` | `/api/v1/memories/pending` | 待确认记忆列表 |
| `POST` | `/api/v1/consolidate` | 手动触发整合（正常由定时任务执行） |
| `GET` | `/api/v1/graph/entities/{id}` | 实体关系查询（图存储启用时） |
| `GET` | `/healthz` `/readyz` `/metrics` | 探针 |

### `/api/v1/context` 为什么值得单独设计

Agent 侧最常见的需求不是"检索记忆"，而是"给我一段可以直接放进 system prompt 的记忆摘要"。若只提供 search，每个 Agent 都要自己实现"检索 → 去重 → 按层排序 → 截断到 token 预算 → 格式化"这套逻辑，必然各写各的、口径不一。

因此提供组合接口，入参 `{query, user_id, space_id, token_budget}`，返回已排好序、已截断、已格式化的文本块 + 结构化明细。**token_budget 是必填的**——不设预算会让记忆挤占对话历史的空间，反而降低效果。

## 五、与 OCTO 的集成方式

1. **鉴权**：复用 `octo-auth`。`user_id` / `space_id` / `tenant_id` 全部沿用 OCTO 的标识体系——记忆的作用域键直接就是这些 id，若两套系统标识不一致，记忆就会挂错人。
2. **自动抽取的触发**：消费 Kafka 的 `octo.message.created`。这是记忆能"自动积累"而非"需要显式调用"的关键。消费策略是**会话结束后批量抽取**而非逐条抽取——单条消息里的信息往往不完整（"用 pnpm" 需要上文才知道在说什么），且逐条抽取的 LLM 成本高一个数量级。
3. **供 Agent 使用**：以 `octo-marketplace` 的 MCP Server 形式暴露 `memory.search` / `memory.write` 工具，让 Lobster Agent 与 codex/cc/openclaw 各 channel 都能直接用，无需各自集成。
4. **与 `octo-spec` 的分工**：`octo-spec` 管**文件形态的项目规范**（人工维护、进版本库、Code Review 可见），memory-service 管**对话中沉淀的隐性知识**。可以互转：memory 中被反复命中的 team 层规则，应当提示用户"要不要固化进 octo-spec"。
5. **模型调用**：抽取、冲突判定、摘要一律经 `model-gateway`。抽取用 `auto:extraction`（廉价快速），冲突判定用 `auto:reasoning`（需要准确）。
6. **事件外发**：写入/遗忘发 `aios.memory.written` / `aios.memory.forgotten` 到 Kafka，供审计与数据侧使用。

## 六、开发指南

```powershell
# 依赖
docker compose up -d postgres redis
# 图存储可选
docker compose --profile graph up -d neo4j

# 环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 配置
Copy-Item config\config.yaml.example config\config.yaml

# 数据库准备（需要 pgvector 扩展）
# CREATE EXTENSION IF NOT EXISTS vector;
# CREATE SCHEMA IF NOT EXISTS memory;

# 运行
$env:MEMORY_CONFIG_PATH="config/config.yaml"
uvicorn src.main:app --reload --port 8400

# 测试
pytest tests -v
```

默认监听 `:8400`，交互文档在 `http://localhost:8400/docs`。

## 七、当前状态

**脚手架阶段。** 已定义：三层记忆的作用域与权限模型、数据模型与 DDL、写入流水线（抽取→判定→执行）、整合与衰减策略、API 契约、配置模板。

所有实现体为 `raise NotImplementedError` 或 `HTTPException(501)`，关键决策已写在 docstring 里。

实施顺序建议：

1. `storage/vector.py` — 表结构与 CRUD（其余全部依赖它）
2. `memory/personal.py` — 单层跑通，验证作用域隔离
3. `consolidation/merger.py` 的 ADD/NOOP 路径 — 先让写入可用
4. `api/routes.py` 的 search + context — 让 Agent 能读到记忆，产生价值
5. `consolidation/merger.py` 的 UPDATE/DELETE 路径 — 冲突处理
6. `memory/team.py` + `org.py` — 分层与确认流程
7. `storage/graph.py` — 图存储（最后做，收益需验证）

> ⚠️ 第 5 步（冲突判定）是本组件最难做对的部分：判定过于激进会误删有效记忆，过于保守会积累矛盾。建议先跑影子模式（只记录判定结果不实际执行），用真实数据评估准确率后再开启。
