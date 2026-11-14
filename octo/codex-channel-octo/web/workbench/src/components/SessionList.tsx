import type { Session } from '../types'
import { relativeTime } from '../format'

interface Props {
  sessions: Session[] | null
  truncated: boolean
  error: string | null
  selectedHex: string | null
  hasBot: boolean
  onSelect: (hex: string) => void
}

export default function SessionList({
  sessions,
  truncated,
  error,
  selectedHex,
  hasBot,
  onSelect,
}: Props) {
  return (
    <div className="block block-grow">
      <div className="block-head">
        <span className="block-label">Sessions</span>
        {sessions !== null && (
          <span className="block-count">{sessions.length}</span>
        )}
      </div>

      {!hasBot && <div className="block-empty">先选择一个机器人</div>}
      {hasBot && sessions === null && !error && (
        <div className="pane-busy">载入中…</div>
      )}
      {error && <div className="block-error">{error}</div>}
      {hasBot && sessions !== null && sessions.length === 0 && (
        <div className="block-empty">暂无会话工作区</div>
      )}

      {sessions?.map((s) => (
        <div
          key={s.hex}
          className={`session-row${s.hex === selectedHex ? ' is-active' : ''}`}
          onClick={() => onSelect(s.hex)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => e.key === 'Enter' && onSelect(s.hex)}
        >
          <div className="session-line">
            <span className={`kind-tag kind-${s.kind}`}>
              {s.kind === 'dm' ? '私聊' : '群组'}
            </span>
            <span className="session-hex" title={s.hex}>
              {s.hex}
            </span>
          </div>
          <div className="session-meta">
            <span title={`创建 ${s.createdAt} · 最近活动 ${s.lastActiveAt}`}>
              活跃 {relativeTime(s.lastActiveAt)}
            </span>
            <span>{s.fileCount} 个文件</span>
          </div>
        </div>
      ))}

      {truncated && (
        <div className="block-note">列表已截断,仅显示最近会话</div>
      )}
      {hasBot && (
        <div className="block-note block-note-warn">
          ⏱ 7 天无活动的会话目录会被自动清理
        </div>
      )}
    </div>
  )
}
