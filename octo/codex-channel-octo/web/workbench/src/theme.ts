/**
 * Dual-theme support for the workbench SPA.
 *
 * Resolution order:
 *   1. Manual choice persisted in localStorage under `wb-theme`.
 *   2. Follow the host site: the workbench is usually embedded as a
 *      same-origin iframe inside octo-web, which applies dark mode via a
 *      `theme-mode="dark"` *attribute* on `document.body` (light mode =
 *      attribute absent) and persists the choice in sessionStorage under
 *      the key `theme-mode` (values "1"/"0", see dmworkbase StorageService
 *      + WKConfig.themeMode). We probe `window.parent` inside try/catch so
 *      a cross-origin or standalone window degrades silently.
 *   3. Default: light.
 *
 * The resolved theme is exposed as `data-theme="light|dark"` on the root
 * <html> element; styles.css carries both variable sets. While no manual
 * choice exists we keep following live changes of the parent's
 * `theme-mode` attribute through a MutationObserver.
 */

import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

/** localStorage key for the user's explicit manual choice. */
export const THEME_STORAGE_KEY = 'wb-theme'

/** Key octo-web (WKApp.config.themeMode → StorageService) persists under. */
const PARENT_STORAGE_KEY = 'theme-mode'
/** Attribute octo-web sets on document.body for dark mode. */
const PARENT_ATTR = 'theme-mode'

const listeners = new Set<(t: Theme) => void>()

function readStored(): Theme | null {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    return null
  }
}

/**
 * Probe the parent document's theme. Returns null when we are not embedded
 * (top-level window) or the parent is not same-origin readable.
 */
export function detectParentTheme(): Theme | null {
  try {
    const w = window.parent
    if (!w || w === window) return null
    const doc = w.document
    if (!doc || !doc.body) return null
    // 1) Authoritative, live signal: body attribute maintained by octo-web.
    const attr = doc.body.getAttribute(PARENT_ATTR)
    if (attr === 'dark') return 'dark'
    // 2) Persisted choice (written before/without touching the body attr).
    try {
      const stored = w.sessionStorage.getItem(PARENT_STORAGE_KEY)
      if (stored === '1' || stored === 'dark') return 'dark'
      if (stored === '0' || stored === 'light') return 'light'
    } catch {
      /* storage blocked — fall through */
    }
    // 3) Same-origin body without the attribute ⇒ octo-web light (default).
    return 'light'
  } catch {
    // Cross-origin or sandboxed iframe: window.parent access throws.
    return null
  }
}

export function resolveTheme(): Theme {
  return readStored() ?? detectParentTheme() ?? 'light'
}

/** Theme currently applied to the document (light until proven otherwise). */
export function currentTheme(): Theme {
  return document.documentElement.getAttribute('data-theme') === 'dark'
    ? 'dark'
    : 'light'
}

/** Apply a theme to the document and notify subscribers (no persistence). */
export function applyTheme(t: Theme) {
  const el = document.documentElement
  if (el.getAttribute('data-theme') === t) return
  el.setAttribute('data-theme', t)
  listeners.forEach((fn) => fn(t))
}

/** Explicit user choice: persist and apply. Supersedes parent following. */
export function chooseTheme(t: Theme) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, t)
  } catch {
    /* private mode etc. — apply anyway */
  }
  applyTheme(t)
}

let observerStarted = false

/**
 * Resolve + apply the initial theme and start following the parent's
 * `theme-mode` attribute. Call once before first render (main.tsx);
 * index.html additionally repeats a minimal synchronous version of this
 * resolution before CSS paints to avoid a flash.
 */
export function initTheme() {
  applyTheme(resolveTheme())
  if (observerStarted) return
  observerStarted = true
  try {
    const w = window.parent
    if (!w || w === window) return
    const doc = w.document
    if (!doc || !doc.body) return
    // A MutationObserver from this frame can observe the parent document's
    // body just fine (same-origin), so no need to reach into the parent's
    // globals (typed awkwardly across frames).
    if (typeof MutationObserver === 'undefined') return
    const mo = new MutationObserver(() => {
      if (readStored()) return // manual choice always wins
      const t = detectParentTheme()
      if (t) applyTheme(t)
    })
    mo.observe(doc.body, { attributes: true, attributeFilter: [PARENT_ATTR] })
  } catch {
    /* not embedded same-origin — standalone behavior is fine */
  }
}

/** React binding: current theme + a toggle that persists the choice. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(currentTheme)
  useEffect(() => {
    const on = (t: Theme) => setTheme(t)
    listeners.add(on)
    return () => {
      listeners.delete(on)
    }
  }, [])
  const toggle = useCallback(() => {
    chooseTheme(currentTheme() === 'dark' ? 'light' : 'dark')
  }, [])
  return [theme, toggle]
}
