/**
 * lumo-bridge tests — pure unit tests with a mocked global fetch. No real Lumo
 * server is contacted: we hand back synthetic SSE responses and assert the
 * bridge's behaviour (chunk yielding, event filtering, error/terminal handling,
 * session creation + stale-resume retry, artifact reclaim, timeouts).
 *
 * Style mirrors agent-bridge.test.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, rmSync, readdirSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// uploadLumoImage delegates to fetchInboundImageBytes, whose SSRF gate
// (assertPublicUrl) does a DNS lookup — mock it deterministically like
// media-inbound.test.ts does.
vi.mock('node:dns/promises', () => ({
  lookup: vi.fn(async (hostname: string) => {
    if (hostname === 'octo.example.com') return [{ address: '203.0.113.42', family: 4 }];
    throw new Error(`Test DNS mock: unexpected hostname ${hostname}`);
  }),
}));

import { queryLumo, uploadLumoImage } from '../lumo-bridge.js';
import type { Config } from '../config.js';

// ─── Helpers ────────────────────────────────────────────────────────────────

let tmp: string;
let fetchMock: ReturnType<typeof vi.fn>;
const realFetch = globalThis.fetch;

/** Stream request bodies captured for assertions ({conversationId, content}). */
let streamBodies: Array<{ conversationId?: string; content?: string }>;

function setup(): void {
  tmp = mkdtempSync(join(tmpdir(), 'cc-octo-lumo-test-'));
  streamBodies = [];
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
}

function teardown(): void {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
  rmSync(tmp, { recursive: true, force: true });
}

function cfg(
  lumo: { baseUrl: string; idleTimeoutMs?: number; maxTurnMs?: number },
): Config {
  return {
    botToken: 't',
    apiUrl: 'https://example.com',
    baseDir: tmp,
    cwd: tmp,
    cwdBase: tmp,
    dataDir: join(tmp, 'data'),
    sdk: {},
    rateLimit: { maxPerMinute: 5 },
    context: { maxContextChars: 6000, historyLimit: 40 },
    maxResponseChars: 524288,
    dispatchTimeoutMs: 300000,
    runtime: 'lumo',
    lumo,
  } as Config;
}

async function collect(it: AsyncIterable<string>): Promise<string[]> {
  const out: string[] = [];
  for await (const c of it) out.push(c);
  return out;
}

/** Build a 200 SSE Response from event objects / raw lines. */
function sse(events: Array<Record<string, unknown> | string>): Response {
  const lines = events.map((e) =>
    typeof e === 'string' ? e : `data: ${JSON.stringify(e)}`,
  );
  return new Response(lines.join('\n') + '\n', {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

/** A 200 SSE Response whose body never emits — used to trigger timeouts. */
function hangingSse(): Response {
  const stream = new ReadableStream<Uint8Array>({ start() {
    /* never enqueue, never close */
  } });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

const chunk = (content: string): Record<string, unknown> => ({ type: 'chunk', content });
const done: Record<string, unknown> = { type: 'done' };
const BASE = 'http://127.0.0.1:8080';

beforeEach(setup);
afterEach(teardown);

// ─── Streaming behaviour ────────────────────────────────────────────────────

describe('queryLumo streaming', () => {
  it('① concatenates chunk events into yielded output', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      return sse([chunk('Hello, '), chunk('world'), done]);
    });
    const out = await collect(queryLumo('hi', cfg({ baseUrl: BASE })));
    expect(out).toEqual(['Hello, ', 'world']);
    expect(out.join('')).toBe('Hello, world');
  });

  it('② ignores :keepalive comments and unknown events', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      return sse([
        ':keepalive',
        { type: 'modelSwitch', model: 'x' },
        { type: 'autoModel' },
        { type: 'sources', list: [] },
        { type: 'status', text: 'thinking' },
        chunk('real'),
        { type: 'searchNotice' },
        done,
      ]);
    });
    const out = await collect(queryLumo('hi', cfg({ baseUrl: BASE })));
    expect(out).toEqual(['real']);
  });

  it('③ throws on an error event', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      return sse([chunk('partial'), { type: 'error', message: 'boom' }]);
    });
    await expect(collect(queryLumo('hi', cfg({ baseUrl: BASE })))).rejects.toThrow('boom');
  });

  it('④ throws when the stream ends without a done/error event', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      return sse([chunk('no terminal')]);
    });
    await expect(collect(queryLumo('hi', cfg({ baseUrl: BASE })))).rejects.toThrow(
      /without a done\/error/,
    );
  });
});

// ─── Session lifecycle ───────────────────────────────────────────────────────

describe('queryLumo sessions', () => {
  it('⑤ creates a conversation and reports its id on the first turn', async () => {
    let created = 0;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/conversations')) {
        created++;
        // No auth header; camelCase JSON body {}.
        expect((init?.headers as Record<string, string>)?.Authorization).toBeUndefined();
        return Response.json({ id: 'conv-new' });
      }
      streamBodies.push(JSON.parse(String(init?.body)) as { conversationId?: string });
      return sse([chunk('ok'), done]);
    });
    const seen: string[] = [];
    const out = await collect(
      queryLumo('hi', cfg({ baseUrl: BASE }), undefined, undefined, {
        onSessionId: (id) => seen.push(id),
      }),
    );
    expect(created).toBe(1);
    expect(seen).toEqual(['conv-new']);
    expect(streamBodies[0]?.conversationId).toBe('conv-new');
    expect(out.join('')).toBe('ok');
  });

  it('⑥ on 404 fires onResumeFailed, recreates, and retries once', async () => {
    let created = 0;
    let streamCalls = 0;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/conversations')) {
        created++;
        return Response.json({ id: 'conv-2' });
      }
      streamCalls++;
      const body = JSON.parse(String(init?.body)) as { conversationId?: string; content?: string };
      streamBodies.push(body);
      // First stream (the stale resume) → 404; retry → real content.
      if (streamCalls === 1) return new Response('nope', { status: 404 });
      return sse([chunk('recovered'), done]);
    });
    const seen: string[] = [];
    let resumeFailed = 0;
    const out = await collect(
      queryLumo('hi', cfg({ baseUrl: BASE }), undefined, undefined, {
        resume: 'stale-conv',
        onSessionId: (id) => seen.push(id),
        onResumeFailed: () => { resumeFailed++; },
        fallbackRetryPrompt: 'RETRY-PROMPT',
      }),
    );
    expect(resumeFailed).toBe(1);
    expect(created).toBe(1);
    expect(seen).toEqual(['conv-2']);
    expect(streamCalls).toBe(2);
    // The first stream used the stale resume id; the retry used the fallback prompt.
    expect(streamBodies[0]?.conversationId).toBe('stale-conv');
    expect(streamBodies[1]?.conversationId).toBe('conv-2');
    expect(streamBodies[1]?.content).toBe('RETRY-PROMPT');
    expect(out.join('')).toBe('recovered');
  });
});

// ─── Artifact reclaim ────────────────────────────────────────────────────────

describe('queryLumo artifact reclaim', () => {
  const hash = 'a'.repeat(32);
  const uploadPath = `/uploads/${hash}.png`;

  it('⑦ downloads to .lumo-dl.part then renames into cwd with the expected name', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      if (url.includes('/uploads/')) {
        return new Response(Buffer.from('PNGDATA'), { status: 200 });
      }
      return sse([chunk(`file: ${uploadPath} done`), done]);
    });
    await collect(queryLumo('hi', cfg({ baseUrl: BASE })));

    const files = readdirSync(tmp);
    expect(files).not.toContain('.lumo-dl.part'); // temp renamed away
    const named = files.filter((f) => /^Lumo-\d{8}-\d{6}-[0-9a-f]{4}\.png$/.test(f));
    expect(named).toHaveLength(1);
    expect(readFileSync(join(tmp, named[0]!), 'utf-8')).toBe('PNGDATA');
  });

  it('⑧ never fails the turn when an artifact download fails', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      if (url.includes('/uploads/')) return new Response('boom', { status: 500 });
      return sse([chunk(`file: ${uploadPath}`), done]);
    });
    const out = await collect(queryLumo('hi', cfg({ baseUrl: BASE })));
    expect(out.join('')).toContain(uploadPath);
    // No file written, but no throw either.
    expect(readdirSync(tmp).filter((f) => f.startsWith('Lumo-'))).toHaveLength(0);
    expect(existsSync(join(tmp, '.lumo-dl.part'))).toBe(false);
    errSpy.mockRestore();
  });
});

// ─── Timeouts ────────────────────────────────────────────────────────────────

describe('queryLumo timeouts', () => {
  it('⑨ aborts on the idle timeout', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      return hangingSse();
    });
    await expect(
      collect(queryLumo('hi', cfg({ baseUrl: BASE, idleTimeoutMs: 20, maxTurnMs: 5000 }))),
    ).rejects.toThrow(/idle timeout/);
  });

  it('⑩ aborts on the absolute (max-turn) timeout', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      return hangingSse();
    });
    await expect(
      collect(queryLumo('hi', cfg({ baseUrl: BASE, idleTimeoutMs: 5000, maxTurnMs: 20 }))),
    ).rejects.toThrow(/absolute timeout/);
  });
});

// ─── Image attachments (#86 lumo) ────────────────────────────────────────────

describe('uploadLumoImage', () => {
  it('⑪ downloads from Octo then multipart-uploads to Lumo, returning the attachment ref', async () => {
    fetchMock.mockImplementation(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.startsWith('https://octo.example.com/')) {
        // Octo media fetch — must carry the scoped bot token.
        expect((init?.headers as Record<string, string>)?.Authorization).toBe('Bearer octo-token');
        return new Response(new Uint8Array([1, 2, 3]), {
          status: 200,
          headers: { 'content-type': 'image/png' },
        });
      }
      if (u === `${BASE}/api/uploads`) {
        // Lumo upload — multipart body, no Authorization in the demo mode.
        expect(init?.method).toBe('POST');
        expect(init?.body).toBeInstanceOf(FormData);
        expect((init?.body as FormData).get('file')).toBeInstanceOf(Blob);
        return Response.json({
          id: 'abc123',
          url: '/uploads/abc123.png',
          fileName: 'x.png',
          mimeType: 'image/png',
          size: 3,
        });
      }
      throw new Error(`unexpected fetch ${u}`);
    });
    const r = await uploadLumoImage({
      url: 'https://octo.example.com/file/a.png',
      baseUrl: BASE,
      botToken: 'octo-token',
      apiUrl: 'https://octo.example.com',
    });
    expect('attachment' in r).toBe(true);
    if ('attachment' in r) {
      expect(r.attachment).toEqual({
        id: 'abc123',
        url: '/uploads/abc123.png',
        fileName: 'x.png',
        mimeType: 'image/png',
      });
    }
  });

  it('⑫ returns {error} when the Lumo upload rejects (HTTP 413)', async () => {
    fetchMock.mockImplementation(async (url: string | URL) => {
      const u = String(url);
      if (u.startsWith('https://octo.example.com/')) {
        return new Response(new Uint8Array([1]), {
          status: 200,
          headers: { 'content-type': 'image/png' },
        });
      }
      if (u === `${BASE}/api/uploads`) return new Response('too large', { status: 413 });
      throw new Error(`unexpected fetch ${u}`);
    });
    const r = await uploadLumoImage({
      url: 'https://octo.example.com/file/a.png',
      baseUrl: BASE,
      botToken: 'octo-token',
      apiUrl: 'https://octo.example.com',
    });
    expect('error' in r && /HTTP 413/.test(r.error)).toBe(true);
  });

  it('⑬ returns {error} when the upload response lacks id/url', async () => {
    fetchMock.mockImplementation(async (url: string | URL) => {
      const u = String(url);
      if (u.startsWith('https://octo.example.com/')) {
        return new Response(new Uint8Array([1]), {
          status: 200,
          headers: { 'content-type': 'image/png' },
        });
      }
      if (u === `${BASE}/api/uploads`) return Response.json({ nope: true });
      throw new Error(`unexpected fetch ${u}`);
    });
    const r = await uploadLumoImage({
      url: 'https://octo.example.com/file/a.png',
      baseUrl: BASE,
      botToken: 'octo-token',
      apiUrl: 'https://octo.example.com',
    });
    expect('error' in r && /id\/url/.test(r.error)).toBe(true);
  });
});

describe('queryLumo attachments passthrough', () => {
  const att = { id: 'h1', url: '/uploads/h1.png', fileName: 'a.png', mimeType: 'image/png' };

  it('⑭ carries attachments in the stream request body (camelCase)', async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      streamBodies.push(JSON.parse(String(init?.body)) as { conversationId?: string; content?: string });
      return sse([chunk('seen'), done]);
    });
    const out = await collect(
      queryLumo('what is this', cfg({ baseUrl: BASE }), undefined, undefined, {
        attachments: [att],
      }),
    );
    expect(out.join('')).toBe('seen');
    expect(streamBodies[0]).toMatchObject({
      conversationId: 'c1',
      content: 'what is this',
      attachments: [att],
    });
  });

  it('⑮ omits the attachments field when none are provided', async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'c1' });
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      expect('attachments' in body).toBe(false);
      return sse([chunk('ok'), done]);
    });
    await collect(queryLumo('hi', cfg({ baseUrl: BASE })));
  });

  it('⑯ re-sends the SAME attachments on the stale-resume retry path', async () => {
    let streamCalls = 0;
    const seenBodies: Array<{ conversationId?: string; attachments?: unknown[] }> = [];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/conversations')) return Response.json({ id: 'conv-2' });
      streamCalls++;
      const body = JSON.parse(String(init?.body)) as { conversationId?: string; attachments?: unknown[] };
      seenBodies.push(body);
      if (streamCalls === 1) return new Response('gone', { status: 404 });
      return sse([chunk('recovered'), done]);
    });
    const out = await collect(
      queryLumo('describe the image', cfg({ baseUrl: BASE }), undefined, undefined, {
        resume: 'stale-conv',
        onResumeFailed: () => {},
        fallbackRetryPrompt: 'RETRY-PROMPT',
        attachments: [att],
      }),
    );
    expect(out.join('')).toBe('recovered');
    expect(streamCalls).toBe(2);
    // First call (stale resume) carried the attachments…
    expect(seenBodies[0]?.attachments).toEqual([att]);
    // …and the retry carried them too, with the fallback prompt text.
    expect(seenBodies[1]?.conversationId).toBe('conv-2');
    expect(seenBodies[1]?.attachments).toEqual([att]);
  });
});
