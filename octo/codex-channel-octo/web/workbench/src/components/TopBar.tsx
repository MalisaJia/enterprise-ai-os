import { maskToken } from '../token'
import { useTheme } from '../theme'

interface Props {
  token: string
}

/** Fixed status strip: identity, host, theme switch, and a way back home. */
export default function TopBar({ token }: Props) {
  const mainSite = window.location.origin + '/'
  const [theme, toggleTheme] = useTheme()
  return (
    <header className="topbar">
      <div className="topbar-brand">
        <span className="brand-mark">▚▞</span>
        <span className="brand-name">CODEX&nbsp;WORKBENCH</span>
        <span className="brand-ver">v0.1</span>
      </div>

      <div className="topbar-status">
        <span className="status-item">
          <span className="dot dot-live" />
          已登录
          <span className="status-token">{maskToken(token)}</span>
        </span>
        <span className="status-sep">/</span>
        <span className="status-item status-host" title="工作台与主站同源部署">
          主站 {window.location.host}
        </span>
      </div>

      {/* Inline SVGs, zero dependencies. Shows the theme you switch TO. */}
      <button
        type="button"
        className="theme-btn"
        onClick={toggleTheme}
        title={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
        aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
      >
        {theme === 'dark' ? (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
            <circle cx="12" cy="12" r="4.2" />
            <path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3l1.7 1.7M17 17l1.7 1.7M18.7 5.3L17 7M7 17l-1.7 1.7" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M20.2 14.5A8.3 8.3 0 0 1 9.5 3.8a8.3 8.3 0 1 0 10.7 10.7Z" />
          </svg>
        )}
      </button>

      <a className="topbar-home" href={mainSite}>
        返回主站 ↗
      </a>
    </header>
  )
}
