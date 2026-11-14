/**
 * Preview panel — intentionally code-split (React.lazy in App.tsx) so
 * react-markdown and react-syntax-highlighter never land in the main chunk.
 * mammoth is additionally dynamic-imported only when a .docx is opened.
 */

import { useEffect, useMemo, useState } from 'react'
import DOMPurify from 'dompurify'
import ReactMarkdown from 'react-markdown'
import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneLight, vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'

import ts from 'react-syntax-highlighter/dist/esm/languages/prism/typescript'
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx'
import js from 'react-syntax-highlighter/dist/esm/languages/prism/javascript'
import jsx from 'react-syntax-highlighter/dist/esm/languages/prism/jsx'
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python'
import go from 'react-syntax-highlighter/dist/esm/languages/prism/go'
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash'
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json'
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml'
import toml from 'react-syntax-highlighter/dist/esm/languages/prism/toml'
import markup from 'react-syntax-highlighter/dist/esm/languages/prism/markup'
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css'
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql'

import { fetchFile, type FilePayload } from '../api'
import { formatBytes, previewKind, TEXT_PREVIEW_LIMIT } from '../format'
import { useTheme } from '../theme'
import type { FileSelection } from '../App'

const REGISTERED: Record<string, unknown> = {
  typescript: ts, tsx, javascript: js, jsx, python, go, bash, json, yaml,
  toml, markup, css, sql,
}
for (const [name, lang] of Object.entries(REGISTERED)) {
  SyntaxHighlighter.registerLanguage(name, lang as never)
}
const isRegistered = (lang?: string) => !!lang && lang in REGISTERED

/* mammoth does NOT filter hyperlink protocols: a crafted .docx can carry
   <w:hyperlink w:instr> or relationship targets like `javascript:…`, which
   land as `<a href="javascript:…">` in the generated HTML. Injecting that
   via dangerouslySetInnerHTML executes arbitrary JS on the workbench origin
   on click (e.g. exfiltrating the token from localStorage). We therefore
   run every mammoth result through DOMPurify before rendering.

   Config trade-off: the default profile is used unchanged on purpose.
   - It strips script/style elements, on* handlers and dangerous URI
     schemes (javascript:, vbscript:, data: in navigational hrefs) —
     exactly the attack surface mammoth can surface.
   - Everything mammoth legitimately emits survives untouched: semantic
     tags (p/h1-6/ul/ol/li/table), <a href> with http(s)/mailto/tel, <img>,
     and inline `style` attributes (kept by default), so document
     formatting is preserved with no false positives. No allow-list
     extension was needed. */
const sanitizeDocxHtml = (html: string) => DOMPurify.sanitize(html)

interface Props {
  token: string
  botId: string
  session: string
  file: FileSelection
  onAuthError: (err: unknown) => boolean
}

type Phase =
  | { s: 'loading' }
  | { s: 'ready'; payload: FilePayload; url: string }
  | { s: 'error'; msg: string }

export default function Preview({ token, botId, session, file, onAuthError }: Props) {
  const [phase, setPhase] = useState<Phase>({ s: 'loading' })
  const [theme] = useTheme()
  const [textContent, setTextContent] = useState<string | null>(null)
  const [docxHtml, setDocxHtml] = useState<string | null>(null)
  const [docxError, setDocxError] = useState<string | null>(null)

  const { kind, language } = useMemo(() => previewKind(file.name), [file.name])

  // ── Fetch the file ───────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    let url: string | null = null
    setPhase({ s: 'loading' })
    setTextContent(null)
    setDocxHtml(null)
    setDocxError(null)

    ;(async () => {
      try {
        const payload = await fetchFile(token, botId, session, file.path)
        if (cancelled) return
        url = URL.createObjectURL(payload.blob)
        setPhase({ s: 'ready', payload, url })
      } catch (err) {
        if (cancelled) return
        if (onAuthError(err)) return
        setPhase({ s: 'error', msg: err instanceof Error ? err.message : String(err) })
      }
    })()
    return () => {
      cancelled = true
      if (url) URL.revokeObjectURL(url)
    }
  }, [token, botId, session, file.path, onAuthError])

  // ── Decode text for markdown/code ────────────────────────────────────────
  useEffect(() => {
    if (phase.s !== 'ready') return
    if (kind !== 'markdown' && kind !== 'code') return
    if (phase.payload.blob.size > TEXT_PREVIEW_LIMIT) {
      setTextContent(`[文件过大(${formatBytes(phase.payload.blob.size)}),已省略在线渲染]`)
      return
    }
    let cancelled = false
    phase.payload.blob.text().then((t) => {
      if (!cancelled) setTextContent(t)
    })
    return () => {
      cancelled = true
    }
  }, [phase, kind])

  // ── docx → mammoth (lazy chunk) ──────────────────────────────────────────
  useEffect(() => {
    if (phase.s !== 'ready' || kind !== 'docx') return
    let cancelled = false
    ;(async () => {
      try {
        const arrayBuffer = await phase.payload.blob.arrayBuffer()
        const mammoth = await import('mammoth')
        const result = await mammoth.convertToHtml({ arrayBuffer })
        // Sanitize before storing — never let raw mammoth HTML reach the DOM.
        if (!cancelled) setDocxHtml(sanitizeDocxHtml(result.value))
      } catch (err) {
        if (!cancelled) {
          setDocxError(err instanceof Error ? err.message : String(err))
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [phase, kind])

  // ── Header (always visible once we know the filename) ────────────────────
  const header = (
    <div className="preview-head">
      <span className="preview-name" title={file.path}>
        {file.path}
      </span>
      {phase.s === 'ready' && (
        <span className="preview-size">{formatBytes(phase.payload.blob.size)}</span>
      )}
      {phase.s === 'ready' && (
        /* Download always goes through the file API content we already hold;
           a direct href to /workbench/api/…/file cannot carry the `token`
           header, so we serve the identical bytes via object URL. */
        <a className="btn btn-ghost btn-sm" href={phase.url} download={file.name}>
          ↓ 下载
        </a>
      )}
    </div>
  )

  if (phase.s === 'loading') {
    return (
      <div className="preview-wrap">
        {header}
        <div className="pane-busy">读取文件…</div>
      </div>
    )
  }

  if (phase.s === 'error') {
    return (
      <div className="preview-wrap">
        {header}
        <div className="preview-error">
          <div className="preview-error-title">预览失败</div>
          <p>{phase.msg}</p>
        </div>
      </div>
    )
  }

  const { payload } = phase

  if (payload.attachment) {
    return (
      <div className="preview-wrap">
        {header}
        <DownloadOnly note="服务端要求以附件形式获取,不支持在线预览。" url={phase.url} name={file.name} />
      </div>
    )
  }

  switch (kind) {
    case 'markdown':
      return (
        <div className="preview-wrap">
          {header}
          <div className="preview-scroll md-view">
            {textContent === null ? (
              <div className="pane-busy">解析中…</div>
            ) : (
              <ReactMarkdown>{textContent}</ReactMarkdown>
            )}
          </div>
        </div>
      )

    case 'code':
      return (
        <div className="preview-wrap">
          {header}
          <div className="preview-scroll">
            {textContent === null ? (
              <div className="pane-busy">解析中…</div>
            ) : (
              <SyntaxHighlighter
                language={isRegistered(language) ? language : undefined}
                style={theme === 'dark' ? vscDarkPlus : oneLight}
                showLineNumbers
                customStyle={{
                  margin: 0,
                  background: 'transparent',
                  fontSize: 12.5,
                  padding: '14px 10px',
                }}
              >
                {textContent}
              </SyntaxHighlighter>
            )}
          </div>
        </div>
      )

    case 'image':
      return (
        <div className="preview-wrap">
          {header}
          <div className="preview-scroll img-stage">
            <img src={phase.url} alt={file.name} />
          </div>
        </div>
      )

    case 'docx':
      return (
        <div className="preview-wrap">
          {header}
          <div className="preview-scroll">
            {docxError ? (
              <>
                <div className="preview-error">
                  <div className="preview-error-title">docx 解析失败</div>
                  <p>{docxError}</p>
                </div>
                <DownloadOnly note="转换失败,可下载原文件。" url={phase.url} name={file.name} />
              </>
            ) : docxHtml === null ? (
              <div className="pane-busy">转换 docx 中…(首次加载需下载解析器)</div>
            ) : (
              <div
                className="docx-view"
                // mammoth emits semantic HTML from OOXML but does not filter
                // hyperlink protocols; this value already passed through
                // DOMPurify.sanitize() above (see sanitizeDocxHtml).
                dangerouslySetInnerHTML={{ __html: docxHtml }}
              />
            )}
          </div>
        </div>
      )

    default:
      return (
        <div className="preview-wrap">
          {header}
          <DownloadOnly
            note={`类型 ${payload.contentType || '未知'} 暂不支持在线预览。`}
            url={phase.url}
            name={file.name}
          />
        </div>
      )
  }
}

function DownloadOnly({ note, url, name }: { note: string; url: string; name: string }) {
  return (
    <div className="download-card">
      <div className="download-glyph">▼</div>
      <p className="download-note">{note}</p>
      <a className="btn btn-solid" href={url} download={name}>
        ↓ 下载 {name}
      </a>
    </div>
  )
}
