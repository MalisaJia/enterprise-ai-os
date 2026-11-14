/**
 * Lumo Bridge — Lumo Work invocation over its HTTP SSE API.
 * Outputs AsyncIterable<string> — does not know about the Octo API.
 *
 * This is the `runtime: 'lumo'` counterpart of agent-bridge's `queryAgent`. Its
 * signature is intentionally identical so index.ts can swap the two behind a
 * single `config.runtime` branch. Lumo owns its own persona server-side, so the
 * codex security-prefix / AGENTS.md machinery does NOT apply here; we forward
 * the pre-assembled user message verbatim and stream the reply back.
 *
 * HTTP style mirrors src/octo/api.ts (native fetch, no extra deps).
 */

import { randomUUID } from 'node:crypto';
import { mkdir, open, rename } from 'node:fs/promises';
import { join as pathJoin } from 'node:path';
import type { Config } from './config.js';
import { resolveSessionCwd } from './cwd-resolver.js';
import type { SessionCtx } from './cwd-resolver.js';
import { fetchInboundImageBytes } from './media-inbound.js';

/**
 * Image attachment forwarded to Lumo's native multimodal channel (#86 lumo).
 * Mirrors Lumo's AttachmentIn schema (camelCase JSON): an upload id + the
 * /uploads/<hex>.<ext> relative URL the Lumo server can read back.
 */
export interface LumoAttachment {
  id: string;
  url: string;
  fileName?: string;
  mimeType?: string;
}

/** Timeout for the (non-streaming) conversation-create request. */
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
/** Idle timeout: reset on every received byte. Default 30s. */
const DEFAULT_IDLE_TIMEOUT_MS = 30_000;
/** Absolute per-turn ceiling. Default 15min. */
const DEFAULT_MAX_TURN_MS = 900_000;
/** Timeout for a single artifact download. */
const ARTIFACT_TIMEOUT_MS = 60_000;
/** Timeout for a single image upload to Lumo's /api/uploads. */
const UPLOAD_TIMEOUT_MS = 60_000;

/** Lumo-side upload cap is 10MB; bridge-side fetch cap is 5MB (MAX_IMAGE_BYTES),
 *  so anything that passed the fetch fits the upload — no second check needed. */

/**
 * Download one inbound Octo image URL and forward the bytes to Lumo's
 * POST /api/uploads (multipart/form-data, field `file`), returning the
 * attachment reference to ride along on the next /api/chat/stream request.
 *
 * Download hardening (SSRF / per-hop token scoping / MIME allowlist / size cap)
 * is delegated to fetchInboundImageBytes so the codex and lumo runtimes share
 * one security envelope. On ANY failure returns `{ error }` — the caller falls
 * back to the plain URL text marker (degrades to pre-#86 behaviour).
 */
export async function uploadLumoImage(params: {
  url: string;
  baseUrl: string;
  botToken: string;
  apiUrl: string;
}): Promise<{ attachment: LumoAttachment } | { error: string }> {
  const { url, baseUrl, botToken, apiUrl } = params;

  const fetched = await fetchInboundImageBytes({ url, botToken, apiUrl });
  if ('error' in fetched) return { error: fetched.error };

  // Lumo validates extension against content-type (double check on the
  // server), so name the file with the extension implied by the MIME we
  // verified during download.
  const fileName = `${randomUUID()}.${fetched.ext}`;
  const form = new FormData();
  form.append(
    'file',
    new Blob([new Uint8Array(fetched.bytes)], { type: fetched.mime }),
    fileName,
  );

  let resp: Response;
  try {
    resp = await fetch(`${baseUrl}/api/uploads`, {
      method: 'POST',
      body: form,
      signal: AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
    });
  } catch (err) {
    return { error: `上传失败: ${String(err)}` };
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    return { error: `上传失败 HTTP ${resp.status}${text ? `: ${text.slice(0, 200)}` : ''}` };
  }

  const data = (await resp.json().catch(() => ({}))) as {
    id?: unknown;
    url?: unknown;
    fileName?: unknown;
    mimeType?: unknown;
  };
  if (typeof data.id !== 'string' || typeof data.url !== 'string') {
    return { error: '上传响应缺少 id/url' };
  }
  return {
    attachment: {
      id: data.id,
      url: data.url,
      ...(typeof data.fileName === 'string' ? { fileName: data.fileName } : {}),
      ...(typeof data.mimeType === 'string' ? { mimeType: data.mimeType } : {}),
    },
  };
}

/**
 * Artifact references inside Lumo's output text: `/uploads/<32 hex>.<ext>`.
 * Matched against the accumulated reply so produced files can be reclaimed into
 * the session sandbox after the turn completes.
 */
const ARTIFACT_RE = /\/uploads\/[0-9a-f]{32}\.[A-Za-z0-9]+/g;

/**
 * Thrown internally when the stream request reports the conversation is gone
 * (HTTP 404 or a body carrying 「会话不存在」). Lets queryLumo distinguish a
 * stale-resume from an ordinary failure so it can recreate + retry exactly once.
 */
class LumoSessionMissing extends Error {
  constructor(message = '会话不存在') {
    super(message);
    this.name = 'LumoSessionMissing';
  }
}

/**
 * Parse one SSE line. Only `data:`-prefixed JSON lines are handled; `:keepalive`
 * comment lines and `event:`/`id:` fields fall through untouched. Recognised
 * event types: `chunk` (yield its content), `error` (throw), `done` (terminate).
 * Everything else (modelSwitch / autoModel / sources / status / searchNotice /
 * unknown) is ignored. Sets `state.terminal` on the `done` event.
 */
function* processLine(
  line: string,
  state: { terminal: boolean },
): Generator<string> {
  if (!line.startsWith('data:')) return; // ignores ':keepalive' comments + event:/id:
  const payload = line.slice(5).replace(/^ /, '');
  if (!payload) return;
  let evt: Record<string, unknown>;
  try {
    evt = JSON.parse(payload) as Record<string, unknown>;
  } catch {
    return; // malformed data line — skip rather than fail the turn
  }
  switch (evt.type) {
    case 'chunk': {
      const content = typeof evt.content === 'string' ? evt.content : '';
      if (content) yield content;
      break;
    }
    case 'error': {
      const message = typeof evt.message === 'string' ? evt.message : 'lumo stream error';
      if (message.includes('会话不存在')) throw new LumoSessionMissing(message);
      throw new Error(message);
    }
    case 'done':
      state.terminal = true;
      break;
    default:
      // modelSwitch / autoModel / sources / status / searchNotice / unknown.
      break;
  }
}

/** `yyyyMMdd-HHmmss` in local time — used for reclaimed artifact filenames. */
function formatStamp(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
    `-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  );
}

/**
 * Download every `/uploads/<hash>.<ext>` reference in `fullText` into `cwd`.
 * Each file streams to a `.lumo-dl.part` temp then renames to
 * `Lumo-<yyyyMMdd-HHmmss>-<uuid4hex>.<ext>`. ANY failure is logged and skipped
 * — reclaiming artifacts must never fail the turn.
 */
async function reclaimArtifacts(baseUrl: string, cwd: string, fullText: string): Promise<void> {
  const matches = fullText.match(ARTIFACT_RE);
  if (!matches || matches.length === 0) return;
  const seen = new Set<string>();
  for (const path of matches) {
    if (seen.has(path)) continue;
    seen.add(path);
    try {
      const resp = await fetch(`${baseUrl}${path}`, {
        signal: AbortSignal.timeout(ARTIFACT_TIMEOUT_MS),
      });
      if (!resp.ok || !resp.body) {
        console.error(
          `[codex-channel-octo] lumo artifact ${path} download failed (${resp.status})`,
        );
        continue;
      }
      const ext = path.slice(path.lastIndexOf('.') + 1);
      const name =
        `Lumo-${formatStamp(new Date())}-${randomUUID().replace(/-/g, '').slice(0, 4)}.${ext}`;
      await mkdir(cwd, { recursive: true });
      const partPath = pathJoin(cwd, '.lumo-dl.part');
      const fh = await open(partPath, 'w');
      try {
        const reader = resp.body.getReader();
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          if (value) await fh.write(value);
        }
      } finally {
        await fh.close();
      }
      await rename(partPath, pathJoin(cwd, name));
    } catch (err) {
      console.error(
        `[codex-channel-octo] lumo artifact reclaim failed for ${path}: ${String(err)}`,
      );
    }
  }
}

/**
 * Query Lumo Work with the given user message.
 *
 * - cwd = `resolveSessionCwd(config.cwdBase ?? config.cwd, sessionCtx)` — the
 *   same per-session sandbox codex uses, so reclaimed artifacts land there.
 * - Session: with no `opts.resume`, POST `{baseUrl}/api/conversations` (JSON
 *   `{}`, camelCase, no auth header) to mint an id, then report it via
 *   `opts.onSessionId`. With a resume id, it is used directly as the
 *   conversationId. If the stream request reports the conversation is gone
 *   (404 / 「会话不存在」), `opts.onResumeFailed()` fires, a fresh conversation is
 *   created, and the turn retries ONCE with `opts.fallbackRetryPrompt ??
 *   userMessage`.
 * - Stream: POST `{baseUrl}/api/chat/stream` with `{conversationId, content}`
 *   and `Accept: text/event-stream`; when `opts.attachments` is non-empty the
 *   body additionally carries `attachments` (camelCase, Lumo AttachmentIn
 *   shape) so Lumo's native multimodal channel sees the images. The retry
 *   path re-sends the SAME attachments — the stale-resume recovery must not
 *   silently drop the images. Parse SSE `data:` JSON events (see processLine).
 *   A stream that ends without a `done`/`error` event throws.
 * - Timeouts: an AbortController enforces an idle timeout (reset on every byte,
 *   default 30000ms) and an absolute ceiling (default 900000ms), both
 *   overridable via `config.lumo.idleTimeoutMs` / `maxTurnMs`. Abort throws.
 *
 * @param userMessage - Pre-assembled user prompt body (caller-sanitized).
 * @param config - Application config (lumo.* + cwdBase/cwd used).
 * @param sessionCtx - Per-session routing for cwd isolation.
 * @param onToolUse - Accepted for signature parity with queryAgent; Lumo's SSE
 *   protocol carries no per-tool events, so it is never invoked.
 * @param opts - session options (identical shape to queryAgent).
 * @yields the reply text chunks as they stream in.
 */
export async function* queryLumo(
  userMessage: string,
  config: Config,
  sessionCtx?: SessionCtx,
  onToolUse?: (toolName: string, toolInput?: unknown) => void,
  opts?: {
    resume?: string;
    onSessionId?: (id: string) => void;
    groupInstructions?: string;
    onResumeFailed?: () => void;
    fallbackRetryPrompt?: string;
    attachments?: LumoAttachment[];
  },
): AsyncIterable<string> {
  // Lumo maintains its own persona server-side, so opts.groupInstructions is
  // deliberately ignored here (no AGENTS.md / security-prefix injection).
  if (onToolUse) {
    // Lumo's SSE stream emits no per-tool events; the callback is accepted only
    // for signature parity with queryAgent and is intentionally never invoked.
  }

  const cwdBase = config.cwdBase ?? config.cwd;
  const cwd = sessionCtx ? resolveSessionCwd(cwdBase, sessionCtx) : cwdBase;

  const lumo = config.lumo;
  if (!lumo?.baseUrl) {
    throw new Error('queryLumo requires config.lumo.baseUrl (runtime=lumo)');
  }
  const baseUrl = lumo.baseUrl.replace(/\/+$/, '');
  const idleTimeoutMs = lumo.idleTimeoutMs ?? DEFAULT_IDLE_TIMEOUT_MS;
  const maxTurnMs = lumo.maxTurnMs ?? DEFAULT_MAX_TURN_MS;

  /** Mint a fresh conversation id: POST /api/conversations (JSON {}, no auth). */
  const createConversation = async (): Promise<string> => {
    const resp = await fetch(`${baseUrl}/api/conversations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
      signal: AbortSignal.timeout(DEFAULT_REQUEST_TIMEOUT_MS),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(
        `Lumo createConversation failed (${resp.status}): ${text || resp.statusText}`,
      );
    }
    const data = (await resp.json().catch(() => ({}))) as { id?: unknown };
    const id = typeof data.id === 'string' ? data.id : undefined;
    if (!id) throw new Error('Lumo createConversation returned no id');
    return id;
  };

  /**
   * Stream one turn. Yields chunk contents as they arrive; throws
   * LumoSessionMissing when the conversation is reported gone, an Error on an
   * `error` event / non-ok status / missing terminal event, or on timeout abort.
   *
   * `attachments` (when non-empty) rides in the JSON body as camelCase
   * `attachments` — Lumo persists the metadata with the user message and its
   * pipeline turns the referenced files into vision-model image_url parts.
   */
  async function* streamOnce(
    convId: string,
    content: string,
    attachments?: LumoAttachment[],
  ): AsyncGenerator<string> {
    const controller = new AbortController();
    const { signal } = controller;
    let abortReason: 'idle' | 'absolute' = 'idle';
    // Rejects when either timer aborts. A no-op catch keeps it from surfacing an
    // unhandled rejection when it fires while we are not actively racing it.
    const abortP = new Promise<never>((_resolve, reject) => {
      signal.addEventListener(
        'abort',
        () => reject(new Error(`Lumo stream aborted: ${abortReason} timeout`)),
        { once: true },
      );
    });
    abortP.catch(() => {});

    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const resetIdle = (): void => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        abortReason = 'idle';
        controller.abort();
      }, idleTimeoutMs);
    };
    const absTimer = setTimeout(() => {
      abortReason = 'absolute';
      controller.abort();
    }, maxTurnMs);
    resetIdle();
    const cleanup = (): void => {
      if (idleTimer) clearTimeout(idleTimer);
      clearTimeout(absTimer);
    };

    try {
      const resp = await Promise.race([
        fetch(`${baseUrl}/api/chat/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify({
            conversationId: convId,
            content,
            ...(attachments && attachments.length > 0 ? { attachments } : {}),
          }),
          signal,
        }),
        abortP,
      ]);

      if (resp.status === 404) throw new LumoSessionMissing();
      if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        if (text.includes('会话不存在')) throw new LumoSessionMissing(text);
        throw new Error(
          `Lumo stream request failed (${resp.status}): ${text || resp.statusText}`,
        );
      }
      if (!resp.body) throw new Error('Lumo stream response has no body');

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      const state = { terminal: false };
      let buffer = '';
      let sawTerminal = false;

      try {
        while (!sawTerminal) {
          const { value, done } = await Promise.race([reader.read(), abortP]);
          if (done) break;
          resetIdle(); // any byte arrival resets the idle clock
          if (value) buffer += decoder.decode(value, { stream: true });
          let nl: number;
          while ((nl = buffer.indexOf('\n')) >= 0) {
            const raw = buffer.slice(0, nl);
            buffer = buffer.slice(nl + 1);
            const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
            for (const out of processLine(line, state)) yield out;
            if (state.terminal) {
              sawTerminal = true;
              break;
            }
          }
        }
        // Flush a trailing line that arrived without a final newline.
        if (!sawTerminal && buffer.length > 0) {
          const line = buffer.endsWith('\r') ? buffer.slice(0, -1) : buffer;
          for (const out of processLine(line, state)) yield out;
          if (state.terminal) sawTerminal = true;
        }
      } finally {
        try {
          reader.releaseLock();
        } catch {
          /* ignore */
        }
      }

      if (!sawTerminal) {
        throw new Error('Lumo stream ended without a done/error event');
      }
    } finally {
      cleanup();
    }
  }

  const safeOnSessionId = (id: string): void => {
    if (!opts?.onSessionId) return;
    try {
      opts.onSessionId(id);
    } catch (err) {
      console.error(`[codex-channel-octo] onSessionId callback threw: ${String(err)}`);
    }
  };

  let conversationId = opts?.resume;
  if (!conversationId) {
    conversationId = await createConversation();
    safeOnSessionId(conversationId);
  }

  const accumulated: string[] = [];
  let retried = false;

  // Drain one stream, teeing chunks into `accumulated` for post-turn reclaim.
  const drain = async function* (
    convId: string,
    content: string,
    attachments?: LumoAttachment[],
  ): AsyncGenerator<string> {
    for await (const chunk of streamOnce(convId, content, attachments)) {
      accumulated.push(chunk);
      yield chunk;
    }
  };

  try {
    yield* drain(conversationId, userMessage, opts?.attachments);
  } catch (err) {
    if (err instanceof LumoSessionMissing && !retried) {
      retried = true;
      console.error(
        `[codex-channel-octo] lumo conversation missing — recreating and retrying once: ${String(err)}`,
      );
      try {
        opts?.onResumeFailed?.();
      } catch (cbErr) {
        console.error(`[codex-channel-octo] onResumeFailed callback threw: ${String(cbErr)}`);
      }
      conversationId = await createConversation();
      safeOnSessionId(conversationId);
      // The retried turn produces the authoritative output for artifact reclaim.
      accumulated.length = 0;
      // The recovery turn re-sends the SAME attachments — a stale-resume retry
      // must not silently drop the images the user asked about.
      yield* drain(
        conversationId,
        opts?.fallbackRetryPrompt ?? userMessage,
        opts?.attachments,
      );
    } else {
      throw err;
    }
  }

  // Artifact reclaim runs after the terminal event and before the generator
  // returns; it never fails the turn (all errors are logged inside).
  await reclaimArtifacts(baseUrl, cwd, accumulated.join(''));
}
