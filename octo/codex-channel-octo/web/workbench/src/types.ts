/** API contract types — mirror the backend contract verbatim. */

export type SandboxMode = 'read-only' | 'workspace-write'

export interface Bot {
  id: string
  robotUid: string
  ownerUid: string
  sandboxMode: SandboxMode
  allowWorkspaceWrite: boolean
  /** Agent runtime backend. Omitted/'codex' = codex sandbox applies; 'lumo' has no codex sandbox. */
  runtime?: 'codex' | 'lumo'
  connected: boolean
}

export interface BotListResponse {
  bots: Bot[]
}

export type SessionKind = 'dm' | 'group'

export interface Session {
  hex: string
  kind: SessionKind
  createdAt: string
  lastActiveAt: string
  fileCount: number
}

export interface SessionListResponse {
  sessions: Session[]
  truncated: boolean
}

export interface TreeEntry {
  name: string
  type: 'file' | 'dir'
  size: number
  mtime: string
}

export interface TreeResponse {
  entries: TreeEntry[]
}

export interface SandboxToggleResponse {
  ok: boolean
  sandboxMode: string
  allowWorkspaceWrite: boolean
}

export interface ApiErrorResponse {
  error: string
}
