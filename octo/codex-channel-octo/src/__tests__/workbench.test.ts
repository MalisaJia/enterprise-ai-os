/**
 * Workbench backend tests (#24).
 *
 * Coverage:
 *  - Auth: missing/invalid token → 401; non-owner bot access → 403; 60s token
 *    cache (one upstream call per token within TTL); malformed bot id → 400.
 *  - Bot roster contract shape (GET /workbench/api/bots) — own bots only.
 *  - Sessions listing from the cwd-resolver layout (16-hex dirs + markers).
 *  - Path traversal protection: lexical escapes, absolute paths, symlink and
 *    junction escapes all refused (403); invalid session (400); missing (404).
 *  - File streaming: inline text, ETag/If-None-Match → 304, Range → 206,
 *    empty file + suffix Range → 416, .docx inline (no attachment),
 *    attachment disposition for unknown types.
 *  - Sandbox toggle: double-switch semantics, ATOMIC write (temp+rename, no
 *    leftover temp files), validation failure rolls back, restart via
 *    removeBot→addBot.
 *  - Upload (#43): PUT …/files/:name writes into the session sandbox root
 *    (200 + listing-shaped entry), overwrites, shows up in the tree, rejects
 *    hostile names (400), missing/invalid session (400/404), >20MB (413),
 *    non-owner (403), wrong method (405); never escapes the sandbox.
 *  - Static hosting: SPA fallback, 503 when dist is missing, traversal-safe.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { BotManager, type ManagedBot } from '../bot-manager.js';
import type { Config } from '../config.js';
import { loadConfig, resolveBotConfigs } from '../config.js';
import { resolveSessionCwd } from '../cwd-resolver.js';
import { startWorkbenchServer, type WorkbenchServerHandle } from '../workbench/server.js';
import { extractUid } from '../workbench/auth.js';

// ─── Fixtures ───────────────────────────────────────────────────────────────

const OWNER_UID = 'u-owner';
const STRANGER_UID = 'u-stranger';
const OWNER_TOKEN = 'tok-owner';
const STRANGER_TOKEN = 'tok-stranger';

let base: string; // <tmp>/base — the workbench baseDir (per-bot subtrees)
let wsRoot: string; // bot1's workspace (<base>/bot1/workspace)
let handle: WorkbenchServerHandle;
let upstreamCalls: number;
let upstreamUidByToken: Record<string, string>;
let startCalls: number;

function fakeFetch(): typeof fetch {
  return (async (url: URL | string, init?: RequestInit) => {
    upstreamCalls++;
    const u = String(url);
    expect(u).toBe('http://127.0.0.1:28080/v1/user/current');
    const headers = (init?.headers ?? {}) as Record<string, string>;
    expect(headers.token).toBeTruthy(); // bare `token` header, not Authorization
    const uid = upstreamUidByToken[headers.token];
    if (!uid) {
      return new Response(JSON.stringify({ code: 401, msg: 'token invalid' }), { status: 401 });
    }
    return new Response(JSON.stringify({ uid, name: 'Test User' }), { status: 200 });
  }) as typeof fetch;
}

function fakeRouter(): ManagedBot['router'] {
  const known = new Set<string>();
  return {
    registerKnownBot: (uid: string) => known.add(uid),
    unregisterKnownBot: (uid: string) => known.delete(uid),
  } as unknown as ManagedBot['router'];
}

function makeBot(configId: string): ManagedBot {
  return {
    robotUid: `robot-${configId}`,
    router: fakeRouter(),
    connect: async () => {},
    shutdown: async () => {},
    ownerUid: OWNER_UID,
    cwdBase: wsRoot,
  };
}

/** Write the two-layer config the REAL loader understands (for sandbox tests). */
function writeRealConfigs(): void {
  writeFileSync(
    join(base, 'config.json'),
    JSON.stringify({ apiUrl: 'http://127.0.0.1:28080', bots: [{ id: 'bot1' }] }),
  );
  writeFileSync(
    join(base, 'bot1', 'config.json'),
    JSON.stringify({ botToken: 'bot-token-1', sdk: { sandboxMode: 'read-only', allowWorkspaceWrite: false } }),
  );
}

function realResolveBotConfig(configId: string): Config {
  // Mirrors index.ts#resolveBotConfigById (same merge, same throw-on-missing).
  const match = resolveBotConfigs(loadConfig(join(base, 'config.json'))).find((c) => c.botId === configId);
  if (!match) {
    throw new Error(`bot config not found for id "${configId}"`);
  }
  return match;
}

async function startServer(resolveBotConfig: (id: string) => Config): Promise<void> {
  const manager = new BotManager(async (configId) => {
    startCalls++;
    return makeBot(configId);
  });
  await manager.addBot('bot1');
  handle = await startWorkbenchServer(
    {
      manager,
      resolveBotConfig,
      apiUrl: () => 'http://127.0.0.1:28080',
      baseDir: base,
      staticRoot: join(base, 'static-dist'),
      host: '127.0.0.1',
      fetchImpl: fakeFetch(),
    },
    0, // ephemeral port
  );
}

function api(path: string, token?: string, init?: RequestInit): Promise<Response> {
  return fetch(`http://127.0.0.1:${handle.port}${path}`, {
    ...init,
    headers: { ...(token ? { token } : {}), ...(init?.headers ?? {}) },
  });
}

beforeEach(async () => {
  base = mkdtempSync(join(tmpdir(), 'workbench-test-'));
  wsRoot = join(base, 'bot1', 'workspace');
  mkdirSync(wsRoot, { recursive: true });
  upstreamCalls = 0;
  startCalls = 0;
  upstreamUidByToken = { [OWNER_TOKEN]: OWNER_UID, [STRANGER_TOKEN]: STRANGER_UID };
});

afterEach(async () => {
  if (handle) await handle.close();
  rmSync(base, { recursive: true, force: true });
});

/** Create a real session dir via cwd-resolver (dir + registry marker). */
function makeSession(key: string, kind: 'dm' | 'group' = 'dm'): string {
  const dir = resolveSessionCwd(wsRoot, { kind, sessionKey: key });
  writeFileSync(join(dir, 'hello.txt'), 'hello workbench\n');
  return dir.split(/[\\/]/).pop() as string; // the 16-hex name
}

// ─── Auth ───────────────────────────────────────────────────────────────────

describe('workbench auth', () => {
  beforeEach(async () => {
    writeRealConfigs();
    await startServer(realResolveBotConfig);
  });

  it('rejects a request without a token (401)', async () => {
    const res = await api('/workbench/api/bots');
    expect(res.status).toBe(401);
    expect(upstreamCalls).toBe(0); // short-circuits before upstream
  });

  it('rejects a token the upstream rejects (401)', async () => {
    const res = await api('/workbench/api/bots', 'tok-bogus');
    expect(res.status).toBe(401);
    expect(upstreamCalls).toBe(1);
  });

  it('accepts a valid token and caches it for the TTL (one upstream call)', async () => {
    const a = await api('/workbench/api/bots', OWNER_TOKEN);
    const b = await api('/workbench/api/bots', OWNER_TOKEN);
    expect(a.status).toBe(200);
    expect(b.status).toBe(200);
    expect(upstreamCalls).toBe(1); // second request hit the in-process cache
  });

  it('returns 403 when a valid user touches another user\'s bot', async () => {
    const res = await api('/workbench/api/bots/bot1/sessions', STRANGER_TOKEN);
    expect(res.status).toBe(403);
  });

  it('403s the sandbox toggle for non-owners BEFORE any write', async () => {
    const before = readFileSync(join(base, 'bot1', 'config.json'), 'utf-8');
    const res = await api('/workbench/api/bots/bot1/sandbox', STRANGER_TOKEN, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ allowWrite: true }),
    });
    expect(res.status).toBe(403);
    expect(readFileSync(join(base, 'bot1', 'config.json'), 'utf-8')).toBe(before);
  });

  it('404s an unknown bot id', async () => {
    const res = await api('/workbench/api/bots/nope/sessions', OWNER_TOKEN);
    expect(res.status).toBe(404);
  });

  it('400s a malformed (undecodable) bot id instead of 500', async () => {
    // %FF is a syntactically valid percent-escape but invalid UTF-8, so the
    // URL layer keeps it intact while decodeURIComponent throws URIError.
    const res = await api('/workbench/api/bots/%FF/sessions', OWNER_TOKEN);
    expect(res.status).toBe(400);
  });

  it('extractUid parses bare and envelope shapes, rejects error codes', () => {
    expect(extractUid({ uid: 'u1' })).toBe('u1');
    expect(extractUid({ data: { uid: 'u2' } })).toBe('u2');
    expect(extractUid({ code: 401, uid: 'u3' })).toBeUndefined();
    expect(extractUid('nope')).toBeUndefined();
    expect(extractUid(null)).toBeUndefined();
  });
});

// ─── Roster contract ────────────────────────────────────────────────────────

describe('GET /workbench/api/bots', () => {
  beforeEach(async () => {
    writeRealConfigs();
    await startServer(realResolveBotConfig);
  });

  it('emits the exact contract shape', async () => {
    const res = await api('/workbench/api/bots', OWNER_TOKEN);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { bots: Array<Record<string, unknown>> };
    expect(Array.isArray(body.bots)).toBe(true);
    expect(body.bots).toHaveLength(1);
    const bot = body.bots[0];
    expect(bot).toEqual({
      id: 'bot1',
      robotUid: 'robot-bot1',
      ownerUid: OWNER_UID,
      sandboxMode: 'read-only',
      allowWorkspaceWrite: false,
      runtime: 'codex',
      connected: true,
    });
  });

  it('reflects the on-disk double-switch values', async () => {
    writeFileSync(
      join(base, 'bot1', 'config.json'),
      JSON.stringify({ botToken: 'bot-token-1', sdk: { sandboxMode: 'workspace-write', allowWorkspaceWrite: true } }),
    );
    const res = await api('/workbench/api/bots', OWNER_TOKEN);
    const body = (await res.json()) as { bots: Array<Record<string, unknown>> };
    expect(body.bots[0].sandboxMode).toBe('workspace-write');
    expect(body.bots[0].allowWorkspaceWrite).toBe(true);
  });

  it('lists ONLY the caller\'s own bots (W4 filter semantics)', async () => {
    // Every fixture bot is owned by OWNER_UID — a different valid user must
    // see an empty roster, never someone else's bots.
    const res = await api('/workbench/api/bots', STRANGER_TOKEN);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { bots: unknown[] };
    expect(body.bots).toEqual([]);
  });
});

// ─── Sessions / tree / traversal ────────────────────────────────────────────

describe('sessions and tree', () => {
  let hex: string;
  beforeEach(async () => {
    writeRealConfigs();
    await startServer(realResolveBotConfig);
    hex = makeSession('alice', 'dm');
  });

  it('lists sessions from the 16-hex dirs + registry markers', async () => {
    const res = await api('/workbench/api/bots/bot1/sessions', OWNER_TOKEN);
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      sessions: Array<{ hex: string; kind: string; createdAt: string; lastActiveAt: string; fileCount: number }>;
      truncated: boolean;
    };
    expect(body.truncated).toBe(false);
    expect(body.sessions).toHaveLength(1);
    const s = body.sessions[0];
    expect(s.hex).toBe(hex);
    expect(s.hex).toMatch(/^[0-9a-f]{16}$/);
    expect(s.kind).toBe('dm');
    expect(new Date(s.createdAt).getTime()).not.toBeNaN();
    expect(new Date(s.lastActiveAt).getTime()).not.toBeNaN();
    expect(s.fileCount).toBe(1); // hello.txt
  });

  it('lists a single level of the session tree (never recursive)', async () => {
    mkdirSync(join(wsRoot, hex, 'subdir'));
    writeFileSync(join(wsRoot, hex, 'subdir', 'deep.txt'), 'deep');
    const res = await api(`/workbench/api/bots/bot1/tree?session=${hex}&path=`, OWNER_TOKEN);
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      entries: Array<{ name: string; type: string; size: number; mtime: string }>;
    };
    const names = body.entries.map((e) => `${e.type}:${e.name}`);
    expect(names).toContain('dir:subdir');
    expect(names).toContain('file:hello.txt');
    expect(names).not.toContain('file:deep.txt'); // no recursion
    const file = body.entries.find((e) => e.name === 'hello.txt');
    expect(file?.size).toBe('hello workbench\n'.length);
  });

  it('rejects traversal: lexical ../ escapes', async () => {
    writeFileSync(join(wsRoot, 'secret.txt'), 'top-level secret');
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=../secret.txt`, OWNER_TOKEN);
    expect(res.status).toBe(403);
  });

  it('rejects traversal: deep ../../../ escapes', async () => {
    const res = await api(
      `/workbench/api/bots/bot1/tree?session=${hex}&path=../../../../../../etc`,
      OWNER_TOKEN,
    );
    expect(res.status).toBe(403);
  });

  it('rejects traversal: absolute paths', async () => {
    const outside = join(base, 'outside.txt');
    writeFileSync(outside, 'outside');
    const res = await api(
      `/workbench/api/bots/bot1/file?session=${hex}&path=${encodeURIComponent(outside)}`,
      OWNER_TOKEN,
    );
    expect(res.status).toBe(403);
  });

  it('rejects a symlinked final component (even pointing inward-ish)', async () => {
    const target = join(base, 'outside2.txt');
    writeFileSync(target, 'outside2');
    let link: string;
    try {
      link = join(wsRoot, hex, 'link.txt');
      symlinkSync(target, link);
    } catch {
      return; // fs.symlinkSync needs privilege on some Windows setups — covered by junction test
    }
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=link.txt`, OWNER_TOKEN);
    expect(res.status).toBe(403);
  });

  it('rejects traversal THROUGH a junction/symlinked directory (Windows-safe)', async () => {
    // Junctions need no privileges on Windows. Point a dir inside the session
    // at an OUTSIDE dir and try to read through it.
    const outsideDir = join(base, 'outside-dir');
    mkdirSync(outsideDir);
    writeFileSync(join(outsideDir, 'crown.txt'), 'jewels');
    let escapeDir: string;
    try {
      escapeDir = join(wsRoot, hex, 'escape');
      symlinkSync(outsideDir, escapeDir, 'junction');
    } catch {
      return; // extremely locked-down environment; lexical tests still cover
    }
    const tree = await api(`/workbench/api/bots/bot1/tree?session=${hex}&path=escape`, OWNER_TOKEN);
    expect(tree.status).toBe(403);
    const file = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=escape/crown.txt`, OWNER_TOKEN);
    expect(file.status).toBe(403);
  });

  it('400s a malformed session hex, 404s an unknown session', async () => {
    const bad = await api(`/workbench/api/bots/bot1/tree?session=NOPE&path=`, OWNER_TOKEN);
    expect(bad.status).toBe(400);
    const missing = await api(
      `/workbench/api/bots/bot1/tree?session=${'f'.repeat(16)}&path=`,
      OWNER_TOKEN,
    );
    expect(missing.status).toBe(404);
  });
});

// ─── File streaming ─────────────────────────────────────────────────────────

describe('GET /workbench/api/bots/:id/file', () => {
  let hex: string;
  beforeEach(async () => {
    writeRealConfigs();
    await startServer(realResolveBotConfig);
    hex = makeSession('bob');
  });

  it('streams text inline with the right Content-Type', async () => {
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=hello.txt`, OWNER_TOKEN);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/plain');
    expect(res.headers.get('content-disposition')).toBeNull(); // inline, not attachment
    expect(await res.text()).toBe('hello workbench\n');
  });

  it('honors ETag + If-None-Match → 304', async () => {
    const first = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=hello.txt`, OWNER_TOKEN);
    const etag = first.headers.get('etag');
    expect(etag).toMatch(/^"\d+.*:\d+"$/); // "<mtime>:<size>" quoted
    await first.arrayBuffer();
    const second = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=hello.txt`, OWNER_TOKEN, {
      headers: { 'if-none-match': etag as string },
    });
    expect(second.status).toBe(304);
  });

  it('supports Range requests (206 + Content-Range)', async () => {
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=hello.txt`, OWNER_TOKEN, {
      headers: { range: 'bytes=0-4' },
    });
    expect(res.status).toBe(206);
    expect(res.headers.get('content-range')).toBe(`bytes 0-4/${'hello workbench\n'.length}`);
    expect(await res.text()).toBe('hello');
  });

  it('416s an unsatisfiable range', async () => {
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=hello.txt`, OWNER_TOKEN, {
      headers: { range: 'bytes=99999-' },
    });
    expect(res.status).toBe(416);
  });

  it('416s a suffix Range on an EMPTY file (no satisfiable bytes)', async () => {
    writeFileSync(join(wsRoot, hex, 'empty.bin'), '');
    // Before the fix this produced {start:0,end:-1} → createReadStream threw
    // ERR_OUT_OF_RANGE mid-handler. The whole-file GET must still work (200).
    const ranged = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=empty.bin`, OWNER_TOKEN, {
      headers: { range: 'bytes=-100' },
    });
    expect(ranged.status).toBe(416);
    const full = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=empty.bin`, OWNER_TOKEN);
    expect(full.status).toBe(200);
    expect(await full.text()).toBe('');
  });

  it('inlines .docx under 10MB WITHOUT attachment disposition (mammoth reachable)', async () => {
    // Symmetric counterpart of the unknown-type attachment case: a .docx must
    // carry its OOXML Content-Type and NO Content-Disposition: attachment.
    writeFileSync(join(wsRoot, hex, 'report.docx'), Buffer.from('PK\x03\x04fake-docx-payload'));
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=report.docx`, OWNER_TOKEN);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain(
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    );
    expect(res.headers.get('content-disposition')).toBeNull();
  });

  it('falls back to attachment for a .docx over the 10MB inline cap', async () => {
    writeFileSync(join(wsRoot, hex, 'huge.docx'), Buffer.alloc(10 * 1024 * 1024 + 1));
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=huge.docx`, OWNER_TOKEN, {
      method: 'HEAD', // headers only — no need to stream 10MB through the test
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('content-disposition')).toContain('attachment');
  });

  it('attachments unknown types instead of previewing them', async () => {
    writeFileSync(join(wsRoot, hex, 'blob.xyz'), Buffer.from([1, 2, 3, 4]));
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=blob.xyz`, OWNER_TOKEN);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/octet-stream');
    expect(res.headers.get('content-disposition')).toContain('attachment');
  });

  it('400s a directory path', async () => {
    const res = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=.`, OWNER_TOKEN);
    expect(res.status).toBe(400);
  });
});

// ─── Sandbox toggle (atomic config rewrite + per-bot restart) ──────────────

describe('POST /workbench/api/bots/:id/sandbox', () => {
  beforeEach(async () => {
    writeRealConfigs();
    await startServer(realResolveBotConfig);
  });

  it('enables write access: BOTH switches set, atomic write, per-bot restart', async () => {
    const res = await api('/workbench/api/bots/bot1/sandbox', OWNER_TOKEN, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ allowWrite: true }),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, sandboxMode: 'workspace-write', allowWorkspaceWrite: true });

    // Double-switch: BOTH fields move together (config.ts L82-96 semantics).
    const written = JSON.parse(readFileSync(join(base, 'bot1', 'config.json'), 'utf-8')) as {
      sdk: { sandboxMode: string; allowWorkspaceWrite: boolean };
    };
    expect(written.sdk.sandboxMode).toBe('workspace-write');
    expect(written.sdk.allowWorkspaceWrite).toBe(true);

    // Atomicity: no leftover temp files in the bot dir.
    const leftovers = readdirSync(join(base, 'bot1')).filter((f) => f.endsWith('.tmp'));
    expect(leftovers).toEqual([]);

    // Per-bot restart happened through the manager (remove→add = start again).
    expect(startCalls).toBeGreaterThanOrEqual(2);
  });

  it('disables write access: BOTH switches reset', async () => {
    await api('/workbench/api/bots/bot1/sandbox', OWNER_TOKEN, {
      method: 'POST',
      body: JSON.stringify({ allowWrite: true }),
    });
    const res = await api('/workbench/api/bots/bot1/sandbox', OWNER_TOKEN, {
      method: 'POST',
      body: JSON.stringify({ allowWrite: false }),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, sandboxMode: 'read-only', allowWorkspaceWrite: false });
    const written = JSON.parse(readFileSync(join(base, 'bot1', 'config.json'), 'utf-8')) as {
      sdk: { sandboxMode: string; allowWorkspaceWrite: boolean };
    };
    expect(written.sdk.sandboxMode).toBe('read-only');
    expect(written.sdk.allowWorkspaceWrite).toBe(false);
  });

  it('rolls back when the rewritten config fails validation (422)', async () => {
    const original = readFileSync(join(base, 'bot1', 'config.json'), 'utf-8');
    await handle.close();
    // Restart the server with a validator that always rejects.
    const manager = new BotManager(async (configId) => makeBot(configId));
    await manager.addBot('bot1');
    handle = await startWorkbenchServer(
      {
        manager,
        resolveBotConfig: () => {
          throw new Error('simulated invalid config');
        },
        apiUrl: () => 'http://127.0.0.1:28080',
        baseDir: base,
        staticRoot: join(base, 'static-dist'),
        host: '127.0.0.1',
        fetchImpl: fakeFetch(),
      },
      0,
    );
    const res = await api('/workbench/api/bots/bot1/sandbox', OWNER_TOKEN, {
      method: 'POST',
      body: JSON.stringify({ allowWrite: true }),
    });
    expect(res.status).toBe(422);
    // The bad config must NOT survive — original content restored byte-for-byte.
    expect(readFileSync(join(base, 'bot1', 'config.json'), 'utf-8')).toBe(original);
    const leftovers = readdirSync(join(base, 'bot1')).filter((f) => f.endsWith('.tmp'));
    expect(leftovers).toEqual([]);
  });

  it('400s a malformed body / non-boolean allowWrite', async () => {
    const bad = await api('/workbench/api/bots/bot1/sandbox', OWNER_TOKEN, {
      method: 'POST',
      body: 'not-json',
    });
    expect(bad.status).toBe(400);
    const nonBool = await api('/workbench/api/bots/bot1/sandbox', OWNER_TOKEN, {
      method: 'POST',
      body: JSON.stringify({ allowWrite: 'yes' }),
    });
    expect(nonBool.status).toBe(400);
  });
});

// ─── Upload (#43) ──────────────────────────────────────────────────────────

describe('PUT /workbench/api/bots/:id/files/:filename', () => {
  let hex: string;
  beforeEach(async () => {
    writeRealConfigs();
    await startServer(realResolveBotConfig);
    hex = makeSession('carol');
  });

  function put(name: string, body: BodyInit, token: string = OWNER_TOKEN): Promise<Response> {
    return api(`/workbench/api/bots/bot1/files/${encodeURIComponent(name)}?session=${hex}`, token, {
      method: 'PUT',
      headers: { 'content-type': 'application/octet-stream' },
      body,
    });
  }

  it('uploads a file into the session root and returns the listing entry', async () => {
    const res = await put('notes.txt', 'uploaded via workbench');
    expect(res.status).toBe(200);
    const entry = (await res.json()) as { name: string; type: string; size: number; mtime: string };
    expect(entry.name).toBe('notes.txt');
    expect(entry.type).toBe('file');
    expect(entry.size).toBe('uploaded via workbench'.length);
    expect(new Date(entry.mtime).getTime()).not.toBeNaN();
    // Same root as the tree listing: the file shows up immediately.
    const tree = await api(`/workbench/api/bots/bot1/tree?session=${hex}&path=`, OWNER_TOKEN);
    const names = ((await tree.json()) as { entries: Array<{ name: string }> }).entries.map((e) => e.name);
    expect(names).toContain('notes.txt');
    // And it is readable through the existing file endpoint.
    const file = await api(`/workbench/api/bots/bot1/file?session=${hex}&path=notes.txt`, OWNER_TOKEN);
    expect(await file.text()).toBe('uploaded via workbench');
  });

  it('overwrites an existing file with the same name', async () => {
    writeFileSync(join(wsRoot, hex, 'notes.txt'), 'old content');
    const res = await put('notes.txt', 'new content');
    expect(res.status).toBe(200);
    expect(readFileSync(join(wsRoot, hex, 'notes.txt'), 'utf-8')).toBe('new content');
  });

  it('accepts unicode (CJK) file names', async () => {
    const res = await put('报告 2026-09.md', '# 报告');
    expect(res.status).toBe(200);
    const entry = (await res.json()) as { name: string };
    expect(entry.name).toBe('报告 2026-09.md');
  });

  it('400s hostile file names (path separators, .., controls, bad chars, too long)', async () => {
    expect((await put('a/b.txt', 'x')).status).toBe(400);
    expect((await put('a..b.txt', 'x')).status).toBe(400);
    expect((await put('bad:colon.txt', 'x')).status).toBe(400);
    expect((await put('trail. ', 'x')).status).toBe(400);
    expect((await put('x'.repeat(201), 'x')).status).toBe(400);
    // '.' / '..' never survive WHATWG URL normalization and an empty segment
    // is filtered out of the route — both land on the unknown-endpoint 404
    // BEFORE ever reaching the sanitizer (defense in depth keeps the checks).
    expect((await put('..', 'x')).status).toBe(404);
    expect((await put('', 'x')).status).toBe(404);
    // Nothing hostile reached the filesystem.
    expect(readdirSync(join(wsRoot, hex))).toEqual(['hello.txt']);
  });

  it('400s a missing/invalid session and 404s an unknown one', async () => {
    const missing = await api('/workbench/api/bots/bot1/files/a.txt', OWNER_TOKEN, {
      method: 'PUT',
      body: 'x',
    });
    expect(missing.status).toBe(400);
    const bad = await api('/workbench/api/bots/bot1/files/a.txt?session=NOPE', OWNER_TOKEN, {
      method: 'PUT',
      body: 'x',
    });
    expect(bad.status).toBe(400);
    const unknown = await api(`/workbench/api/bots/bot1/files/a.txt?session=${'f'.repeat(16)}`, OWNER_TOKEN, {
      method: 'PUT',
      body: 'x',
    });
    expect(unknown.status).toBe(404);
  });

  it(
    '413s a body over the 20 MB limit (declared Content-Length)',
    async () => {
      // undici sets an honest Content-Length for a Buffer body — the server's
      // pre-check catches it, discards the incoming bytes WITHOUT buffering,
      // and only answers 413 once the body is drained (answering earlier
      // makes Windows RST the still-uploading connection and lose the 413).
      const big = Buffer.alloc(20 * 1024 * 1024 + 1, 66);
      const res = await put('big.bin', big);
      expect(res.status).toBe(413);
      // The file must not have been created (not even partially).
      expect(readdirSync(join(wsRoot, hex))).toEqual(['hello.txt']);
    },
    20_000,
  );

  it(
    '413s a chunked body that crosses the cap (byte counter)',
    async () => {
      // Chunked transfer has NO Content-Length, so only the streaming byte
      // counter can catch it. node:http gives deterministic control; the
      // server flushes the 413, keeps discarding bytes until the request
      // ends, then closes the connection cleanly.
      const status = await new Promise<number>((resolveP, rejectP) => {
        const req = request(
          {
            host: '127.0.0.1',
            port: handle.port,
            path: `/workbench/api/bots/bot1/files/big.bin?session=${hex}`,
            method: 'PUT',
            headers: { token: OWNER_TOKEN, 'transfer-encoding': 'chunked' },
          },
          (resp) => {
            resp.resume();
            resolveP(resp.statusCode ?? 0);
          },
        );
        req.on('error', () => {
          /* socket cut after the 413 — status already captured */
        });
        const chunk = Buffer.alloc(1024 * 1024, 65);
        for (let i = 0; i < 21; i++) req.write(chunk);
        req.end();
        // Safety net: never hang the suite if the response never arrives.
        setTimeout(() => rejectP(new Error('no 413 response within 15s')), 15_000).unref();
      });
      expect(status).toBe(413);
    },
    20_000,
  );

  it('403s a non-owner BEFORE anything is written', async () => {
    const res = await put('evil.txt', 'nope', STRANGER_TOKEN);
    expect(res.status).toBe(403);
    expect(readdirSync(join(wsRoot, hex))).toEqual(['hello.txt']);
  });

  it('405s non-PUT methods on the files route', async () => {
    const res = await api(`/workbench/api/bots/bot1/files/a.txt?session=${hex}`, OWNER_TOKEN);
    expect(res.status).toBe(405);
  });

  it('refuses to overwrite a directory or a symlink of the same name', async () => {
    mkdirSync(join(wsRoot, hex, 'adir'));
    const dirRes = await put('adir', 'x');
    expect(dirRes.status).toBe(400);
    const outside = join(base, 'outside3.txt');
    writeFileSync(outside, 'outside3');
    let linkOk = true;
    try {
      symlinkSync(outside, join(wsRoot, hex, 'link3.txt'));
    } catch {
      linkOk = false; // symlink privileges unavailable on this machine
    }
    if (linkOk) {
      const linkRes = await put('link3.txt', 'overwrite attempt');
      expect(linkRes.status).toBe(403);
      expect(readFileSync(outside, 'utf-8')).toBe('outside3'); // target untouched
    }
  });
});

// ─── Static SPA hosting ─────────────────────────────────────────────────────

describe('static hosting', () => {
  beforeEach(async () => {
    writeRealConfigs();
    await startServer(realResolveBotConfig);
  });

  it('serves index.html for /workbench/ and SPA-fallbacks unknown routes', async () => {
    mkdirSync(join(base, 'static-dist'), { recursive: true });
    writeFileSync(join(base, 'static-dist', 'index.html'), '<html>WB-SPA</html>');
    writeFileSync(join(base, 'static-dist', 'app.js'), 'console.log(1)');
    mkdirSync(join(base, 'static-dist', 'assets'), { recursive: true });

    const root = await api('/workbench/', OWNER_TOKEN);
    expect(root.status).toBe(200);
    expect(root.headers.get('content-type')).toContain('text/html');
    expect(await root.text()).toBe('<html>WB-SPA</html>');

    const asset = await api('/workbench/app.js', OWNER_TOKEN);
    expect(asset.status).toBe(200);
    expect(await asset.text()).toBe('console.log(1)');

    const spa = await api('/workbench/bots/whatever/deep/route', OWNER_TOKEN);
    expect(spa.status).toBe(200);
    expect(await spa.text()).toBe('<html>WB-SPA</html>'); // SPA fallback
  });

  it('does NOT require auth for the static page', async () => {
    mkdirSync(join(base, 'static-dist'), { recursive: true });
    writeFileSync(join(base, 'static-dist', 'index.html'), '<html>OPEN</html>');
    const res = await api('/workbench/'); // no token at all
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('<html>OPEN</html>');
  });

  it('returns 503 with a hint when dist is missing', async () => {
    // static-dist was never created for this test.
    const res = await api('/workbench/');
    expect(res.status).toBe(503);
    expect(await res.text()).toContain('web/workbench/dist');
  });

  it('never serves files outside staticRoot (encoded traversal)', async () => {
    mkdirSync(join(base, 'static-dist'), { recursive: true });
    writeFileSync(join(base, 'static-dist', 'index.html'), '<html>FALLBACK</html>');
    writeFileSync(join(base, 'config.json'), JSON.stringify({ secret: true }));
    // WHATWG URL parsing decodes %2E and normalizes the dot-segments away
    // (→ /config.json, outside the /workbench prefix) — so we get a 404 (or at
    // worst the SPA fallback). Either way the secret file must NEVER leak.
    const res = await api('/workbench/%2e%2e/config.json');
    expect([200, 404]).toContain(res.status);
    const text = await res.text();
    expect(text).not.toContain('secret');
  });
});
