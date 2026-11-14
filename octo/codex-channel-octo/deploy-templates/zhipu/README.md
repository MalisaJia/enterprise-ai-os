# GLM-5.2 部署模板(codex-channel-octo)

本目录提供把桥接器接到 **GLM-5.2** 所需的两个配置文件模板。
模板已按实测调整:实际接入方式**不是**智谱官方端点,而是**自建
new-api 网关** `https://ai.wuguhub.com/v1`——已实测其 `POST /v1/responses`
返回标准 Responses 协议响应(`status = "completed"`),**协议兼容 ✅**,可直接供 Codex 使用。
网关模型列表只有 **`glm-5.2`**(推理模型,返回 `reasoning_tokens`)。

| 文件 | 作用 |
| --- | --- |
| `models.json` | GLM 模型元数据目录,仅含 `glm-5.2` 一个模型(上下文 204800,推理档位低/高/最大) |
| `config.toml` | Codex provider 配置:指向自建网关 `https://ai.wuguhub.com/v1`,`wire_api = "responses"` |

## 一、拷到哪里:每个 bot 的 codex-home 目录

把 `models.json` 和 `config.toml` **两个文件一起**拷到**每个 bot** 的
CODEX_HOME 目录,默认路径为:

```
~/.codex-channel-octo/<botId>/codex-home/
```

(Windows 下即 `C:\Users\<用户名>\.codex-channel-octo\<botId>\codex-home\`。)

核对源码后的依据:

- 每个 bot 的 CODEX_HOME 由 `resolveBotConfigs()` 派生为
  `<baseDir>/<botId>/codex-home`(`src/config.ts` 第 536-539 行的
  `botCodexHome`),未显式配置时在 `src/config.ts` 第 590 行作为
  `sdk.codexHome` 的默认值;`baseDir` 默认就是 `~/.codex-channel-octo`。
- 若在配置里显式设置了 `sdk.codexHome`(如共享个人 `~/.codex`),则拷到该目录。

## 二、桥接器把该目录传给 SDK 的机制(代码核对结论)

桥接器**不是**通过 `@openai/codex-sdk` 构造函数的专门 `codexHome` 选项,
而是**通过子进程环境变量 `CODEX_HOME`** 传递:

1. 配置键名:`sdk.codexHome`(per-bot config.json,声明见
   `src/config.ts` 第 109-117 行)。
2. `src/agent-bridge.ts` 第 36-52 行 `buildCodexEnv()` 在继承的进程环境
   之上叠加 `out.CODEX_HOME = sdk.codexHome`(第 50 行)。
3. `src/agent-bridge.ts` 第 338-343 行,该环境通过
   `new Codex({ env })` 传给 SDK,由 SDK 启动的 Codex 子进程继承;
   Codex 据此从该目录读取 `config.toml` / `models.json`。

## 三、替换 API Key(必做)

把 `config.toml` 中的

```
experimental_bearer_token = "REPLACE_WITH_YOUR_ZHIPU_CODING_PLAN_KEY"
```

替换为你在网关上创建的 API Key(由部署方自行生成,**部署时替换**,
模板中仅为占位符)。注意:

- **不同网关/套餐的 Key 不通用**——请使用与 `https://ai.wuguhub.com/v1`
  该网关配套创建的 Key,不要混用智谱官方平台或团队的 Key。
- 每个 bot 用自己的目录、各自替换,不要把 Key 提交进仓库。

## 四、模型说明与省钱建议

- 默认 `model = "glm-5.2"`,也是网关模型列表中**唯一**的模型(推理模型,
  响应中会返回 `reasoning_tokens`,按网关计费时请留意推理开销)。
- 如需更换网关上的其它模型,同步改 `config.toml` 的 `model` 与 `models.json`
  里的模型对象即可。
- 前提:桥接器的 `sdk.model` 未设置——桥接器只在配置了 `sdk.model` 时
  才向 SDK 传 model 覆盖(`src/agent-bridge.ts` 第 205 行),未设置时以
  `config.toml` 的 `model` 为准。

## 五、octo-smart-summary 同网关复用提示

- 同一网关的 `/v1/chat/completions` 端点同样可用,可直接给 **octo-smart-summary** 用:
  `LLM_API_URL=https://ai.wuguhub.com/v1`、`LLM_MODEL=glm-5.2`(Key 同上,部署时填入)。
- 同一套 Key/网关地址可直接复用,无需额外配置。

## 六、两点注意事项

1. **`model_catalog_json = "models.json"`(相对路径)**:Codex 将相对路径
   按**包含 `config.toml` 的目录**(即本目录拷入的 codex-home)解析,两个
   文件同目录拷贝即可生效;智谱官方示例写的是 `~/.codex/models.json`
   (支持 `~` 展开)。若某环境下解析失败,可改写为指向该文件的绝对路径。
2. **推理强度覆盖**:桥接器每次都会把 `sdk.modelReasoningEffort`
   (默认 `"medium"`)通过 ThreadOptions 传给 SDK(`src/agent-bridge.ts`
   第 180-182、201 行),会**覆盖** `config.toml` 里的
   `model_reasoning_effort = "max"`。若想保留官方推荐的 max 强度,请在该
   bot 的 config.json 中设置 `"sdk": { "modelReasoningEffort": "xhigh" }`
   (允许值:`minimal` / `low` / `medium` / `high` / `xhigh`)。
