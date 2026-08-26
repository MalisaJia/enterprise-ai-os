# workflow-engine

> **组件定位**：以 DAG 描述、可持久化、可人工审批介入的企业级工作流引擎，负责把多个 Agent 与工具调用编排成一条可靠、可重放、可审计的业务流程。

## 一、为什么需要

OCTO 生态里存在两种"编排"，但都不是 DAG 工作流：

| OCTO 组件 | 它做的编排 | 缺什么 |
|---|---|---|
| `octo-server`（Lobster Agent 调度） | 单个 Agent 会话内的多轮工具调用调度 | 会话级，进程内。会话结束状态即丢失；无法表达"A 完成后并行跑 B/C，都成功再跑 D" |
| `multica` | issue 驱动的 Squads / Autopilot | **事件驱动**而非**图驱动**。流程隐含在 issue 状态流转里，无法预先定义、无法可视化、无法在流程中间插入审批 |

企业级流程的三个硬需求，两者都不满足：

1. **持久化与可恢复**。审批流可能跨越数天。进程重启、Pod 漂移都不能丢失执行进度。
2. **人工审批节点**。"合同审核 Agent 出结论 → 法务确认 → 才允许发出"。审批是流程的一等公民，不是外挂的通知。
3. **确定性重放与审计**。出了问题要能回答"第 3 步用的什么输入、哪个模型、谁批的"。这在合规场景是硬要求。

**本组件补的就是这三点。** 它不取代 Lobster 的会话内调度，而是位于其**上层**：workflow 的一个节点可以是"调 octo-server 起一次 Agent 会话"。

## 二、技术选型

| 维度 | 选择 | 理由 |
|---|---|---|
| 语言 | **Go 1.22+** | 与 `octo-server` 同语言，便于共享 `octo-lib` 与运维工具链。工作流引擎是长驻高并发调度器，goroutine 模型天然合适；GC 停顿与内存占用都优于 Python |
| 设计参考 | **Temporal** | 借鉴其 Workflow / Activity 分离、事件溯源（event sourcing）、确定性重放三大核心概念 |
| 是否直接用 Temporal | **否**（自建） | Temporal 是重量级依赖（需独立集群 + Cassandra/MySQL），且其"工作流即代码"模型要求流程用 Go 编写、编译进二进制。我们需要**声明式 YAML/JSON 定义 + 运行时动态注册**，让业务方不写代码就能配流程。取其思想，不取其实现 |
| 状态存储 | PostgreSQL | 事件日志 + 当前状态快照。已有实例，不引入新组件 |
| 任务分发 | PostgreSQL 行锁 + `FOR UPDATE SKIP LOCKED` | 而非 Kafka。工作流任务需要"可取消、可重试、有优先级、能查单条状态"，消息队列在这些方面都吃力。中等吞吐（<1k tasks/s）下 PG 完全够用 |
| HTTP | `net/http` + `chi` | 轻量。引擎的 API 面很小，不需要全功能框架 |

### 与 Temporal 的关键取舍

Temporal 靠"重放代码"保证确定性，代价是流程必须是编译期代码。我们靠**"事件日志 + 声明式定义的纯函数求值"**保证确定性：节点的输入完全由前驱节点的输出与 DAG 定义决定，重放时不重新执行 Activity，只重新计算调度决策。这让流程可以是数据（存在 DB 里、可热更新），但也意味着**表达能力弱于图灵完备的代码**——只支持顺序、并行、条件分支、循环（有界）。这个限制对企业审批/文档处理类流程是可接受的。

## 三、架构设计

```
                         HTTP API (internal/api)
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
   提交/查询 workflow        审批回调              管理（暂停/取消/重跑）
        │                         │                         │
        ▼                         ▼                         ▼
┌────────────────────────────────────────────────────────────────┐
│                   Scheduler (internal/engine)                  │
│  轮询 ready 队列 → 分发到 worker pool → 处理超时与重试         │
│  · FOR UPDATE SKIP LOCKED 抢任务，天然支持多副本               │
│  · 心跳续约，worker 崩溃后任务自动回到 ready                   │
└────────────────────────────────────────────────────────────────┘
        │                                          ▲
        ▼                                          │ 状态回写 + 事件追加
┌────────────────────────────────────────────────────────────────┐
│                   Executor (internal/engine)                   │
│  取节点 → 求值输入表达式 → 查 Activity Registry → 执行         │
│  → 判定后继（依赖满足 / 条件分支）→ 入 ready 队列              │
└────────────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────────────┐
│              Activity Registry (internal/activity)             │
│  agent.invoke     → octo-server 起 Agent 会话                  │
│  llm.complete     → model-gateway                              │
│  rag.search       → rag-pipeline                               │
│  memory.write     → memory-service                             │
│  http.request     → 任意 HTTP（逃生舱）                        │
│  notify.send      → octo-adapters（飞书/Slack）                │
│  approval.request → 交给 approval 模块，节点进入 waiting        │
│  script.eval      → 沙箱表达式（数据整形，不执行副作用）        │
└────────────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────────────┐
│                  Approval (internal/approval)                  │
│  创建审批单 → 推送到 IM → 等待回调 → 恢复工作流                │
│  支持：单人 / 多人任一 / 多人全部 / 会签比例 / 超时默认动作     │
└────────────────────────────────────────────────────────────────┘
```

### 状态机

节点状态：`pending → ready → running → (succeeded | failed | waiting | skipped | canceled)`

`waiting` 是审批节点特有的状态。它与 `running` 的关键区别是**不占用 worker、不计入执行超时**——审批可能等三天，占着 worker 不放会让整个引擎瞬间饥饿。

工作流状态：`pending → running → (succeeded | failed | canceled | paused)`

### 核心数据表

- `workflow.definitions` — DAG 定义（版本化，运行中实例锁定其创建时的版本）
- `workflow.runs` — 工作流实例
- `workflow.node_runs` — 节点执行记录（含输入/输出快照）
- `workflow.events` — 追加式事件日志（重放与审计的唯一来源）
- `workflow.approvals` — 审批单

**为什么定义要版本化**：一个跑了三天的审批流，中途有人改了 DAG。如果实例跟着新定义走，会出现"已执行的节点在新定义里不存在"这种无法收敛的状态。锁版本是唯一正确做法。

## 四、API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/definitions` | 注册/更新 DAG 定义（返回新版本号） |
| `GET` | `/api/v1/definitions/{name}` | 查询定义（可指定 `?version=`） |
| `POST` | `/api/v1/definitions/{name}/validate` | 只校验不保存：环检测、依赖存在性、Activity 类型合法性 |
| `POST` | `/api/v1/runs` | 启动实例。`{definition, version?, input, idempotency_key?}` |
| `GET` | `/api/v1/runs/{run_id}` | 实例状态 + 各节点状态 |
| `GET` | `/api/v1/runs/{run_id}/events` | 事件日志（审计） |
| `POST` | `/api/v1/runs/{run_id}/pause` | 暂停（已 running 的节点跑完，不再调度新节点） |
| `POST` | `/api/v1/runs/{run_id}/resume` | 恢复 |
| `POST` | `/api/v1/runs/{run_id}/cancel` | 取消 |
| `POST` | `/api/v1/runs/{run_id}/retry` | 从失败节点重跑（不重跑已成功节点） |
| `GET` | `/api/v1/approvals` | 待办审批列表（按审批人过滤） |
| `POST` | `/api/v1/approvals/{id}/decide` | 审批决策 `{decision: approve\|reject, comment}` |
| `GET` | `/healthz` `/readyz` `/metrics` | 探针 |

### DAG 定义示例

```yaml
name: contract-review
version: 3
input_schema:
  contract_url: {type: string, required: true}
nodes:
  - id: extract
    type: agent.invoke
    config:
      agent: doc-extractor
      prompt: "提取合同关键条款：{{ .input.contract_url }}"
    timeout: 300s
    retry: {max_attempts: 3, backoff: exponential}

  - id: risk_check
    type: llm.complete
    depends_on: [extract]
    config:
      model: "auto:reasoning"
      prompt: "评估以下条款的法律风险：{{ .nodes.extract.output.clauses }}"

  - id: precedent
    type: rag.search
    depends_on: [extract]          # 与 risk_check 并行
    config:
      collection: legal-precedents
      query: "{{ .nodes.extract.output.summary }}"

  - id: legal_approval
    type: approval.request
    depends_on: [risk_check, precedent]
    config:
      approvers: ["role:legal"]
      mode: any                    # 任一法务通过即可
      timeout: 48h
      on_timeout: reject           # 超时视为驳回（保守默认）
      channel: feishu

  - id: notify
    type: notify.send
    depends_on: [legal_approval]
    condition: "{{ eq .nodes.legal_approval.output.decision \"approve\" }}"
    config:
      target: "{{ .input.requester }}"
```

## 五、与 OCTO 的集成方式

1. **鉴权**：复用 `octo-auth`。引擎不自建用户体系；审批人标识直接用 OCTO 的 user_id / role。
2. **Agent 执行**：`agent.invoke` 活动通过 `octo-server` 的 REST/WS 接口创建会话并等待结果。这是**最重要的一条集成**——它让 workflow 成为 Lobster Agent 的上层编排者，而不是竞争者。
3. **审批送达**：`approval.request` 通过 `octo-adapters` 把审批卡片推到飞书/Slack，用户在 IM 里点按钮，回调打到本引擎的 `/approvals/{id}/decide`。不自建审批前端。
4. **模型调用**：一律走 `model-gateway`，引擎里没有任何厂商 SDK。
5. **事件外发**：工作流状态变化写 Kafka（`aios.workflow.run.completed` 等），供 `octo-matter` 建对应待办、供数据侧统计。
6. **触发来源**：可由 Kafka 事件触发（如 `octo.file.uploaded` → 自动启动文档处理流），也可由 `octo-server` 的 Skill 主动调用。

## 六、开发指南

```powershell
# 依赖（在仓库根目录）
docker compose up -d postgres redis

# 拉依赖
cd workflow-engine
go mod download

# 配置
Copy-Item config\config.yaml.example config\config.yaml

# 运行（迁移会在启动时自动执行）
$env:PG_DSN="postgres://aios:aios@localhost:5432/aios?sslmode=disable"
go run ./cmd/server

# 测试
go test ./...

# 静态检查
go vet ./...
gofmt -l .
```

默认监听 `:8300`。

## 七、当前状态

**脚手架阶段。** 已定义：目录结构、struct/interface 契约、状态机、DAG 定义格式、API 契约、配置模板。

所有方法体为 `panic("not implemented")` 或返回 `ErrNotImplemented`。

实施顺序建议：

1. `workflow/dag.go` + `node.go` — 定义解析与环检测（纯逻辑，最容易先做对）
2. 数据层与迁移 — 表结构定下来后其余部分才好并行
3. `engine/executor.go` — 单节点执行 + 依赖判定
4. `engine/scheduler.go` — 多副本抢任务与心跳
5. `activity/registry.go` — 先实现 `http.request` 与 `llm.complete`，跑通端到端
6. `approval/handler.go` — 审批全链路
7. `api/handler.go` — 逐步补齐管理接口

> ⚠️ 第 1 步的环检测与第 4 步的幂等性是本组件最容易埋坑的两处，务必配测试。
