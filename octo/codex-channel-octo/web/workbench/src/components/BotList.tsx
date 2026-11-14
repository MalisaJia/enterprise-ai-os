import type { Bot } from '../types'

interface Props {
  bots: Bot[] | null
  error: string | null
  selectedId: string | null
  switchingId: string | null
  onSelect: (id: string) => void
  onToggle: (bot: Bot) => void
  onRefresh: () => void
}

export default function BotList({
  bots,
  error,
  selectedId,
  switchingId,
  onSelect,
  onToggle,
  onRefresh,
}: Props) {
  return (
    <div className="block">
      <div className="block-head">
        <span className="block-label">Bots</span>
        <button className="icon-btn" title="刷新" onClick={onRefresh}>
          ⟳
        </button>
      </div>

      {bots === null && !error && <div className="pane-busy">载入中…</div>}
      {error && <div className="block-error">{error}</div>}
      {bots !== null && bots.length === 0 && (
        <div className="block-empty">当前账号下没有机器人</div>
      )}

      {bots?.map((bot) => {
        const writable = bot.allowWorkspaceWrite
        const switching = switchingId === bot.id
        // Non-codex runtimes (e.g. lumo) own their execution: no codex sandbox
        // double-switch to flip, so hide the toggle + sandboxMode and show a
        // runtime badge instead.
        const isCodex = (bot.runtime ?? 'codex') === 'codex'
        return (
          <div
            key={bot.id}
            className={`bot-card${bot.id === selectedId ? ' is-active' : ''}`}
            onClick={() => onSelect(bot.id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && onSelect(bot.id)}
          >
            <div className="bot-line">
              <span className={`dot ${bot.connected ? 'dot-live' : 'dot-off'}`} />
              <span className="bot-id" title={bot.id}>
                {bot.id}
              </span>
              {isCodex ? (
                <span className={`badge ${writable ? 'badge-write' : 'badge-ro'}`}>
                  {writable ? '可写' : '只读'}
                </span>
              ) : (
                <span className="badge badge-runtime" title="agent runtime">
                  {bot.runtime}
                </span>
              )}
            </div>

            <div className="bot-meta">
              <span title="robotUid">uid {bot.robotUid}</span>
              {isCodex && <span title="sandboxMode">{bot.sandboxMode}</span>}
            </div>

            {isCodex && (
              <button
                className={`toggle-btn${writable ? ' toggle-btn-demote' : ''}`}
                disabled={switching}
                onClick={(e) => {
                  e.stopPropagation()
                  onToggle(bot)
                }}
              >
                {switching
                  ? '重启中…'
                  : writable
                    ? '改为只读'
                    : '开启写入'}
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}
