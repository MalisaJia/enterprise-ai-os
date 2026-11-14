/**
 * API client — contract-frozen against the backend. Paths are relative to
 * the current origin; the production host serves this SPA and the API under
 * the same /workbench/ prefix, and vite dev proxies /workbench/api.
 *
 * Auth: every request carries a bare `token` header (NOT "Bearer …"),
 * matching how the Octo main site calls its own APIs.
 */

import type {
  BotListResponse,
  SandboxToggleResponse,
  SessionListResponse,
  TreeEntry,
  TreeResponse,
} from './types'

export const API_PREFIX = '/workbench/api'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let msg = `HTTP ${res.status}`
  try {
    const body = (await res.json()) as { error?: string }
    if (body && typeof body.error === 'string' && body.error) msg = body.error
  } catch {
    /* non-JSON error body — keep the generic message */
  }
  return new ApiError(res.status, msg)
}

async function request<T>(
  token: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers: {
      ...(init?.headers || {}),
      token,
    },
  })
  if (!res.ok) throw await parseError(res)
  return (await res.json()) as T
}

export function getBots(token: string): Promise<BotListResponse> {
  return request<BotListResponse>(token, '/bots')
}

export function getSessions(
  token: string,
  botId: string,
): Promise<SessionListResponse> {
  return request<SessionListResponse>(token, `/bots/${encodeURIComponent(botId)}/sessions`)
}

export function getTree(
  token: string,
  botId: string,
  session: string,
  path: string,
): Promise<TreeResponse> {
  const qs = new URLSearchParams({ session, path })
  return request<TreeResponse>(
    token,
    `/bots/${encodeURIComponent(botId)}/tree?${qs.toString()}`,
  )
}

export function toggleSandbox(
  token: string,
  botId: string,
  allowWrite: boolean,
): Promise<SandboxToggleResponse> {
  return request<SandboxToggleResponse>(
    token,
    `/bots/${encodeURIComponent(botId)}/sandbox`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allowWrite }),
    },
  )
}

/** Full URL of the file endpoint — used for downloads. */
export function fileApiUrl(botId: string, session: string, path: string): string {
  const qs = new URLSearchParams({ session, path })
  return `${API_PREFIX}/bots/${encodeURIComponent(botId)}/file?${qs.toString()}`
}

/** Server-side upload cap (#43) — checked client-side too for a fast toast. */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024

/**
 * Upload ONE file into the ROOT of the session sandbox (#43). Raw body PUT
 * (never multipart), same bare-`token` auth as every other call. Overwrites
 * a same-name file; resolves with the listing-shaped entry the server wrote.
 */
export function uploadFile(
  token: string,
  botId: string,
  session: string,
  name: string,
  body: Blob,
): Promise<TreeEntry> {
  const qs = new URLSearchParams({ session })
  return request<TreeEntry>(
    token,
    `/bots/${encodeURIComponent(botId)}/files/${encodeURIComponent(name)}?${qs.toString()}`,
    { method: 'PUT', body },
  )
}

export interface FilePayload {
  blob: Blob
  contentType: string
  /** true when the server sent Content-Disposition: attachment → download only */
  attachment: boolean
}

/** Fetch a workspace file. Throws ApiError on non-2xx. */
export async function fetchFile(
  token: string,
  botId: string,
  session: string,
  path: string,
): Promise<FilePayload> {
  const res = await fetch(fileApiUrl(botId, session, path), {
    headers: { token },
  })
  if (!res.ok) throw await parseError(res)
  const disposition = res.headers.get('Content-Disposition') || ''
  return {
    blob: await res.blob(),
    contentType: res.headers.get('Content-Type') || '',
    attachment: /attachment/i.test(disposition),
  }
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))
