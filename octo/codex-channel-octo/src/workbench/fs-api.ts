/**
 * Workbench filesystem API (#24) — sessions, directory trees, file reads.
 *
 * Everything is served from ONE bot's cwd sandbox: `<baseDir>/<botId>/workspace`
 * (= ManagedBot.cwdBase). Session dirs are the 16-hex sha256-prefix dirs
 * created by cwd-resolver.resolveSessionCwd, with provenance markers under
 * `<cwdBase>/.cc-octo-sessions/<hex>` (JSON `{created, kind}`).
 *
 * Security rules (task contract):
 *  - The `path` parameter is normalized (path.resolve) and REAL-path-checked
 *    (fs.realpathSync); the result must lie strictly inside the session dir
 *    (which itself lives inside the workspace) — violation → 403.
 *  - lstat rejects symlinks as the final path component outright, and the
 *    realpath containment check defeats symlinked INTERMEDIATE components
 *    pointing outside.
 *  - Reads are sandboxed as described below. The ONLY write surface is the
 *    #43 upload endpoint, which creates/overwrites a single file strictly at
 *    a session sandbox ROOT (sanitizeUploadName + resolveUploadPath enforce
 *    the same containment as reads); delete endpoints do not exist. The other
 *    mutation remains the sandbox double-switch on the bot's config.json.
 *  - Tree listing is lazy single-level (never recursive).
 */

import {
  createReadStream,
  lstatSync,
  readdirSync,
  readFileSync,
  realpathSync,
  statSync,
  writeFileSync,
  type Stats,
} from 'node:fs';
import { basename, extname, join, resolve, sep } from 'node:path';
import type { IncomingMessage, ServerResponse } from 'node:http';

/** Session dir names are exactly the cwd-resolver 16-hex sha256 prefix. */
export const SESSION_DIR_RE = /^[0-9a-f]{16}$/;

/** Registry sidecar dir name (cwd-resolver's provenance markers). */
const SESSION_REGISTRY_DIR = '.cc-octo-sessions';

/** Cap the sessions listing; anything beyond flips `truncated`. */
export const MAX_SESSIONS = 500;

/** Text files inline-preview up to 2 MB. */
export const TEXT_INLINE_MAX_BYTES = 2 * 1024 * 1024;

/** Uploaded files (#43) cap at 20 MB raw body; beyond → 413. */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
/** Upload file names are capped at 200 chars (comfortably inside any fs limit). */
export const MAX_UPLOAD_NAME_LEN = 200;
/** Images inline-preview up to 5 MB. */
export const IMAGE_INLINE_MAX_BYTES = 5 * 1024 * 1024;
/** Office documents (docx) inline-preview up to 10 MB; larger → attachment. */
export const DOC_INLINE_MAX_BYTES = 10 * 1024 * 1024;

/** Error carrying the HTTP status the route layer should respond with. */
export class WorkbenchFsError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'WorkbenchFsError';
  }
}

export interface SessionInfo {
  hex: string;
  kind: 'dm' | 'group';
  createdAt: string;
  lastActiveAt: string;
  fileCount: number;
}

export interface TreeEntry {
  name: string;
  type: 'file' | 'dir';
  size: number;
  mtime: string;
}

// ─── Sessions ───────────────────────────────────────────────────────────────

/**
 * Enumerate this bot's session dirs: every 16-hex directory directly under
 * `cwdBase`, enriched with its `.cc-octo-sessions` registry marker (kind +
 * createdAt). Sorted by lastActiveAt desc, capped at MAX_SESSIONS.
 *
 * `fileCount` is a SHALLOW count of regular files directly inside the session
 * dir (a recursive count could walk an arbitrarily deep agent-produced tree on
 * every page load). `lastActiveAt` tracks the dir mtime, which
 * resolveSessionCwd refreshes on every bot turn.
 */
export function listSessions(cwdBase: string): { sessions: SessionInfo[]; truncated: boolean } {
  let names: string[];
  try {
    names = readdirSync(cwdBase);
  } catch {
    return { sessions: [], truncated: false }; // workspace not created yet
  }

  const sessions: SessionInfo[] = [];
  for (const name of names) {
    if (!SESSION_DIR_RE.test(name)) continue;
    const dir = join(cwdBase, name);
    let st: Stats;
    try {
      // lstat: a symlinked hex dir is not a session we created.
      st = lstatSync(dir);
    } catch {
      continue;
    }
    if (!st.isDirectory()) continue;

    const marker = readRegistryMarker(cwdBase, name);
    // fileCount = shallow regular-file count (see header for rationale).
    let fileCount = 0;
    try {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        try {
          if (lstatSync(join(dir, entry.name)).isFile()) fileCount++;
        } catch {
          /* raced away — skip */
        }
      }
    } catch {
      /* unreadable dir — count stays 0 */
    }

    sessions.push({
      hex: name,
      kind: marker?.kind === 'group' ? 'group' : 'dm',
      createdAt: createdAtFor(marker?.created, st),
      lastActiveAt: new Date(st.mtimeMs).toISOString(),
      fileCount,
    });
  }

  sessions.sort((a, b) => (a.lastActiveAt < b.lastActiveAt ? 1 : -1));
  const truncated = sessions.length > MAX_SESSIONS;
  return { sessions: truncated ? sessions.slice(0, MAX_SESSIONS) : sessions, truncated };
}

function readRegistryMarker(
  cwdBase: string,
  hex: string,
): { created?: string; kind?: string } | undefined {
  try {
    const raw = readFileSync(join(cwdBase, SESSION_REGISTRY_DIR, hex), 'utf-8');
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return {
      created: typeof parsed.created === 'string' ? parsed.created : undefined,
      kind: typeof parsed.kind === 'string' ? parsed.kind : undefined,
    };
  } catch {
    return undefined;
  }
}

/**
 * createdAt source of truth is the registry marker's `created` timestamp; fall
 * back to the dir birthtime (when the fs reports it), then ctime.
 */
function createdAtFor(markerCreated: string | undefined, st: Stats): string {
  const fromMarker = validIso(markerCreated);
  if (fromMarker) return fromMarker;
  const ms = st.birthtimeMs > 0 ? st.birthtimeMs : st.ctimeMs;
  return new Date(ms).toISOString();
}

/** True when the string parses as a valid ISO-8601 date. */
function validIso(s: string | undefined): string | undefined {
  if (!s) return undefined;
  const t = Date.parse(s);
  return Number.isNaN(t) ? undefined : s;
}

// ─── Path safety ────────────────────────────────────────────────────────────

/**
 * Normalize `relPath` against the session dir and prove it stays inside.
 * Returns the REAL (symlink-resolved) absolute path.
 *
 * Defense layers, in order:
 *  1. session hex shape check (400 otherwise),
 *  2. lexical resolve() + containment against the session root (403),
 *  3. lstat on the lexical target: missing → 404, symlink → 403 (hard rule:
 *     a symlinked final component is never served, even if it points inward),
 *  4. realpathSync + containment again — defeats symlinked INTERMEDIATE
 *     components that resolve outside the sandbox (403).
 */
export function resolveSandboxPath(cwdBase: string, sessionHex: string, relPath: string): string {
  if (!SESSION_DIR_RE.test(sessionHex)) {
    throw new WorkbenchFsError(400, 'invalid session id');
  }
  if (relPath.includes('\0')) {
    throw new WorkbenchFsError(400, 'invalid path');
  }

  const sessionRoot = join(cwdBase, sessionHex);
  let realRoot: string;
  try {
    realRoot = realpathSync(sessionRoot);
  } catch {
    throw new WorkbenchFsError(404, 'session not found');
  }

  const rel = relPath === '' || relPath === '.' ? '.' : relPath;
  const candidate = resolve(realRoot, rel);
  if (candidate !== realRoot && !candidate.startsWith(realRoot + sep)) {
    // Lexical escape ('../', absolute path, …) — never touch the filesystem.
    throw new WorkbenchFsError(403, 'path escapes the session sandbox');
  }

  let lst: Stats;
  try {
    lst = lstatSync(candidate);
  } catch {
    throw new WorkbenchFsError(404, 'not found');
  }
  if (lst.isSymbolicLink()) {
    throw new WorkbenchFsError(403, 'symlinks are not served');
  }

  let realTarget: string;
  try {
    realTarget = realpathSync(candidate);
  } catch {
    throw new WorkbenchFsError(404, 'not found');
  }
  if (realTarget !== realRoot && !realTarget.startsWith(realRoot + sep)) {
    throw new WorkbenchFsError(403, 'path escapes the session sandbox');
  }
  return realTarget;
}

// ─── Tree ───────────────────────────────────────────────────────────────────

/**
 * Lazy SINGLE-LEVEL listing of one directory inside a session sandbox.
 * Never recurses. Entries are lstat'd (symlinked children report as 'file'
 * with the link's own size — following them is forbidden by the security
 * rules). Dirs sort before files, then by name.
 */
export function listTree(cwdBase: string, sessionHex: string, relPath: string): TreeEntry[] {
  const dir = resolveSandboxPath(cwdBase, sessionHex, relPath);
  let st: Stats;
  try {
    st = statSync(dir);
  } catch {
    throw new WorkbenchFsError(404, 'not found');
  }
  if (!st.isDirectory()) {
    throw new WorkbenchFsError(400, 'path is not a directory');
  }

  const entries: TreeEntry[] = [];
  for (const dirent of readdirSync(dir, { withFileTypes: true })) {
    let es: Stats;
    try {
      es = lstatSync(join(dir, dirent.name));
    } catch {
      continue; // raced away between readdir and lstat
    }
    entries.push({
      name: dirent.name,
      type: es.isDirectory() ? 'dir' : 'file',
      size: es.isFile() ? es.size : 0,
      mtime: new Date(es.mtimeMs).toISOString(),
    });
  }
  entries.sort((a, b) =>
    a.type === b.type ? (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
    : a.type === 'dir' ? -1 : 1,
  );
  return entries;
}

// ─── Upload (#43) ───────────────────────────────────────────────────────────

/** Windows/POSIX-hostile characters an upload name must never carry. */
const UPLOAD_BAD_CHARS = /[<>:"/\\|?*]/;
/** ASCII C0 controls + DEL — never legal in file names. */
const UPLOAD_CTRL_CHARS = /[\u0000-\u001f\u007f]/;

/**
 * Strictly sanitize an uploaded file name (#43). The name targets the ROOT of
 * a session sandbox, so it must be a single plain path component:
 *   - any path separator (either OS) → 400 (no silent path stripping),
 *   - empty / '.' / '..' / containing '..' → 400,
 *   - control chars or the hostile set `<>:"/\|?*` → 400,
 *   - leading/trailing space or dot → 400 (Windows rejects trailing dots),
 *   - longer than MAX_UPLOAD_NAME_LEN → 400.
 * Unicode (CJK) names are allowed. Throws WorkbenchFsError(400).
 */
export function sanitizeUploadName(raw: string): string {
  if (typeof raw !== 'string' || raw.length === 0) {
    throw new WorkbenchFsError(400, 'empty file name');
  }
  if (raw.includes('\0')) {
    throw new WorkbenchFsError(400, 'invalid file name');
  }
  if (raw.includes('/') || raw.includes('\\')) {
    throw new WorkbenchFsError(400, 'file name must not contain path separators');
  }
  if (raw === '.' || raw === '..' || raw.includes('..')) {
    throw new WorkbenchFsError(400, 'file name must not contain ".."');
  }
  if (UPLOAD_CTRL_CHARS.test(raw)) {
    throw new WorkbenchFsError(400, 'file name contains control characters');
  }
  if (UPLOAD_BAD_CHARS.test(raw)) {
    throw new WorkbenchFsError(400, 'file name contains illegal characters');
  }
  if (raw.length > MAX_UPLOAD_NAME_LEN) {
    throw new WorkbenchFsError(400, 'file name too long');
  }
  const first = raw[0];
  const last = raw[raw.length - 1];
  if (first === ' ' || last === ' ' || first === '.' || last === '.') {
    throw new WorkbenchFsError(400, 'file name must not start or end with a space or dot');
  }
  return raw;
}

/**
 * Resolve an UPLOAD target inside a session sandbox root (#43).
 * Same containment discipline as resolveSandboxPath, minus the existence
 * requirement (uploads create files):
 *  1. session hex shape (400), NUL rejected (400),
 *  2. realpath the session root (missing → 404),
 *  3. candidate = root + name, containment-proven by construction (the name
 *     carries no separators),
 *  4. lstat the candidate: symlink → 403 (never overwrite links), directory
 *     → 400 (a plain file is expected); a regular file may be overwritten.
 */
export function resolveUploadPath(cwdBase: string, sessionHex: string, name: string): string {
  if (!SESSION_DIR_RE.test(sessionHex)) {
    throw new WorkbenchFsError(400, 'invalid session id');
  }
  if (name.includes('\0')) {
    throw new WorkbenchFsError(400, 'invalid path');
  }
  let realRoot: string;
  try {
    realRoot = realpathSync(join(cwdBase, sessionHex));
  } catch {
    throw new WorkbenchFsError(404, 'session not found');
  }
  const candidate = join(realRoot, name);
  // Defensive double-check: the root is realpath'd and the name is a single
  // separator-free component, so escape is impossible — assert anyway.
  if (!candidate.startsWith(realRoot + sep)) {
    throw new WorkbenchFsError(403, 'path escapes the session sandbox');
  }
  try {
    const lst = lstatSync(candidate);
    if (lst.isSymbolicLink()) {
      throw new WorkbenchFsError(403, 'symlinks are not overwritable');
    }
    if (lst.isDirectory()) {
      throw new WorkbenchFsError(400, 'a directory already has this name');
    }
  } catch (err) {
    if (err instanceof WorkbenchFsError) throw err;
    // ENOENT → brand-new file; anything else (permissions, …) is a fault.
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
      throw new WorkbenchFsError(500, 'upload target not accessible');
    }
  }
  return candidate;
}

/**
 * Write an uploaded buffer into the session sandbox root (overwrite on name
 * collision) and return the file's listing entry — byte-for-byte the shape
 * `listTree` emits, so the SPA can splice it straight into its view.
 */
export function writeUploadedFile(
  cwdBase: string,
  sessionHex: string,
  name: string,
  data: Buffer,
): TreeEntry {
  const clean = sanitizeUploadName(name);
  const target = resolveUploadPath(cwdBase, sessionHex, clean);
  try {
    writeFileSync(target, data);
  } catch (err) {
    throw new WorkbenchFsError(500, `failed to write uploaded file: ${String(err)}`);
  }
  let st: Stats;
  try {
    st = statSync(target);
  } catch {
    throw new WorkbenchFsError(500, 'upload written but stat failed');
  }
  return {
    name: clean,
    type: 'file',
    size: st.size,
    mtime: new Date(st.mtimeMs).toISOString(),
  };
}

// ─── File streaming ─────────────────────────────────────────────────────────

interface MimeClass {
  type: string;
  cls: 'text' | 'image' | 'document' | 'other';
}

/**
 * Extension → Content-Type + preview class. Text-ish formats (docs, code,
 * data) inline-preview ≤ 2 MB; raster images ≤ 5 MB; Office documents (docx)
 * ≤ 10 MB so the frontend's in-browser renderers (e.g. mammoth) are reachable.
 * SVG is deliberately NOT an inline image class: it is scriptable XML and
 * would be an XSS vector on the workbench origin — it downloads as
 * attachment. Unknown → octet-stream.
 */
const MIME_BY_EXT: Record<string, MimeClass> = {
  '.md': { type: 'text/markdown; charset=utf-8', cls: 'text' },
  '.markdown': { type: 'text/markdown; charset=utf-8', cls: 'text' },
  '.txt': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.log': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.json': { type: 'application/json; charset=utf-8', cls: 'text' },
  '.jsonc': { type: 'application/json; charset=utf-8', cls: 'text' },
  '.csv': { type: 'text/csv; charset=utf-8', cls: 'text' },
  '.tsv': { type: 'text/tab-separated-values; charset=utf-8', cls: 'text' },
  '.xml': { type: 'application/xml; charset=utf-8', cls: 'text' },
  '.yaml': { type: 'text/yaml; charset=utf-8', cls: 'text' },
  '.yml': { type: 'text/yaml; charset=utf-8', cls: 'text' },
  '.toml': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.ini': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.conf': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.html': { type: 'text/html; charset=utf-8', cls: 'text' },
  '.htm': { type: 'text/html; charset=utf-8', cls: 'text' },
  '.js': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.mjs': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.cjs': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.jsx': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.ts': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.tsx': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.py': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.go': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.rs': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.java': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.c': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.h': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.cpp': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.hpp': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.cs': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.rb': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.php': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.sh': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.bash': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.zsh': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.ps1': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.sql': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.css': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.scss': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.less': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.vue': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.svelte': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.swift': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.kt': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.lua': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.pl': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.r': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.proto': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.graphql': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.env': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.patch': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.diff': { type: 'text/plain; charset=utf-8', cls: 'text' },
  '.png': { type: 'image/png', cls: 'image' },
  '.jpg': { type: 'image/jpeg', cls: 'image' },
  '.jpeg': { type: 'image/jpeg', cls: 'image' },
  '.gif': { type: 'image/gif', cls: 'image' },
  '.webp': { type: 'image/webp', cls: 'image' },
  '.bmp': { type: 'image/bmp', cls: 'image' },
  '.ico': { type: 'image/x-icon', cls: 'image' },
  '.avif': { type: 'image/avif', cls: 'image' },
  // Office Open XML word-processing document — inline so the SPA's mammoth
  // renderer can fetch it. Capped at DOC_INLINE_MAX_BYTES (10 MB).
  '.docx': {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    cls: 'document',
  },
};

/** ETag format (contract): "<mtimeMs>:<size>" — quoted. */
export function buildEtag(st: Stats): string {
  return `"${st.mtimeMs}:${st.size}"`;
}

/** If-None-Match matcher: comma-separated list, optional W/ prefix, `*`. */
function ifNoneMatchHits(header: string | undefined, etag: string): boolean {
  if (!header) return false;
  return header.split(',').some((cand) => {
    const t = cand.trim().replace(/^W\//, '');
    return t === '*' || t === etag;
  });
}

/** Parse a single-range `Range: bytes=…` header. null = serve whole file. */
function parseRange(
  header: string | undefined,
  size: number,
): { start: number; end: number } | 'invalid' | null {
  if (!header) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return null; // multi-range / unit ranges → ignore, serve 200 full
  const [, startStr, endStr] = m;
  if (startStr === '' && endStr === '') return null;
  // An empty file has no bytes to serve — every byte range is unsatisfiable.
  // Without this guard `bytes=-N` on size 0 would produce {start:0,end:-1} and
  // make createReadStream throw ERR_OUT_OF_RANGE instead of a clean 416.
  if (size === 0) return 'invalid';
  if (startStr === '') {
    // Suffix form: last N bytes.
    const suffix = Number(endStr);
    if (!Number.isFinite(suffix) || suffix <= 0) return 'invalid';
    const start = Math.max(0, size - suffix);
    return { start, end: size - 1 };
  }
  const start = Number(startStr);
  const end = endStr === '' ? size - 1 : Math.min(Number(endStr), size - 1);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= size) {
    return 'invalid';
  }
  return { start, end };
}

/** RFC 5987-ish Content-Disposition with a sanitized ASCII fallback. */
function dispositionFor(filePath: string): string {
  const name = basename(filePath);
  const ascii = name.replace(/[^\x20-\x7e]/g, '_').replace(/["\\]/g, '_');
  const encoded = encodeURIComponent(name);
  return `attachment; filename="${ascii || 'file'}"; filename*=UTF-8''${encoded}`;
}

/**
 * Stream one sandboxed file to the response: ETag/304, single Range/206,
 * inline-vs-attachment by type and size caps. Throws WorkbenchFsError for
 * route-layer status mapping (403/404/…).
 */
export function serveFile(
  req: IncomingMessage,
  res: ServerResponse,
  cwdBase: string,
  sessionHex: string,
  relPath: string,
): void {
  const target = resolveSandboxPath(cwdBase, sessionHex, relPath);
  let st: Stats;
  try {
    st = statSync(target);
  } catch {
    throw new WorkbenchFsError(404, 'not found');
  }
  if (!st.isFile()) {
    throw new WorkbenchFsError(400, 'path is not a file');
  }

  const etag = buildEtag(st);
  if (ifNoneMatchHits(req.headers['if-none-match'], etag)) {
    res.writeHead(304, { ETag: etag });
    res.end();
    return;
  }

  const info: MimeClass = MIME_BY_EXT[extname(target).toLowerCase()] ?? {
    type: 'application/octet-stream',
    cls: 'other',
  };
  const inline =
    (info.cls === 'text' && st.size <= TEXT_INLINE_MAX_BYTES) ||
    (info.cls === 'image' && st.size <= IMAGE_INLINE_MAX_BYTES) ||
    (info.cls === 'document' && st.size <= DOC_INLINE_MAX_BYTES);

  const headers: Record<string, string | number> = {
    'Content-Type': info.type,
    ETag: etag,
    'Last-Modified': st.mtime.toUTCString(),
    'Accept-Ranges': 'bytes',
    // Operator console over mutable sandbox content — never serve stale.
    'Cache-Control': 'no-cache',
  };
  if (!inline) {
    headers['Content-Disposition'] = dispositionFor(target);
  }

  if (req.method === 'HEAD') {
    headers['Content-Length'] = st.size;
    res.writeHead(200, headers);
    res.end();
    return;
  }

  const range = parseRange(req.headers['range'], st.size);
  if (range === 'invalid') {
    res.writeHead(416, { 'Content-Range': `bytes */${st.size}` });
    res.end();
    return;
  }

  let streamOpts: { start?: number; end?: number } = {};
  if (range) {
    headers['Content-Range'] = `bytes ${range.start}-${range.end}/${st.size}`;
    headers['Content-Length'] = range.end - range.start + 1;
    streamOpts = { start: range.start, end: range.end };
    res.writeHead(206, headers);
  } else {
    headers['Content-Length'] = st.size;
    res.writeHead(200, headers);
  }

  const stream = createReadStream(target, streamOpts);
  stream.on('error', (err) => {
    // Mid-stream races (file truncated/unlinked by the agent) — abort cleanly.
    console.warn(`[workbench] file stream error for ${target}: ${err.message}`);
    stream.destroy();
    res.destroy();
  });
  req.on('close', () => stream.destroy());
  stream.pipe(res);
}
