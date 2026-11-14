import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getBots, getSessions, sleep, toggleSandbox } from './api'
import TopBar from './components/TopBar'
import BotList from './components/BotList'
import SessionList from './components/SessionList'
import FileTree from './components/FileTree'
import ConfirmDialog from './components/ConfirmDialog'
import { findTokenCandidates } from './token'
import type { Bot, Session } from './types'

/** Preview panel is code-split: react-markdown + highlighter only load on demand. */
const Preview = lazy(() => import('./components/Preview'))

type Auth =
  | { phase: 'probing' }
  | { phase: 'anon'; reason: string }
  | { phase: 'ready'; token: string }

export interface FileSelection {
  name: string
  path: string
}

export interface Toast {
  id: number
  text: string
  tone: 'info' | 'error' | 'success'
}

let toastSeq = 1

// ── URL deep-link support ──────────────────────────────────────────────────
// /?bot=<robotUid>&session=<16hex>&path=<relPath> — parsed once at module
// load; each param is consumed a single time and never overrides later
// manual selection. Missing/invalid params silently fall back to defaults.
const DEEP_LINK = (() => {
  const nil = { bot: null as string | null, session: null as string | null, path: null as string | null }
  try {
    const sp = new URLSearchParams(window.location.search)
    const bot = sp.get('bot')?.trim() || null
    const session = sp.get('session')?.trim() || null
    const path = sp.get('path')?.trim() || null
    return {
      bot,
      // must look like a 16-char hex session id to be honored
      session: session && /^[0-9a-f]{16}$/i.test(session) ? session : null,
      path,
    }
  } catch {
    return nil
  }
})()

let deepBotConsumed = false
let deepSessionConsumed = false

// ── #43 three-column layout: draggable widths ─────────────────────
// BOTS | WORKSPACE | PREVIEW with two split bars. Widths persist in
// localStorage under COL_WIDTHS_KEY; double-clicking a split bar resets to
// DEFAULT_COL_WIDTHS. Narrow windows shrink the two fixed columns
// proportionally so the layout never overflows.
const COL_WIDTHS_KEY = 'wb-col-widths'
const SPLIT_W = 6
/** Height of a horizontal split bar in the stacked (narrow) layout. */
const SPLIT_H = 6
const MIN_NAV = 180
const MIN_TREE = 220
const MIN_PREVIEW = 280
const DEFAULT_COL_WIDTHS = { nav: 320, tree: 360 } as const

interface ColWidths {
  nav: number
  tree: number
}

function loadColWidths(): ColWidths {
  try {
    const raw = window.localStorage.getItem(COL_WIDTHS_KEY)
    if (!raw) return { ...DEFAULT_COL_WIDTHS }
    const parsed = JSON.parse(raw) as Partial<ColWidths>
    const nav = Number(parsed?.nav)
    const tree = Number(parsed?.tree)
    if (!Number.isFinite(nav) || !Number.isFinite(tree)) return { ...DEFAULT_COL_WIDTHS }
    return {
      nav: Math.min(Math.max(Math.round(nav), MIN_NAV), 900),
      tree: Math.min(Math.max(Math.round(tree), MIN_TREE), 900),
    }
  } catch {
    return { ...DEFAULT_COL_WIDTHS }
  }
}

function saveColWidths(w: ColWidths): void {
  try {
    window.localStorage.setItem(COL_WIDTHS_KEY, JSON.stringify(w))
  } catch {
    /* storage unavailable — widths simply won't persist */
  }
}

// ── stacked (narrow) layout: draggable row heights ──────────────────
// Below 981px the deck stacks vertically (BOTS / WORKSPACE / PREVIEW);
// the same two split bars then adjust ROW heights instead of column
// widths. Persisted separately under ROW_HEIGHTS_KEY so wide and narrow
// tweaks never clobber each other.
const ROW_HEIGHTS_KEY = 'wb-row-heights'
const MIN_NAV_ROW = 140
const MIN_TREE_ROW = 180
const MIN_PREVIEW_ROW = 200
const DEFAULT_ROW_HEIGHTS = { nav: 280, tree: 320 } as const

interface RowHeights {
  nav: number
  tree: number
}

function loadRowHeights(): RowHeights {
  try {
    const raw = window.localStorage.getItem(ROW_HEIGHTS_KEY)
    if (!raw) return { ...DEFAULT_ROW_HEIGHTS }
    const parsed = JSON.parse(raw) as Partial<RowHeights>
    const nav = Number(parsed?.nav)
    const tree = Number(parsed?.tree)
    if (!Number.isFinite(nav) || !Number.isFinite(tree)) return { ...DEFAULT_ROW_HEIGHTS }
    return {
      nav: Math.min(Math.max(Math.round(nav), MIN_NAV_ROW), 1200),
      tree: Math.min(Math.max(Math.round(tree), MIN_TREE_ROW), 1200),
    }
  } catch {
    return { ...DEFAULT_ROW_HEIGHTS }
  }
}

function saveRowHeights(h: RowHeights): void {
  try {
    window.localStorage.setItem(ROW_HEIGHTS_KEY, JSON.stringify(h))
  } catch {
    /* storage unavailable — heights simply won't persist */
  }
}

/** Last path segment, tolerant of both '/' and '\\' separators. */
const basenameOf = (p: string) => p.split(/[\\/]+/).filter(Boolean).pop() ?? p

export default function App() {
  const [auth, setAuth] = useState<Auth>({ phase: 'probing' })
  const [bots, setBots] = useState<Bot[] | null>(null)
  const [botsError, setBotsError] = useState<string | null>(null)
  const [selectedBotId, setSelectedBotId] = useState<string | null>(null)
  const [sessions, setSessions] = useState<Session[] | null>(null)
  const [sessionsTruncated, setSessionsTruncated] = useState(false)
  const [sessionsError, setSessionsError] = useState<string | null>(null)
  const [selectedSession, setSelectedSession] = useState<string | null>(null)
  const [selectedFile, setSelectedFile] = useState<FileSelection | null>(null)
  const [switchingBotId, setSwitchingBotId] = useState<string | null>(null)
  const [pendingToggle, setPendingToggle] = useState<{ bot: Bot; target: boolean } | null>(null)
  const [toasts, setToasts] = useState<Toast[]>([])

  // ── #43 column widths (persisted, draggable) ──────────────────────────
  const [colWidths, setColWidths] = useState<ColWidths>(loadColWidths)
  // ── stacked layout row heights (persisted, draggable) ─────────────────
  const [rowHeights, setRowHeights] = useState<RowHeights>(loadRowHeights)
  const [dragIndex, setDragIndex] = useState<0 | 1 | null>(null)
  const [isWide, setIsWide] = useState(() => window.matchMedia('(min-width: 981px)').matches)
  const [deckW, setDeckW] = useState(0)
  const deckRef = useRef<HTMLElement | null>(null)
  const dragStartRef = useRef<
    | { mode: 'col'; startX: number; nav: number; tree: number }
    | { mode: 'row'; startY: number; nav: number; tree: number }
    | null
  >(null)
  const widthsRef = useRef(colWidths)
  widthsRef.current = colWidths
  const rowsRef = useRef(rowHeights)
  rowsRef.current = rowHeights

  // Mirrors the CSS breakpoint: below 981px the deck stacks and split bars
  // are hidden, so inline column widths must not be applied.
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 981px)')
    const onChange = (e: MediaQueryListEvent) => setIsWide(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // Deck width (for proportional shrink on narrow windows).
  useEffect(() => {
    const el = deckRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setDeckW(el.clientWidth))
    ro.observe(el)
    setDeckW(el.clientWidth)
    return () => ro.disconnect()
  }, [auth.phase])

  // Document-level drag tracking while a split bar is held. In the wide
  // (3-column) layout the bars drag horizontally (column widths); in the
  // stacked (narrow) layout the SAME bars drag vertically (row heights).
  useEffect(() => {
    if (dragIndex === null) return
    const onMove = (e: MouseEvent) => {
      const start = dragStartRef.current
      if (!start) return
      if (start.mode === 'row') {
        // ── stacked layout: adjust row heights ──
        const dy = e.clientY - start.startY
        if (dragIndex === 0) {
          const nav = Math.max(start.nav + dy, MIN_NAV_ROW)
          setRowHeights((h) => ({ ...h, nav }))
        } else {
          const tree = Math.max(start.tree + dy, MIN_TREE_ROW)
          setRowHeights((h) => ({ ...h, tree }))
        }
        return
      }
      // ── wide layout: adjust column widths ──
      const deckWidth = deckRef.current?.clientWidth || window.innerWidth
      const avail = Math.max(
        MIN_NAV + MIN_TREE,
        deckWidth - SPLIT_W * 2 - MIN_PREVIEW,
      )
      const dx = e.clientX - start.startX
      if (dragIndex === 0) {
        const nav = Math.round(
          Math.min(Math.max(start.nav + dx, MIN_NAV), avail - MIN_TREE),
        )
        setColWidths((w) => ({ ...w, nav }))
      } else {
        const maxTree = Math.max(MIN_TREE, avail - start.nav)
        const tree = Math.round(
          Math.min(Math.max(start.tree + dx, MIN_TREE), maxTree),
        )
        setColWidths((w) => ({ ...w, tree }))
      }
    }
    const onUp = () => {
      if (dragStartRef.current?.mode === 'row') saveRowHeights(rowsRef.current)
      else saveColWidths(widthsRef.current)
      dragStartRef.current = null
      setDragIndex(null)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
  }, [dragIndex])

  const startColDrag = (index: 0 | 1) => (e: React.MouseEvent) => {
    e.preventDefault()
    if (isWide) {
      dragStartRef.current = { mode: 'col', startX: e.clientX, nav: colWidths.nav, tree: colWidths.tree }
    } else {
      dragStartRef.current = { mode: 'row', startY: e.clientY, nav: rowHeights.nav, tree: rowHeights.tree }
    }
    setDragIndex(index)
  }

  const resetColWidths = () => {
    if (isWide) {
      const next = { ...DEFAULT_COL_WIDTHS }
      setColWidths(next)
      saveColWidths(next)
    } else {
      const next = { ...DEFAULT_ROW_HEIGHTS }
      setRowHeights(next)
      saveRowHeights(next)
    }
  }

  const pushToast = useCallback((text: string, tone: Toast['tone'] = 'info') => {
    const id = toastSeq++
    setToasts((t) => [...t, { id, text, tone }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200)
  }, [])

  const logout = useCallback(
    (reason: string) => setAuth({ phase: 'anon', reason }),
    [],
  )

  /** Central handler: any 401/403 drops back to the login gate. */
  const onAuthError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.isAuthError) {
        logout('登录凭据已失效(401/403),请回到主站重新登录。')
        return true
      }
      return false
    },
    [logout],
  )

  // ── Token probe ──────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    const candidates = findTokenCandidates()
    if (candidates.length === 0) {
      setAuth({ phase: 'anon', reason: '' })
      return
    }
    ;(async () => {
      for (const token of candidates) {
        try {
          await getBots(token)
          if (!cancelled) setAuth({ phase: 'ready', token })
          return
        } catch {
          // try the next candidate
        }
      }
      if (!cancelled) setAuth({ phase: 'anon', reason: '' })
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // ── Bot loading ──────────────────────────────────────────────────────────
  const loadBots = useCallback(
    async (token: string, silent = false) => {
      try {
        const { bots: list } = await getBots(token)
        setBots(list)
        setBotsError(null)
        if (!deepBotConsumed) {
          deepBotConsumed = true
          // Deep link may carry either the configId or the robotUid (the
          // main site links with robotUid); accept both.
          const deepHit = DEEP_LINK.bot
            ? list.find((b) => b.id === DEEP_LINK.bot || b.robotUid === DEEP_LINK.bot)
            : undefined
          // No deep link (main-site "工作区" menu entry) → auto-select the
          // first bot so the workspace is never empty.
          setSelectedBotId((cur) =>
            cur && list.some((b) => b.id === cur)
              ? cur
              : (deepHit?.id ?? list[0]?.id ?? null),
          )
        } else {
          setSelectedBotId((cur) =>
            cur && list.some((b) => b.id === cur) ? cur : (list[0]?.id ?? null),
          )
        }
      } catch (err) {
        if (!onAuthError(err) && !silent) {
          setBotsError(err instanceof Error ? err.message : String(err))
        }
      }
    },
    [onAuthError],
  )

  useEffect(() => {
    if (auth.phase === 'ready') void loadBots(auth.token)
    else {
      setBots(null)
      setSelectedBotId(null)
    }
  }, [auth, loadBots])

  // ── Session loading ──────────────────────────────────────────────────────
  useEffect(() => {
    setSelectedSession(null)
    setSelectedFile(null)
    setSessions(null)
    setSessionsError(null)
    if (auth.phase !== 'ready' || !selectedBotId) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await getSessions(auth.token, selectedBotId)
        if (cancelled) return
        setSessions(res.sessions)
        setSessionsTruncated(res.truncated)
        let target: string | null = null
        let deepFile: FileSelection | null = null
        if (!deepSessionConsumed) {
          deepSessionConsumed = true
          if (DEEP_LINK.session && res.sessions.some((s) => s.hex === DEEP_LINK.session)) {
            target = DEEP_LINK.session
          }
          if (DEEP_LINK.path) {
            deepFile = { name: basenameOf(DEEP_LINK.path), path: DEEP_LINK.path }
          }
        }
        if (!target && res.sessions.length > 0) target = res.sessions[0].hex
        if (target) {
          setSelectedSession(target)
          // Preview fetches by path directly, so the file can be selected
          // before the file tree has loaded.
          if (deepFile) setSelectedFile(deepFile)
        }
      } catch (err) {
        if (onAuthError(err)) return
        if (!cancelled) setSessionsError(err instanceof Error ? err.message : String(err))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [auth, selectedBotId, onAuthError])

  // ── Sandbox toggle with confirm + polling ───────────────────────────────
  const requestToggle = useCallback((bot: Bot) => {
    setPendingToggle({ bot, target: !bot.allowWorkspaceWrite })
  }, [])

  const confirmToggle = useCallback(async () => {
    if (!pendingToggle || auth.phase !== 'ready') return
    const { bot, target } = pendingToggle
    setPendingToggle(null)
    setSwitchingBotId(bot.id)
    try {
      await toggleSandbox(auth.token, bot.id, target)
    } catch (err) {
      if (!onAuthError(err)) {
        pushToast(
          `切换失败:${err instanceof Error ? err.message : String(err)}`,
          'error',
        )
      }
      setSwitchingBotId(null)
      return
    }
    // The bot briefly restarts — poll until the state flips or we time out.
    const deadline = Date.now() + 15_000
    const poll = async () => {
      while (Date.now() < deadline) {
        await sleep(1500)
        try {
          const { bots: list } = await getBots(auth.token)
          setBots(list)
          const fresh = list.find((b) => b.id === bot.id)
          if (fresh && fresh.allowWorkspaceWrite === target) {
            pushToast(
              target ? '已切换为可写模式' : '已切换为只读模式',
              'success',
            )
            return true
          }
        } catch (err) {
          if (onAuthError(err)) return false
          // bot may still be restarting — keep polling
        }
      }
      pushToast('状态更新超时,机器人可能仍在重启,请稍候手动刷新。', 'error')
      return false
    }
    await poll().finally(() => setSwitchingBotId(null))
  }, [pendingToggle, auth, onAuthError, pushToast])

  // ── Render ───────────────────────────────────────────────────────────────
  if (auth.phase === 'probing') {
    return (
      <div className="gate">
        <div className="gate-box rise">
          <div className="gate-mark">▚▞</div>
          <div className="gate-title">正在检测登录状态</div>
          <div className="gate-sub">scanning local credentials…</div>
        </div>
      </div>
    )
  }

  if (auth.phase === 'anon') {
    return <LoginGate reason={auth.reason} />
  }

  const token = auth.token
  const selectedBot = bots?.find((b) => b.id === selectedBotId) ?? null

  // Effective column widths: shrink proportionally when the window is too
  // narrow for both fixed columns + the preview minimum (never overflow).
  let navPx = colWidths.nav
  let treePx = colWidths.tree
  if (isWide && deckW > 0) {
    const avail = deckW - SPLIT_W * 2 - MIN_PREVIEW
    const total = navPx + treePx
    if (avail > 0 && total > avail) {
      const k = avail / total
      navPx = Math.round(navPx * k)
      treePx = Math.round(treePx * k)
    }
  }
  // Stacked layout: fixed row heights for the first two panes, preview
  // takes the rest. Applied as inline grid-template-rows (the CSS media
  // query default minmax(220px, auto) is overridden).
  const stackedStyle = !isWide
    ? {
        gridTemplateRows: `${rowHeights.nav}px ${SPLIT_H}px ${rowHeights.tree}px ${SPLIT_H}px minmax(${MIN_PREVIEW_ROW}px, 1fr)`,
      }
    : undefined
  const deckStyle = isWide
    ? {
        gridTemplateColumns: `${navPx}px ${SPLIT_W}px ${treePx}px ${SPLIT_W}px minmax(0, 1fr)`,
      }
    : stackedStyle

  return (
    <div className="shell">
      <TopBar token={token} />

      <main
        className={`deck${dragIndex !== null ? ' is-resizing' : ''}`}
        ref={deckRef}
        style={deckStyle}
      >
        {/* Column Ⅰ — bots & sessions */}
        <section className="pane pane-nav rise" style={{ ['--d' as string]: '0.05s' }}>
          <BotList
            bots={bots}
            error={botsError}
            selectedId={selectedBotId}
            switchingId={switchingBotId}
            onSelect={setSelectedBotId}
            onToggle={requestToggle}
            onRefresh={() => void loadBots(token)}
          />
          <SessionList
            sessions={sessions}
            truncated={sessionsTruncated}
            error={sessionsError}
            selectedHex={selectedSession}
            hasBot={!!selectedBotId}
            onSelect={setSelectedSession}
          />
        </section>

        <div
          className={`split-bar${dragIndex === 0 ? ' is-active' : ''}`}
          role="separator"
          aria-orientation={isWide ? 'vertical' : 'horizontal'}
          title={isWide ? '拖拽调整宽度 · 双击复位' : '拖拽调整高度 · 双击复位'}
          onMouseDown={startColDrag(0)}
          onDoubleClick={resetColWidths}
        />

        {/* Column Ⅱ — file tree */}
        <section className="pane pane-tree rise" style={{ ['--d' as string]: '0.12s' }}>
          <FileTree
            token={token}
            botId={selectedBotId}
            session={selectedSession}
            selectedFile={selectedFile}
            onOpenFile={setSelectedFile}
            onAuthError={onAuthError}
            onToast={pushToast}
          />
        </section>

        <div
          className={`split-bar${dragIndex === 1 ? ' is-active' : ''}`}
          role="separator"
          aria-orientation={isWide ? 'vertical' : 'horizontal'}
          title={isWide ? '拖拽调整宽度 · 双击复位' : '拖拽调整高度 · 双击复位'}
          onMouseDown={startColDrag(1)}
          onDoubleClick={resetColWidths}
        />

        {/* Column Ⅲ — preview */}
        <section className="pane pane-preview rise" style={{ ['--d' as string]: '0.19s' }}>
          {selectedFile && selectedBot && selectedSession ? (
            <Suspense fallback={<div className="pane-busy">加载预览组件…</div>}>
              <Preview
                key={`${selectedBot.id}/${selectedSession}/${selectedFile.path}`}
                token={token}
                botId={selectedBot.id}
                session={selectedSession}
                file={selectedFile}
                onAuthError={onAuthError}
              />
            </Suspense>
          ) : (
            <div className="empty-preview">
              <div className="empty-glyph">▟▙</div>
              <p>选择左侧文件以预览</p>
              <p className="empty-sub">.md · 代码 · 图片 · .docx 支持在线预览</p>
            </div>
          )}
        </section>
      </main>

      {/* Toasts */}
      <div className="toasts" role="status">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>
            {t.text}
          </div>
        ))}
      </div>

      {pendingToggle && (
        <ConfirmDialog
          title={pendingToggle.target ? '切换到可写模式?' : '切换到只读模式?'}
          body={
            <>
              机器人 <b>{pendingToggle.bot.id}</b> 将
              {pendingToggle.target ? '获得工作区写权限' : '被限制为只读'}。
              <br />
              <span className="confirm-warn">⚠ 切换会使该机器人短暂重启数秒。</span>
            </>
          }
          confirmText={pendingToggle.target ? '允许写入' : '改为只读'}
          danger={pendingToggle.target}
          onConfirm={() => void confirmToggle()}
          onCancel={() => setPendingToggle(null)}
        />
      )}
    </div>
  )
}

/** Full-screen gate shown when no usable login credential was found. */
function LoginGate({ reason }: { reason: string }) {
  const mainSite = window.location.origin + '/'
  return (
    <div className="gate">
      <div className="gate-box rise">
        <div className="gate-mark gate-mark-off">▚▞</div>
        <div className="gate-title">未检测到登录态</div>
        <div className="gate-sub">
          {reason || '请先在主站登录后再访问工作台,凭据将由主站写入本地存储。'}
        </div>
        <a className="gate-cta" href={mainSite}>
          前往主站登录 ↗
        </a>
        <div className="gate-host">{window.location.host}</div>
      </div>
    </div>
  )
}
