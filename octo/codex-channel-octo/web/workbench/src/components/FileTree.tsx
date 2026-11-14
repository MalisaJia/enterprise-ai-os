import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import { getTree, MAX_UPLOAD_BYTES, uploadFile } from '../api'
import { formatBytes, formatTime } from '../format'
import type { TreeEntry } from '../types'
import type { FileSelection, Toast } from '../App'

interface Props {
  token: string
  botId: string | null
  session: string | null
  selectedFile: FileSelection | null
  onOpenFile: (file: FileSelection) => void
  onAuthError: (err: unknown) => boolean
  onToast: (text: string, tone?: Toast['tone']) => void
}

interface Crumb {
  name: string
  path: string
}

function sortEntries(entries: TreeEntry[]): TreeEntry[] {
  return [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}

export default function FileTree({
  token,
  botId,
  session,
  selectedFile,
  onOpenFile,
  onAuthError,
  onToast,
}: Props) {
  const [crumbs, setCrumbs] = useState<Crumb[]>([])
  const [entries, setEntries] = useState<TreeEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const currentPath = crumbs.length > 0 ? crumbs[crumbs.length - 1].path : ''

  const load = useCallback(
    async (path: string) => {
      if (!botId || !session) return
      setEntries(null)
      setError(null)
      try {
        const res = await getTree(token, botId, session, path)
        setEntries(sortEntries(res.entries))
      } catch (err) {
        if (!onAuthError(err)) {
          setError(err instanceof Error ? err.message : String(err))
        }
      }
    },
    [token, botId, session, onAuthError],
  )

  // Reset & load root whenever bot/session changes.
  useEffect(() => {
    setCrumbs([])
    if (botId && session) void load('')
    else setEntries(null)
  }, [botId, session, load])

  const enterDir = (e: TreeEntry) => {
    const path = currentPath ? `${currentPath}/${e.name}` : e.name
    setCrumbs((c) => [...c, { name: e.name, path }])
    void load(path)
  }

  const gotoCrumb = (index: number) => {
    // index === -1 → root
    const next = index < 0 ? [] : crumbs.slice(0, index + 1)
    setCrumbs(next)
    void load(next.length > 0 ? next[next.length - 1].path : '')
  }

  // ── #43 upload: raw-body PUT per file into the session sandbox ROOT ────
  const onFilesPicked = async (ev: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(ev.target.files ?? [])
    ev.target.value = '' // allow re-selecting the exact same file(s)
    if (files.length === 0 || !botId || !session || uploading) return
    setUploading(true)
    let firstOk: string | null = null
    let okCount = 0
    for (const file of files) {
      // sequential — one PUT at a time keeps auth errors/toasts deterministic
      if (file.size > MAX_UPLOAD_BYTES) {
        onToast(`「${file.name}」超过 20 MB 上限,已跳过`, 'error')
        continue
      }
      try {
        await uploadFile(token, botId, session, file.name, file)
        okCount += 1
        firstOk ??= file.name
        onToast(`已上传:${file.name}`, 'success')
      } catch (err) {
        if (onAuthError(err)) {
          setUploading(false)
          return
        }
        onToast(
          `上传失败:${file.name} — ${err instanceof Error ? err.message : String(err)}`,
          'error',
        )
      }
    }
    setUploading(false)
    if (okCount > 0) {
      // refresh the ROOT listing, then auto-open the first uploaded file
      setCrumbs([])
      await load('')
      if (firstOk) onOpenFile({ name: firstOk, path: firstOk })
    }
  }

  return (
    <div className="tree-wrap">
      <div className="block-head">
        <span className="block-label">Workspace</span>
        <span className="tree-head-actions">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="upload-input"
            onChange={(ev) => void onFilesPicked(ev)}
          />
          <button
            className="upload-btn"
            disabled={!botId || !session || uploading}
            title="上传文件到当前会话工作区根目录(单文件 ≤ 20 MB)"
            onClick={() => fileInputRef.current?.click()}
          >
            <svg
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              aria-hidden="true"
            >
              <path d="M8 11V2.8M8 2.8 4.6 6.2M8 2.8l3.4 3.4" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M2.6 12.6v.9h10.8v-.9" strokeLinecap="round" />
            </svg>
            {uploading ? '上传中…' : '上传'}
          </button>
        </span>
      </div>

      <nav className="crumbs" aria-label="路径">
        <button
          className={`crumb${crumbs.length === 0 ? ' is-here' : ''}`}
          onClick={() => gotoCrumb(-1)}
        >
          ⌂
        </button>
        {crumbs.map((c, i) => (
          <span key={c.path} className="crumb-seg">
            <span className="crumb-sep">/</span>
            <button
              className={`crumb${i === crumbs.length - 1 ? ' is-here' : ''}`}
              onClick={() => gotoCrumb(i)}
            >
              {c.name}
            </button>
          </span>
        ))}
      </nav>

      {!botId || !session ? (
        <div className="block-empty">选择机器人和会话后浏览文件</div>
      ) : error ? (
        <div className="block-error">{error}</div>
      ) : entries === null ? (
        <div className="pane-busy">载入中…</div>
      ) : entries.length === 0 ? (
        <div className="block-empty">此目录为空</div>
      ) : (
        <table className="tree-table">
          <thead>
            <tr>
              <th>名称</th>
              <th className="num">大小</th>
              <th className="num">修改时间</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => {
              const path = currentPath ? `${currentPath}/${e.name}` : e.name
              const isSel =
                e.type === 'file' && selectedFile?.path === path
              return (
                <tr
                  key={path}
                  className={`tree-row tree-${e.type}${isSel ? ' is-active' : ''}`}
                  onClick={() =>
                    e.type === 'dir' ? enterDir(e) : onOpenFile({ name: e.name, path })
                  }
                >
                  <td className="tree-name">
                    <span className="tree-glyph">
                      {e.type === 'dir' ? '▸' : '▪'}
                    </span>
                    {e.name}
                  </td>
                  <td className="num">
                    {e.type === 'file' ? formatBytes(e.size) : '—'}
                  </td>
                  <td className="num" title={e.mtime}>
                    {formatTime(e.mtime)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
