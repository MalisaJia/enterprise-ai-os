/** Display helpers: byte sizes, timestamps, and file-kind classification. */

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

export function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso || '—'
  const pad = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`
}

export function relativeTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso || '—'
  const diff = Date.now() - d.getTime()
  const min = Math.floor(diff / 60_000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  const days = Math.floor(h / 24)
  if (days < 30) return `${days} 天前`
  return formatTime(iso)
}

export function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}

const MARKDOWN_EXTS = new Set(['md', 'markdown', 'mdown'])
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'])

/** Extension → react-syntax-highlighter language id. */
const CODE_LANG: Record<string, string> = {
  ts: 'typescript',
  tsx: 'tsx',
  js: 'javascript',
  jsx: 'jsx',
  mjs: 'javascript',
  cjs: 'javascript',
  py: 'python',
  go: 'go',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  json: 'json',
  jsonc: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  css: 'css',
  scss: 'scss',
  html: 'markup',
  htm: 'markup',
  xml: 'markup',
  vue: 'markup',
  sql: 'sql',
  rs: 'rust',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  hpp: 'cpp',
  cs: 'csharp',
  rb: 'ruby',
  php: 'php',
  dockerfile: 'docker',
  ini: 'ini',
  env: 'bash',
  makefile: 'makefile',
}

export type PreviewKind = 'markdown' | 'image' | 'code' | 'docx' | 'binary'

export function previewKind(name: string): {
  kind: PreviewKind
  language?: string
} {
  const ext = extOf(name)
  const base = name.toLowerCase()
  if (MARKDOWN_EXTS.has(ext)) return { kind: 'markdown' }
  if (ext === 'docx') return { kind: 'docx' }
  if (IMAGE_EXTS.has(ext)) return { kind: 'image' }
  const lang = CODE_LANG[ext] ?? CODE_LANG[base]
  if (lang) return { kind: 'code', language: lang }
  return { kind: 'binary' }
}

/** Max inline text size for code/markdown preview (2 MB). */
export const TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024
