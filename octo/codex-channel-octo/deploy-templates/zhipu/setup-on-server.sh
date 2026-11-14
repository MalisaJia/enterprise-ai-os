#!/usr/bin/env bash
# =============================================================================
# codex-channel-octo —— 测试服务器一键配置脚本(智谱 GLM-5.2 模板)
#
# 用途:在已构建完成的桥接器服务器上(/opt/codex-channel-octo,dist/ 已生成)
#       写好两层配置、铺好 codex-home 模板文件,并打印守护进程操作命令。
#
# 用法:
#   BOT_TOKEN='bf_xxx' LLM_KEY='xxx' bash setup-on-server.sh
#
# 配置 schema 依据(勿凭猜测修改):
#   - 仓库根目录 config.example.json      —— 全局配置权威示例
#   - 仓库根目录 config.bot.example.json  —— 每 bot 配置权威示例
#   - src/config.ts                       —— 加载/校验/派生逻辑
#   - src/cli.ts                          —— 守护进程 start/stop/status 命令
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# 0. 路径与常量
# -----------------------------------------------------------------------------
# 脚本所在目录(即本模板目录,内含 models.json / config.toml)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 全局配置固定路径:src/config.ts 的 DEFAULT_CONFIG_PATH = ~/.codex-channel-octo/config.json
BASE_DIR="$HOME/.codex-channel-octo"
GLOBAL_CONFIG="$BASE_DIR/config.json"

# 本脚本部署的 bot id 为 codex1(将出现在全局配置 bots[] 中)
BOT_ID="codex1"
BOT_DIR="$BASE_DIR/$BOT_ID"
BOT_CONFIG="$BOT_DIR/config.json"
# codexHome 派生规则:未显式配置时默认 <baseDir>/<botId>/codex-home(src/config.ts resolveBotConfigs)
CODEX_HOME_DIR="$BOT_DIR/codex-home"

# 桥接器安装目录(测试服务器上已构建完成)
APP_DIR="/opt/codex-channel-octo"

# -----------------------------------------------------------------------------
# 1. 读取并校验环境变量:BOT_TOKEN(必须 bf_ 开头)与 LLM_KEY
# -----------------------------------------------------------------------------
BOT_TOKEN="${BOT_TOKEN:-}"
LLM_KEY="${LLM_KEY:-}"

if [ -z "$BOT_TOKEN" ]; then
  echo "错误:未设置环境变量 BOT_TOKEN(Octo 机器人令牌,以 bf_ 开头)。" >&2
  echo "用法:BOT_TOKEN='bf_xxx' LLM_KEY='xxx' bash $0" >&2
  exit 1
fi
case "$BOT_TOKEN" in
  bf_*) ;;
  *)
    echo "错误:BOT_TOKEN 必须以 'bf_' 开头(当前值前缀不符)。" >&2
    exit 1
    ;;
esac

if [ -z "$LLM_KEY" ]; then
  echo "错误:未设置环境变量 LLM_KEY(自建 new-api 网关的 API Key)。" >&2
  exit 1
fi

echo "[1/6] 环境变量校验通过:BOT_TOKEN(bf_ 开头)、LLM_KEY 均已提供。"

# -----------------------------------------------------------------------------
# 2. 创建 bot 子树目录(含 codex-home;data/workspace/memory 由桥接器自动创建)
# -----------------------------------------------------------------------------
mkdir -p "$CODEX_HOME_DIR"
echo "[2/6] 已创建目录:$CODEX_HOME_DIR"

# -----------------------------------------------------------------------------
# 3. 写全局配置 ~/.codex-channel-octo/config.json
#    schema 要点(来自 config.example.json / src/config.ts):
#      - apiUrl 必填,且受 SSRF 校验:必须 https:// 或 http://localhost
#      - bots[] 元素为对象形态 { "id": "..." },id 决定子树目录 <baseDir>/<id>/
#      - 全局配置绝不存放 botToken(令牌在每 bot 的 <id>/config.json 里)
#      - 以 "_" 开头的键(如 _comment)会被加载器剥离,可安全用作注释
#      - 其余键取 example 的最小合理默认(只读沙箱、限流、上下文)
# -----------------------------------------------------------------------------
cat > "$GLOBAL_CONFIG" <<'EOF'
{
  "_comment": "GLOBAL 配置(由 setup-on-server.sh 生成)。共享默认值 + bots 列表;不存放 botToken —— 每个 bot 的令牌在 ~/.codex-channel-octo/<id>/config.json。以 _ 开头的键会被加载器自动剥离。",
  "apiUrl": "http://localhost:28080",
  "bots": [
    { "id": "codex1" }
  ],
  "sdk": {
    "sandboxMode": "read-only",
    "allowWorkspaceWrite": false,
    "approvalPolicy": "never",
    "modelReasoningEffort": "medium",
    "networkAccessEnabled": false,
    "webSearchEnabled": false
  },
  "rateLimit": {
    "maxPerMinute": 5
  },
  "context": {
    "maxContextChars": 6000,
    "historyLimit": 40
  },
  "mentionFreeGroups": []
}
EOF
echo "[3/6] 已写入全局配置:$GLOBAL_CONFIG"

# -----------------------------------------------------------------------------
# 4. 写每 bot 配置 ~/.codex-channel-octo/codex1/config.json
#    schema 要点(来自 config.bot.example.json / src/config.ts):
#      - botToken 必填,格式为 bf_ 开头(此处由环境变量 $BOT_TOKEN 注入)
#      - 本文件是最高优先级层,可覆写全局共享字段与 bots[] 内联字段
#      - sdk 段显式保持默认只读沙箱:不打开写权限
#        (开写需同时 allowWorkspaceWrite=true 且 sandboxMode='workspace-write')
#      - 不设置 sdk.codexHome:保持默认派生目录 ./codex-home,与第 5 步拷贝位置一致
#      - token 属敏感信息,写完后 chmod 600(加载器也会对 0o077 权限告警)
# -----------------------------------------------------------------------------
cat > "$BOT_CONFIG" <<EOF
{
  "_comment": "PER-BOT 配置(由 setup-on-server.sh 生成)。最高优先级层,覆写全局配置。显式保持只读沙箱:首次运行只验证问答;如需让 bot 改文件,须同时设 allowWorkspaceWrite=true 与 sandboxMode='workspace-write' 双开关。",
  "botToken": "$BOT_TOKEN",
  "sdk": {
    "sandboxMode": "read-only",
    "allowWorkspaceWrite": false
  }
}
EOF
chmod 600 "$BOT_CONFIG" "$GLOBAL_CONFIG"
echo "[4/6] 已写入每 bot 配置:$BOT_CONFIG(已 chmod 600)"

# -----------------------------------------------------------------------------
# 5. 铺 codex-home 模板:把本目录的 models.json / config.toml 拷入,
#    并用 sed 把占位符替换为真实网关 Key
# -----------------------------------------------------------------------------
cp "$SCRIPT_DIR/models.json" "$CODEX_HOME_DIR/models.json"
sed "s/REPLACE_WITH_YOUR_ZHIPU_CODING_PLAN_KEY/$LLM_KEY/" \
  "$SCRIPT_DIR/config.toml" > "$CODEX_HOME_DIR/config.toml"
chmod 600 "$CODEX_HOME_DIR/config.toml"

# 校验替换是否真正生效(占位符不得残留)
if grep -q 'REPLACE_WITH_YOUR_ZHIPU_CODING_PLAN_KEY' "$CODEX_HOME_DIR/config.toml"; then
  echo "错误:config.toml 中占位符替换失败,请检查模板文件。" >&2
  exit 1
fi
echo "[5/6] 已拷贝并替换:models.json / config.toml → $CODEX_HOME_DIR"

# -----------------------------------------------------------------------------
# 6. 打印(不执行)守护进程命令与首次验证提示
#    命令依据:package.json bin 指向 dist/cli.js;cli.ts 提供
#    start / stop / restart / status 子命令,日志在 ~/.codex-channel-octo/logs/gateway.log
# -----------------------------------------------------------------------------
echo "[6/6] 配置完成。以下为守护进程命令(在 $APP_DIR 下执行):"
echo
echo "  启动(后台):  cd $APP_DIR && node dist/cli.js start"
echo "  查看状态:     cd $APP_DIR && node dist/cli.js status"
echo "  查看日志:     tail -f $BASE_DIR/logs/gateway.log"
echo "  停止:         cd $APP_DIR && node dist/cli.js stop"
echo "  重启:         cd $APP_DIR && node dist/cli.js restart"
echo
echo "提示:当前为只读模式首次运行 —— 先执行 start,然后在 Octo Web 端"
echo "私聊该 bot 发送一句话,确认有回复后再投入使用。"
