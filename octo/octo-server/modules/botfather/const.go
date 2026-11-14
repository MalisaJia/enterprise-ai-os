package botfather

import "github.com/Mininglamp-OSS/octo-server/modules/botfather/cmdmenu"

const (
	// BotFatherUID 系统Bot的用户UID（历史名 botfather，内部标识保持不变——
	// 动它会断掉好友关系/消息历史/已签发 token；显示名见 BotFatherName）。
	// 真相源在 cmdmenu 叶子包（#335）——读路径模块（user/channel/robot）需要
	// 匹配该 UID 却不能 import 本包（botfather → user 会成环），这里保留别名
	// 供包内调用点使用。
	BotFatherUID = cmdmenu.BotFatherUID
	// BotFatherName 系统Bot的显示名称（原 BotFather，2026-09 去爹味改名 BotStudio。
	// 常量名保留 BotFatherName 不改，避免大面积无意义重命名）。
	BotFatherName = "BotStudio"
	// System UIDs excluded from bot-related batch operations
	systemUIDAdmin      = "u_10000"
	systemUIDFileHelper = "fileHelper"
	// BotTokenPrefix Bot Token前缀
	BotTokenPrefix = "bf_"
	// UserAPIKeyPrefix User API Key前缀
	UserAPIKeyPrefix = "uk_"
	// BotUsernameSuffix 机器人用户名后缀
	BotUsernameSuffix = "_bot"

	// Redis状态机相关
	stateKeyPrefix = "botfather:state:" // Redis Hash key前缀
	stateTTL       = 600                // 状态过期时间（秒）

	// 心跳相关
	heartbeatKeyPrefix = "bot:heartbeat:" // 心跳key前缀
	heartbeatTTL       = 60               // 心跳过期时间（秒）
)

// BotFather 命令
const (
	CmdNewBot         = "/newbot"
	CmdMyBots         = "/mybots"
	CmdConnect        = "/connect"
	CmdDisconnect     = "/disconnect"
	CmdSetName        = "/setname"
	CmdSetDescription = "/setdescription"
	CmdDeleteBot      = "/deletebot"
	CmdToken          = "/token"
	CmdRevoke         = "/revoke"
	CmdCancel         = "/cancel"
	CmdHelp           = "/help"
	CmdStart          = "/start"
	CmdApprove        = "/approve"
	CmdReject         = "/reject"
	CmdPending        = "/pending"
	CmdQuickstart     = "/quickstart"
	CmdInstall        = "/install"
)

// 对话状态
const (
	StateNone                  = ""
	StateWaitingBotName        = "waiting_bot_name"
	StateWaitingSelectBot      = "waiting_select_bot"
	StateWaitingNewName        = "waiting_new_name"
	StateWaitingDescription    = "waiting_description"
	StateWaitingDeleteConfirm  = "waiting_delete_confirm"
	StateWaitingRevokeConfirm  = "waiting_revoke_confirm"
)

// 状态上下文字段
const (
	FieldState   = "state"
	FieldCommand = "command"
	FieldBotID   = "bot_id"
	FieldBotName = "bot_name"
)

// systemExcludedUIDs is the canonical list of system UIDs excluded from
// batch bot operations (ensureBotFatherFriends, repairOrphanBots, etc.).
// Maintain this single list to avoid drift between functions.
var systemExcludedUIDs = []string{BotFatherUID, systemUIDAdmin, systemUIDFileHelper}
