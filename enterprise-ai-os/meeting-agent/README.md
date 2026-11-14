# meeting-agent

> 把一场会议变成可执行的结论：转录 → 摘要 → 决策与 Action Item 抽取 → 分发到 IM 与任务系统。

## 1. 组件定位

一个端到端的会议 Agent。输入是音频/视频流或录音文件，输出是**已分发到责任人**的结构化会议成果——不是一份需要有人再读一遍的会议纪要。

## 2. 为什么需要

OCTO 生态里已有两块相关能力，但缺了把它们串成闭环的那一环：

| OCTO 组件 | 已提供 | 缺什么 |
|---|---|---|
| `octo-speech` | 多引擎 ASR 抽象、流式转写 | 只到"文字"为止。没有说话人归属、没有会议语义理解 |
| `octo-smart-summary` | 通用文本摘要 | 面向聊天记录，不理解会议结构（议题/决策/待办/分歧） |
| `octo-matter` | 任务与待办的存储与流转 | 需要有人手工把纪要里的待办录进去 |
| `octo-adapters` | 飞书/Slack/Discord 消息收发 | 需要有人决定发什么给谁 |

真实痛点不在"生成纪要"——大模型写摘要早就不是难题。痛点在于**纪要写完之后什么都没发生**：待办没人录进系统，决策没人通知到未参会的相关方，下次开会又把上次的结论重议一遍。

本组件补的正是最后一公里：从非结构化的会议音频，得到带责任人、带截止时间、已进入任务系统的 Action Item。

它同时也是整个 Enterprise AI OS 的**垂直场景验证**——把 rag-pipeline、model-gateway、memory-service、workflow-engine 四个自研组件串起来跑通一个真实业务闭环，验证这套底座是否真的好用。这是它被排在 P2 的原因：价值高，但依赖前面几个组件先稳定。

## 3. 技术选型

| 关注点 | 选择 | 理由 |
|---|---|---|
| 语言/框架 | Python + FastAPI | 与其他 Python 组件一致；ASR 与 LLM 生态都在 Python |
| ASR | Whisper / SenseVoice，经 `octo-speech` 抽象 | 中文会议 SenseVoice 的标点与数字规整明显更好；Whisper 的多语种与长音频更稳。两者都要，靠配置切 |
| 说话人分离 | pyannote / 引擎自带 diarization | **不做说话人分离，Action Item 就没有责任人**，整个组件的价值折损一半 |
| 摘要与抽取 | 经 `model-gateway`，强模型 | 抽取决策需要理解"讨论了但没定"与"定了"的区别，廉价模型分不清 |
| 长会处理 | 分段抽取 + 归并，不做单次长上下文 | 两小时会议约 3 万字，塞进单次调用会让中段信息被严重稀释 |
| 会议知识 | `rag-pipeline`（历史纪要）+ `memory-service`（团队约定） | 让 Agent 知道"上次这个议题的结论是什么" |
| 审批分发 | `workflow-engine` | 敏感决策的对外通告需要走审批 |
| 任务落地 | `octo-matter` | 不自建任务表 |

## 4. 架构设计

```
                       ┌─────────────────────────────────────┐
  音频流 / 录音文件 ──→ │        transcription/asr.py         │
  （会议中 / 会后）      │  octo-speech 对接、流式与批量转写    │
                       │  说话人分离、时间戳对齐、术语纠错     │
                       └──────────────┬──────────────────────┘
                                      │ Transcript（带说话人与时间戳的分段）
                       ┌──────────────▼──────────────────────┐
                       │       summarization/summarizer.py   │
                       │  分段摘要 → 归并 → 议题切分          │
                       │  产出：概要 / 议题列表 / 分歧点       │
                       └──────────────┬──────────────────────┘
                                      │ MeetingSummary
                       ┌──────────────▼──────────────────────┐
                       │            extraction/              │
                       │  decisions.py  决策：结论 + 依据 +   │
                       │                 决策人 + 影响范围    │
                       │  actions.py    待办：任务 + 责任人 + │
                       │                 截止时间 + 验收标准  │
                       └──────────────┬──────────────────────┘
                                      │ Decision[] / ActionItem[]
                       ┌──────────────▼──────────────────────┐
                       │      distribution/dispatcher.py     │
                       │  → octo-matter    创建任务           │
                       │  → octo-adapters  推送纪要与 @责任人  │
                       │  → rag-pipeline   纪要入库供检索      │
                       │  → memory-service 团队约定入记忆      │
                       │  → workflow-engine 敏感决策走审批     │
                       └─────────────────────────────────────┘
```

### 为什么抽取要拆成 decisions 和 actions 两个模块

它们的判定标准完全不同，混在一个 prompt 里两边都会变差：

- **决策**关心"定了什么、为什么、谁定的"。最难的是区分**已定**与**只是讨论过**——"我觉得应该用方案 A" 不是决策，"那就按方案 A 做" 才是。抽错的代价是记录了一个从未达成的共识，下次开会时争议会更大。
- **待办**关心"谁在什么时间前做完什么"。最难的是**责任人归属**与**隐式截止时间**（"下周之前"要能算成具体日期）。抽错的代价是任务派给了错误的人。

两者的召回/精确取舍也相反：待办宁可多抽（漏一个任务的代价大，多一个可以删掉），决策宁可少抽（错误的决策记录会误导后续所有讨论）。放在一个模块里没法分别配置阈值。

### 流式与批量两条路径

| | 流式（会议中） | 批量（会后） |
|---|---|---|
| 触发 | 音频流接入 | 录音文件上传 |
| 转写 | 增量，边说边转 | 整段，可用更大模型 |
| 摘要 | 滚动摘要，供实时查看 | 完整摘要 |
| 抽取 | 不做——会议中的表述随时被推翻 | 做 |
| 分发 | 不做 | 做 |

**会议进行中绝不抽取和分发**。这条很重要：会议前半段说"这个交给张三"，后半段可能改成李四。若边开边派任务，张三会收到一个马上就作废的任务，而系统里已经留下了记录。实时能力只提供"看到目前聊了什么"，落地动作一律等会议结束。

## 5. API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/meetings` | 创建会议记录（可先建后传音频） |
| GET | `/api/v1/meetings/{id}` | 会议详情（含处理状态机） |
| POST | `/api/v1/meetings/{id}/audio` | 上传录音，触发批量流水线，返回 `job_id` |
| POST | `/api/v1/meetings/{id}/stream` | WebSocket，实时音频流入与增量转写流出 |
| GET | `/api/v1/meetings/{id}/transcript` | 转录文本（带说话人与时间戳） |
| POST | `/api/v1/meetings/{id}/transcript/speakers` | 修正说话人映射（把 SPEAKER_00 认成张三） |
| GET | `/api/v1/meetings/{id}/summary` | 摘要与议题 |
| GET | `/api/v1/meetings/{id}/decisions` | 决策列表 |
| GET | `/api/v1/meetings/{id}/actions` | Action Item 列表 |
| PATCH | `/api/v1/meetings/{id}/actions/{action_id}` | 人工修正待办（责任人/截止时间） |
| POST | `/api/v1/meetings/{id}/dispatch` | 触发分发（默认需人工确认后才调） |
| GET | `/api/v1/meetings/{id}/dispatch` | 分发结果与失败重试状态 |
| POST | `/api/v1/meetings/{id}/reprocess` | 用新 prompt/模型重跑，保留历史版本 |
| GET | `/api/v1/jobs/{job_id}` | 异步任务进度 |

两处设计取舍：

**分发默认需要人工确认**（`dispatch.require_confirmation: true`）。抽取准确率再高也不是 100%，而分发是有外部副作用的动作——任务已经派出去、消息已经发到群里，撤回的社交成本比"多点一下确认"高得多。等准确率在真实数据上验证达标，再按会议类型逐步放开自动分发。

**提供 `/reprocess` 且保留版本**。prompt 与模型会持续迭代，需要能拿历史会议重跑来对比效果。覆盖式重跑会毁掉基线，所以每次重跑产生新版本，旧版本保留。

## 6. 与 OCTO 的集成方式

| 方向 | 对接方式 |
|---|---|
| ASR | 调 `octo-speech` 的转写接口，不直连 Whisper。引擎选型、模型加载、GPU 调度都是它的职责 |
| 会议触发 | 消费 Kafka `octo.meeting.ended`；或从飞书日历 webhook 触发 |
| 任务落地 | 调 `octo-matter` 创建 todo，`external_ref` 回填会议 id 与 action id，保证可追溯 |
| 纪要推送 | 经 `octo-adapters` 发到飞书/Slack，@ 到责任人 |
| 纪要入库 | 推 Kafka `aios.document.ingest`，由 `rag-pipeline` 摄入，下次会议可检索"上次结论" |
| 团队约定 | 会议中确立的规范提议给 `memory-service` 的 team 层（进待确认队列，不直接生效） |
| 敏感决策 | 调 `workflow-engine` 起审批流，通过后才对外通告 |
| 鉴权 | `octo-auth`，会议的可见范围继承所在 space 的成员关系 |
| 音频存储 | MinIO / S3，只存 key 不存字节流 |

一条边界：**本组件不做会议室预定、日历管理、参会人管理**。这些是 IM/日历系统的职责，做了就是重复建设且永远比不上原生体验。本组件只消费"会议发生了"这个事实。

## 7. 开发指南

```bash
# 起依赖（PG / Redis / Kafka / MinIO）
docker compose up -d postgres redis kafka minio

cp config/config.yaml.example config/config.yaml
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 本服务需要 model-gateway 与 octo-speech 可达
export MODEL_GATEWAY_URL=http://localhost:8200/v1
export OCTO_SPEECH_URL=http://localhost:8600
export AUTH_ENABLED=false        # 本地跳过 octo-auth

uvicorn src.main:app --reload --port 8500
# http://localhost:8500/docs
```

无 `octo-speech` 时的本地开发：把 `transcription.engine` 设为 `mock`，直接喂一份现成的转录 JSON。摘要与抽取的调优不需要真跑 ASR，这样迭代快得多。

## 8. 当前状态

**脚手架阶段。** 目录结构、接口契约、数据模型与设计约束已定，方法体均为 `NotImplementedError` / `501`。

建议实现顺序：

1. `transcription/asr.py` 的 mock 引擎 + `Transcript` 数据模型 —— 后面全部依赖这个数据结构，先把它定死
2. `summarization/summarizer.py` 的分段归并 —— 长会处理是绕不开的
3. `extraction/actions.py` —— 待办比决策容易做对，先拿它验证抽取链路
4. `extraction/decisions.py` —— 难点是"已定 vs 讨论过"的判别，需要真实会议数据调 prompt
5. `distribution/dispatcher.py` —— 先只对接 `octo-matter`，跑通一条最短闭环
6. `api/routes.py` 的批量路径
7. 真实 ASR 引擎对接与说话人分离
8. 流式路径（WebSocket）—— 最后做，它只是体验加分项

第 3、4 步之前**必须先攒一批标注好的真实会议数据**。没有基线就调不出可靠的抽取 prompt，只能凭感觉改，改完不知道是变好还是变坏。这一步的准备工作往往比写代码耗时更久，要提前排。
