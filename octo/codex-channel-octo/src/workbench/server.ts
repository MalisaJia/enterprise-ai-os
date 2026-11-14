/**
 * Workbench HTTP server (#24) — runs INSIDE the gateway process.
 *
 * It must live here (not a separate entry process) because the sandbox toggle
 * needs direct BotManager access for a per-bot remove→add restart while every
 * other bot keeps its connection.
 *
 * Surface (all routes carry the `/workbench/` prefix — upstream does NO path
 * rewriting):
 *   GET  /workbench/…                        static SPA from web/workbench/dist
 *   GET  /workbench/api/bots                 bot roster (auth: valid user, own bots only)
 *   GET  /workbench/api/bots/:id/sessions    session dirs of one bot (owner only)
 *   GET  /workbench/api/bots/:id/tree        single-level dir listing (owner only)
 *   GET  /workbench/api/bots/:id/file        stream one file        (owner only)
 *   PUT  /workbench/api/bots/:id/files/:name #43 upload one file into the
 *                                              session sandbox root (owner
 *                                              only; raw body ≤ 20 MB, target
 *                                              via ?session=<16hex>)
 *   POST /workbench/api/bots/:id/sandbox     flip the sandbox double-switch
 *
 * Security: every /workbench/api/* call authenticates via the bare `token`
 * header (auth.ts); per-bot routes additionally require caller uid === the
 * bot's ownerUid. File access is jailed to the bot's workspace (fs-api.ts).
 * The ONLY mutation is the sandbox double-switch, written atomically to the
 * bot's config.json and validated before a per-bot restart.
 *
 * Resilience: nothing here may take the message pipeline down — every handler
 * is wrapped, the server's own errors are logged and swallowed, and a listen
 * failure leaves the gateway running without the workbench.
 */

import { createServer, type IncomingMessage, type ServerResponse, type Server } from 'node:http';
import {
  closeSync,
  createReadStream,
  existsSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { basename, dirname, extname, join, resolve, sep } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import type { BotManager } from '../bot-manager.js';
import type { Config } from '../config.js';
import { AuthError, TokenAuthenticator } from './auth.js';
import {
  MAX_UPLOAD_BYTES,
  listSessions,
  listTree,
  serveFile,
  writeUploadedFile,
  WorkbenchFsError,
} from './fs-api.js';

const PREFIX = '/workbench';
const API_PREFIX = '/workbench/api/';
/** POST bodies are tiny ({allowWrite}); anything bigger is a client bug. */
const MAX_BODY_BYTES = 64 * 1024;

/** An upload body crossed the MAX_UPLOAD_BYTES cap — the route answers 413. */
class UploadTooLargeError extends Error {
  constructor() {
    super('upload exceeds the size limit');
    this.name = 'UploadTooLargeError';
  }
}

/** Everything startWorkbenchServer needs, injected for wiring + tests. */
export interface WorkbenchDeps {
  manager: BotManager;
  /** Resolve one bot's effective config by configId; throws when unknown/invalid. */
  resolveBotConfig: (configId: string) => Config;
  /** Octo API base URL, re-resolved at call time (hot-reload safe). */
  apiUrl: () => string;
  /** Directory containing the per-bot subtrees (<baseDir>/<id>/config.json). */
  baseDir: string;
  /** Static asset root. Default: <repoRoot>/web/workbench/dist. */
  staticRoot?: string;
  /** Bind address. Default 0.0.0.0 (upstream docker host-gateway bridge). */
  host?: string;
  /** fetch injection for the auth check (tests). */
  fetchImpl?: typeof fetch;
  /** Auth cache TTL override (tests). */
  authTtlMs?: number;
}

export interface WorkbenchServerHandle {
  server: Server;
  port: number;
  host: string;
  close: () => Promise<void>;
}

/**
 * Repo root as seen from this module. Works both compiled
 * (dist/workbench/server.js) and under vitest (src/workbench/server.ts) —
 * either way it is exactly two directories up.
 */
function defaultRepoRoot(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
}

/** Start the server; resolves once listening, rejects on a listen error. */
export function startWorkbenchServer(
  deps: WorkbenchDeps,
  listenPort: number,
): Promise<WorkbenchServerHandle> {
  const host = deps.host ?? '0.0.0.0';
  const staticRoot = deps.staticRoot ?? join(defaultRepoRoot(), 'web', 'workbench', 'dist');
  const authenticator = new TokenAuthenticator({
    apiUrl: deps.apiUrl,
    fetchImpl: deps.fetchImpl,
    ttlMs: deps.authTtlMs,
  });

  const server = createServer((req, res) => {
    // The workbench must never crash the gateway process — handleRequest is
    // fully wrapped and responds with JSON errors instead of throwing.
    handleRequest(req, res, deps, authenticator, staticRoot).catch((err) => {
      console.error(`[workbench] unhandled handler error: ${String(err)}`);
      try {
        sendJson(res, 500, { error: 'internal error' });
      } catch {
        /* response already committed */
      }
    });
  });

  // A listen/socket-level failure is logged, never propagated to the gateway.
  server.on('error', (err) => {
    console.error(`[workbench] server error: ${String(err)}`);
  });

  return new Promise((resolvePromise, reject) => {
    server.once('error', reject); // listen error (EADDRINUSE, …)
    server.listen(listenPort, host, () => {
      const addr = server.address();
      const port = typeof addr === 'object' && addr ? addr.port : listenPort;
      resolvePromise({
        server,
        port,
        host,
        close: () =>
          new Promise<void>((r) => {
            server.close(() => r());
            // Don't wait for keep-alive sockets to drain — shutdown stays fast.
            server.closeAllConnections?.();
          }),
      });
    });
  });
}

// ─── Routing ────────────────────────────────────────────────────────────────

async function handleRequest(
  req: IncomingMessage,
  res: ServerResponse,
  deps: WorkbenchDeps,
  authenticator: TokenAuthenticator,
  staticRoot: string,
): Promise<void> {
  let url: URL;
  try {
    url = new URL(req.url ?? '/', 'http://workbench.local');
  } catch {
    sendJson(res, 400, { error: 'bad request' });
    return;
  }
  const pathname = url.pathname;

  if (!pathname.startsWith(PREFIX)) {
    sendJson(res, 404, { error: 'not found' });
    return;
  }
  if (pathname === PREFIX) {
    // Convenience redirect so relative asset URLs resolve correctly.
    res.writeHead(302, { Location: `${PREFIX}/` });
    res.end();
    return;
  }

  if (pathname.startsWith(API_PREFIX)) {
    await handleApi(req, res, url, deps, authenticator);
    return;
  }

  if (req.method !== 'GET' && req.method !== 'HEAD') {
    sendJson(res, 405, { error: 'method not allowed' });
    return;
  }
  serveStatic(req, res, pathname, staticRoot);
}

async function handleApi(
  req: IncomingMessage,
  res: ServerResponse,
  url: URL,
  deps: WorkbenchDeps,
  authenticator: TokenAuthenticator,
): Promise<void> {
  try {
    // EVERY /workbench/api/* endpoint authenticates — no exceptions.
    const uid = await authenticator.authenticate(req);

    const segments = url.pathname.slice(API_PREFIX.length).split('/').filter(Boolean);
    const method = req.method ?? 'GET';

    // GET /workbench/api/bots — roster. A valid user is enough, but the
    // roster is FILTERED to the caller's own bots (W4 baseline): non-owned
    // bots must not appear, otherwise the frontend renders toggle controls
    // for bots the caller cannot operate anyway.
    if (method === 'GET' && segments.length === 1 && segments[0] === 'bots') {
      sendJson(res, 200, { bots: buildBotRoster(deps, uid) });
      return;
    }

    if (segments.length === 3 && segments[0] === 'bots') {
      let configId: string;
      try {
        configId = decodeURIComponent(segments[1]);
      } catch {
        // Malformed percent-encoding (e.g. `%E0%A4%A`) → client error, not a
        // server fault; decodeURIComponent throws URIError here.
        sendJson(res, 400, { error: 'bad bot id' });
        return;
      }
      const action = segments[2];
      const bot = deps.manager.get(configId);
      if (!bot) {
        sendJson(res, 404, { error: `bot "${configId}" not found` });
        return;
      }
      authenticator.assertOwner(uid, bot.ownerUid);

      if (method === 'GET' && action === 'sessions') {
        const { sessions, truncated } = listSessions(bot.cwdBase);
        sendJson(res, 200, { sessions, truncated });
        return;
      }
      if (method === 'GET' && action === 'tree') {
        const entries = listTree(bot.cwdBase, url.searchParams.get('session') ?? '', url.searchParams.get('path') ?? '');
        sendJson(res, 200, { entries });
        return;
      }
      if ((method === 'GET' || method === 'HEAD') && action === 'file') {
        serveFile(req, res, bot.cwdBase, url.searchParams.get('session') ?? '', url.searchParams.get('path') ?? '');
        return;
      }
      if (method === 'POST' && action === 'sandbox') {
        await handleSandboxToggle(req, res, deps, configId);
        return;
      }
    }

    // #43: PUT /workbench/api/bots/:id/files/:filename — raw-body upload into
    // the ROOT of the session sandbox named by ?session=<16hex> (the exact
    // root the tree listing reads, so the file appears in the list at once).
    // Auth is identical to every other per-bot route: token → uid → owner.
    if (segments.length === 4 && segments[0] === 'bots' && segments[2] === 'files') {
      let configId: string;
      let rawName: string;
      try {
        configId = decodeURIComponent(segments[1]);
        rawName = decodeURIComponent(segments[3]);
      } catch {
        sendJson(res, 400, { error: 'bad bot id or file name' });
        return;
      }
      const bot = deps.manager.get(configId);
      if (!bot) {
        sendJson(res, 404, { error: `bot "${configId}" not found` });
        return;
      }
      authenticator.assertOwner(uid, bot.ownerUid);

      if (method !== 'PUT') {
        sendJson(res, 405, { error: 'method not allowed' });
        return;
      }

      let data: Buffer;
      try {
        data = await readUploadBody(req, MAX_UPLOAD_BYTES);
      } catch (err) {
        if (err instanceof UploadTooLargeError) {
          // Connection: close — after the 413 is flushed, Node drains and
          // discards the unread body, then closes. We never destroy the
          // socket here: an early RST while the client is still uploading
          // makes fetch() report a network error instead of the 413.
          res.setHeader('Connection', 'close');
          sendJson(res, 413, {
            error: `file exceeds the ${MAX_UPLOAD_BYTES / (1024 * 1024)} MB upload limit`,
          });
        } else {
          sendJson(res, 400, { error: 'failed to read the request body' });
        }
        return;
      }

      // sanitizeUploadName / resolveUploadPath throw WorkbenchFsError, which
      // the catch below maps to 400/403/404/500.
      const entry = writeUploadedFile(
        bot.cwdBase,
        url.searchParams.get('session') ?? '',
        rawName,
        data,
      );
      sendJson(res, 200, entry);
      return;
    }

    sendJson(res, 404, { error: 'unknown workbench endpoint' });
  } catch (err) {
    if (err instanceof AuthError) {
      sendJson(res, err.status, { error: err.message });
      return;
    }
    if (err instanceof WorkbenchFsError) {
      sendJson(res, err.status, { error: err.message });
      return;
    }
    if (err instanceof UploadTooLargeError) {
      res.setHeader('Connection', 'close'); // drain + close after the 413
      sendJson(res, 413, { error: err.message });
      return;
    }
    throw err;
  }
}

/**
 * Roster row builder for GET /workbench/api/bots (contract field names).
 * FILTERED to `ownerUid === uid` (W4 baseline) — the caller only ever sees
 * their own bots.
 */
function buildBotRoster(
  deps: WorkbenchDeps,
  uid: string,
): Array<{
  id: string;
  robotUid: string;
  ownerUid: string;
  sandboxMode: 'read-only' | 'workspace-write';
  allowWorkspaceWrite: boolean;
  runtime: 'codex' | 'lumo';
  connected: boolean;
}> {
  const bots = [];
  for (const { configId, bot } of deps.manager.snapshot()) {
    // Only the caller's OWN bots are listed; an ownerless bot (undefined) is
    // never equal to an authenticated uid, so it stays invisible.
    if (bot.ownerUid !== uid) continue;
    // Current sandbox double-switch from the effective config on disk. A
    // momentarily unreadable config degrades to the safe defaults rather than
    // hiding the bot.
    let sandboxMode: 'read-only' | 'workspace-write' = 'read-only';
    let allowWorkspaceWrite = false;
    let runtime: 'codex' | 'lumo' = 'codex';
    try {
      const cfg = deps.resolveBotConfig(configId);
      sandboxMode = cfg.sdk.sandboxMode === 'workspace-write' ? 'workspace-write' : 'read-only';
      allowWorkspaceWrite = cfg.sdk.allowWorkspaceWrite === true;
      runtime = cfg.runtime ?? 'codex';
    } catch {
      /* keep defaults */
    }
    bots.push({
      id: configId,
      robotUid: bot.robotUid,
      ownerUid: bot.ownerUid ?? '',
      sandboxMode,
      allowWorkspaceWrite,
      runtime,
      // A bot exists in the manager only after connect() succeeded; a later
      // socket blip self-heals via the gateway's reconnect. Best-effort flag.
      connected: deps.manager.has(configId),
    });
  }
  return bots;
}

// ─── POST /workbench/api/bots/:id/sandbox ───────────────────────────────────

async function handleSandboxToggle(
  req: IncomingMessage,
  res: ServerResponse,
  deps: WorkbenchDeps,
  configId: string,
): Promise<void> {
  const body = await readBody(req);
  let allowWrite: unknown;
  try {
    allowWrite = (JSON.parse(body) as Record<string, unknown>).allowWrite;
  } catch {
    sendJson(res, 400, { error: 'body must be JSON: {"allowWrite": true|false}' });
    return;
  }
  if (typeof allowWrite !== 'boolean') {
    sendJson(res, 400, { error: 'allowWrite must be a boolean' });
    return;
  }

  // The codex sandbox double-switch is meaningless for a non-codex runtime
  // (e.g. lumo, which owns its own execution) — reject it outright.
  let runtime: 'codex' | 'lumo' = 'codex';
  try {
    runtime = deps.resolveBotConfig(configId).runtime ?? 'codex';
  } catch {
    /* unreadable config falls through to the write + 422 validation below */
  }
  if (runtime !== 'codex') {
    sendJson(res, 400, { error: `sandbox toggle applies only to codex-runtime bots (this bot is '${runtime}')` });
    return;
  }

  // Double-switch semantics must match config.ts exactly: BOTH fields move
  // together (true → workspace-write+true, false → read-only+false). Setting
  // only one would be silently downgraded by resolveSandbox.
  const sandboxMode = allowWrite ? 'workspace-write' : 'read-only';
  const perBotConfigPath = join(deps.baseDir, configId, 'config.json');

  let original: string | undefined;
  let parsed: Record<string, unknown>;
  try {
    original = readFileSync(perBotConfigPath, 'utf-8');
    parsed = JSON.parse(original) as Record<string, unknown>;
  } catch (err) {
    if (original === undefined) {
      // File missing — start from an empty per-bot config.
      parsed = {};
      original = undefined;
    } else {
      sendJson(res, 409, { error: `per-bot config.json is unreadable: ${String(err)}` });
      return;
    }
  }

  const sdk =
    parsed.sdk !== null && typeof parsed.sdk === 'object' && !Array.isArray(parsed.sdk)
      ? (parsed.sdk as Record<string, unknown>)
      : {};
  sdk.sandboxMode = sandboxMode;
  sdk.allowWorkspaceWrite = allowWrite;
  parsed.sdk = sdk;
  const nextJson = `${JSON.stringify(parsed, null, 2)}\n`;

  // Atomic swap: temp file (same dir → rename is atomic) + fsync + rename.
  atomicWriteFile(perBotConfigPath, nextJson);

  // Re-run the boot-time validation on the merged config. If the new state is
  // invalid for ANY reason, roll back — a bad config must never take effect.
  try {
    deps.resolveBotConfig(configId);
  } catch (err) {
    try {
      if (original !== undefined) {
        atomicWriteFile(perBotConfigPath, original);
      } else {
        // There was no per-bot file before — restore that exact state.
        unlinkQuietly(perBotConfigPath);
      }
    } catch (rollbackErr) {
      console.error(`[workbench] sandbox rollback failed for "${configId}": ${String(rollbackErr)}`);
    }
    sendJson(res, 422, { error: `new sandbox config rejected validation: ${String(err)}` });
    return;
  }

  // Per-bot restart through the manager's serial queue: remove→add. Only THIS
  // bot reconnects; siblings keep their connections. (Hot-reload reconcile
  // only diffs the global bot SET, so a per-bot change needs this restart.)
  try {
    await deps.manager.removeBot(configId);
    await deps.manager.addBot(configId);
  } catch (err) {
    // The new config is persisted and valid, but the bot did not come back —
    // surface it; the operator can retry or fix the config.
    sendJson(res, 502, { error: `sandbox applied but restart failed: ${String(err)}` });
    return;
  }

  sendJson(res, 200, { ok: true, sandboxMode, allowWorkspaceWrite: allowWrite });
}

/**
 * Atomic file write: temp file in the SAME directory (cross-device rename is
 * not atomic), fsync the content, rename over the target. Mode 0600 — the
 * per-bot config carries the botToken.
 */
export function atomicWriteFile(filePath: string, content: string): void {
  mkdirSync(dirname(filePath), { recursive: true });
  const tmp = join(
    dirname(filePath),
    `.${basename(filePath)}.${process.pid}.${randomUUID()}.tmp`,
  );
  const fd = openSync(tmp, 'w', 0o600);
  try {
    writeFileSync(fd, content);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  try {
    renameSync(tmp, filePath);
  } catch (err) {
    unlinkQuietly(tmp);
    throw err;
  }
}

function unlinkQuietly(p: string): void {
  try {
    unlinkSync(p);
  } catch {
    /* best effort */
  }
}

async function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolvePromise, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolvePromise(Buffer.concat(chunks).toString('utf-8')));
    req.on('error', reject);
  });
}

/**
 * #43: read a RAW upload body, capped at `maxBytes`. Streams into memory and
 * stops BUFFERING the moment the cap is crossed (a Content-Length header —
 * when present and honest — short-circuits before buffering any byte). The
 * stream is deliberately NOT paused/destroyed: bytes keep flowing and are
 * discarded until the request ends, so the 413 the route sends is actually
 * delivered (destroying a socket with unread input produces a TCP RST that
 * discards the response on the client side). Rejects with UploadTooLargeError.
 */
async function readUploadBody(req: IncomingMessage, maxBytes: number): Promise<Buffer> {
  const declared = Number(req.headers['content-length']);
  if (Number.isFinite(declared) && declared > maxBytes) {
    // The honest Content-Length already proves the body is oversized. Do NOT
    // reject before the body is drained: the route ends the response the
    // moment it sees the rejection, and closing a socket that still has
    // unread input makes Node send a TCP RST (at least on Windows) which
    // discards the 413 on the client side — fetch() then reports a network
    // error instead of the status code. Discard bytes until `end`, THEN
    // reject, so the 413 is delivered and the connection closes cleanly.
    return new Promise((_resolve, reject) => {
      req.on('data', () => {
        /* over cap — consume and discard */
      });
      req.on('end', () => reject(new UploadTooLargeError()));
      req.on('error', (err) => reject(err));
    });
  }
  return new Promise((resolvePromise, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    let aborted = false;
    req.on('data', (chunk: Buffer) => {
      if (aborted) return; // over cap — discard until end, then clean close
      size += chunk.length;
      if (size > maxBytes) {
        aborted = true;
        reject(new UploadTooLargeError());
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (!aborted) resolvePromise(Buffer.concat(chunks));
    });
    req.on('error', (err) => {
      if (!aborted) reject(err);
    });
  });
}

// ─── Static SPA hosting ─────────────────────────────────────────────────────

/** Content types for the built frontend assets. */
const STATIC_MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.txt': 'text/plain; charset=utf-8',
  '.wasm': 'application/wasm',
};

/**
 * Serve `GET /workbench/<rel>` from `staticRoot` (= web/workbench/dist).
 * Unknown paths fall back to index.html (SPA routing); a missing dist dir is
 * a 503 with a build hint. Symlinks and containment escapes are refused.
 */
function serveStatic(
  req: IncomingMessage,
  res: ServerResponse,
  pathname: string,
  staticRoot: string,
): void {
  if (!existsSync(staticRoot)) {
    sendText(
      res,
      503,
      'Workbench UI not built: build the frontend into web/workbench/dist ' +
        '(this server serves the SPA from there).',
    );
    return;
  }

  let rel = pathname.slice(`${PREFIX}/`.length);
  try {
    rel = decodeURIComponent(rel);
  } catch {
    sendJson(res, 400, { error: 'bad path' });
    return;
  }
  if (rel === '') rel = 'index.html';

  const target = resolvePathInside(staticRoot, rel);
  const chosen = pickStaticFile(target, staticRoot);
  if (!chosen) {
    sendJson(res, 404, { error: 'not found' });
    return;
  }

  const mime = STATIC_MIME[extname(chosen).toLowerCase()] ?? 'application/octet-stream';
  let st;
  try {
    st = statSync(chosen);
  } catch {
    sendJson(res, 404, { error: 'not found' });
    return;
  }
  res.writeHead(200, {
    'Content-Type': mime,
    'Content-Length': st.size,
    // SPA builds change on deploy; never cache across them.
    'Cache-Control': 'no-cache',
    ETag: `"${createHash('sha256').update(`${st.mtimeMs}:${st.size}:${chosen}`).digest('hex').slice(0, 32)}"`,
  });
  if (req.method === 'HEAD') {
    res.end();
    return;
  }
  const stream = createReadStream(chosen);
  stream.on('error', () => {
    stream.destroy();
    res.destroy();
  });
  stream.pipe(res);
}

/**
 * Resolve `rel` inside `staticRoot` and prove containment (defeats `/../`
 * URL traversal). Returns the realpath, or undefined on any escape/error.
 * Symlinks are refused — a built frontend never needs them.
 */
function resolvePathInside(staticRoot: string, rel: string): string | undefined {
  try {
    const candidate = resolve(staticRoot, rel);
    if (candidate !== staticRoot && !candidate.startsWith(staticRoot + sep)) {
      return undefined;
    }
    const lst = lstatSync(candidate);
    if (lst.isSymbolicLink()) return undefined;
    const real = realpathSync(candidate);
    const realRoot = realpathSync(staticRoot);
    if (real !== realRoot && !real.startsWith(realRoot + sep)) return undefined;
    return real;
  } catch {
    return undefined;
  }
}

/**
 * SPA fallback: the resolved file when it exists (regular file); otherwise
 * index.html. A directory path also falls back to index.html.
 */
function pickStaticFile(target: string | undefined, staticRoot: string): string | undefined {
  if (target) {
    try {
      if (statSync(target).isFile()) return target;
    } catch {
      /* fall through to index.html */
    }
  }
  return resolvePathInside(staticRoot, 'index.html');
}

// ─── Small helpers ──────────────────────────────────────────────────────────

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  if (res.writableEnded || res.headersSent) return;
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(payload),
    'Cache-Control': 'no-store',
  });
  res.end(payload);
}

function sendText(res: ServerResponse, status: number, text: string): void {
  if (res.writableEnded || res.headersSent) return;
  res.writeHead(status, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(text);
}

