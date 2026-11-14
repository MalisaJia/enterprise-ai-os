/**
 * Tests for delivery-card.ts (#32): sandbox snapshot/diff, octo/v1 card
 * payload shape, fallback text, deep links, and deliverArtifacts' card →
 * text degradation over the mocked /v1/bot/sendMessage endpoint.
 */

import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  snapshotTree,
  diffSnapshots,
  buildDeliveryCard,
  buildFallbackText,
  buildDeepLink,
  deliverArtifacts,
  shouldDeliver,
  MESSAGE_TYPE_INTERACTIVE_CARD,
  CARD_PROFILE,
  CARD_VERSION,
  MAX_CARD_FILE_ROWS,
  SNAPSHOT_MAX_NODES,
  type TreeSnapshot,
} from '../delivery-card.js';
import { ChannelType, MessageType } from '../octo/types.js';
import { loadConfig } from '../config.js';

// Whitelist shapes per octo-server pkg/cardmsg (whitelist.go) — the card we
// emit must only ever use these element / action types.
const WHITELIST_ELEMENTS = new Set([
  'TextBlock', 'Image', 'Container', 'ColumnSet', 'Column', 'FactSet',
]);
const WHITELIST_ACTIONS = new Set(['Action.OpenUrl']);

// ─── fixtures ───────────────────────────────────────────────────────────────

let tmpBase: string;

beforeEach(() => {
  tmpBase = mkdtempSync(join(tmpdir(), 'cc-delivery-'));
});

afterEach(() => {
  rmSync(tmpBase, { recursive: true, force: true });
});

const SESSION_HEX = 'a1b2c3d4e5f60718';

/** Create <tmpBase>/<SESSION_HEX>/<rel> with content. */
function sandboxFile(rel: string, content = 'x'): string {
  const p = join(tmpBase, SESSION_HEX, ...rel.split('/'));
  mkdirSync(join(p, '..'), { recursive: true });
  writeFileSync(p, content);
  return p;
}

// ─── snapshotTree ───────────────────────────────────────────────────────────

describe('snapshotTree', () => {
  it('collects relPath → mtimeMs for regular files with / separators', () => {
    sandboxFile('report.md');
    sandboxFile('src/main.ts');

    const snap = snapshotTree(tmpBase, SESSION_HEX);
    expect([...snap.keys()].sort()).toEqual(['report.md', 'src/main.ts']);
    for (const mtime of snap.values()) {
      expect(Number.isFinite(mtime)).toBe(true);
    }
  });

  it('skips dot-files and dot-dirs (registry, caches are not deliverables)', () => {
    sandboxFile('.hidden');
    sandboxFile('.cc-octo-sessions/registry.json');
    sandboxFile('visible.txt');

    const snap = snapshotTree(tmpBase, SESSION_HEX);
    expect([...snap.keys()]).toEqual(['visible.txt']);
  });

  it('stops at maxDepth (default 3) — deeper files are invisible to the diff', () => {
    // walk('' ,1) → dirs recursed while depth < maxDepth: files at path depth
    // ≤ 3 are seen; anything nested deeper is never listed.
    sandboxFile('l1/l2/ok.txt');        // path depth 3 → included
    sandboxFile('l1/l2/l3/deep.txt');   // path depth 4 → excluded

    const snap = snapshotTree(tmpBase, SESSION_HEX);
    expect(snap.has('l1/l2/ok.txt')).toBe(true);
    expect(snap.has('l1/l2/l3/deep.txt')).toBe(false);
  });

  it('bounds the walk at SNAPSHOT_MAX_NODES entries', () => {
    for (let i = 0; i < SNAPSHOT_MAX_NODES + 40; i++) {
      sandboxFile(`f${String(i).padStart(3, '0')}.txt`);
    }
    const snap = snapshotTree(tmpBase, SESSION_HEX);
    expect(snap.size).toBeLessThanOrEqual(SNAPSHOT_MAX_NODES);
  });

  it('never throws — a missing session dir yields an empty snapshot', () => {
    expect(snapshotTree(tmpBase, 'deadbeefdeadbeef')).toEqual(new Map());
  });
});

// ─── diffSnapshots ──────────────────────────────────────────────────────────

describe('diffSnapshots', () => {
  it('reports added files', () => {
    const before: TreeSnapshot = new Map([['a.txt', 1]]);
    const after: TreeSnapshot = new Map([['a.txt', 1], ['b.txt', 2]]);
    expect(diffSnapshots(before, after)).toEqual(['b.txt']);
  });

  it('reports modified files (mtime changed)', () => {
    const before: TreeSnapshot = new Map([['a.txt', 100]]);
    const after: TreeSnapshot = new Map([['a.txt', 200]]);
    expect(diffSnapshots(before, after)).toEqual(['a.txt']);
  });

  it('ignores unchanged and deleted files', () => {
    const before: TreeSnapshot = new Map([['keep.txt', 1], ['gone.txt', 1]]);
    const after: TreeSnapshot = new Map([['keep.txt', 1]]);
    expect(diffSnapshots(before, after)).toEqual([]);
  });

  it('sorts for stable display order', () => {
    const before: TreeSnapshot = new Map();
    const after: TreeSnapshot = new Map([['z.md', 1], ['a.md', 1], ['m.md', 1]]);
    expect(diffSnapshots(before, after)).toEqual(['a.md', 'm.md', 'z.md']);
  });
});

// ─── buildDeepLink ──────────────────────────────────────────────────────────

describe('buildDeepLink', () => {
  it('matches the documented workbench workspace URL shape', () => {
    const link = buildDeepLink({
      publicBaseUrl: 'https://wb.example.com',
      robotUid: 'u_bot1',
      sessionHex: SESSION_HEX,
      firstFile: 'report.md',
    });
    expect(link).toBe(
      `https://wb.example.com/workspace?bot=u_bot1&session=${SESSION_HEX}&path=report.md`,
    );
  });

  it('strips trailing slashes and URL-encodes each query value', () => {
    const link = buildDeepLink({
      publicBaseUrl: 'https://wb.example.com///',
      robotUid: 'u bot&1',
      sessionHex: SESSION_HEX,
      firstFile: 'src/报告 final.md',
    });
    expect(link.startsWith('https://wb.example.com/workspace?')).toBe(true);
    expect(link).toContain(`bot=${encodeURIComponent('u bot&1')}`);
    expect(link).toContain(`path=${encodeURIComponent('src/报告 final.md')}`);
    expect(link).not.toContain('///');
  });
});

// ─── buildFallbackText ──────────────────────────────────────────────────────

describe('buildFallbackText', () => {
  it('puts the deep link on its OWN (last) line', () => {
    const text = buildFallbackText(['a.md', 'b.md'], 'https://wb.example.com/workspace?x=1');
    const lines = text.split('\n');
    expect(lines[0]).toContain('2 个文件');
    expect(lines[1]).toBe('• a.md');
    expect(lines[2]).toBe('• b.md');
    expect(lines[lines.length - 1]).toBe('https://wb.example.com/workspace?x=1');
  });

  it('lists at most MAX_CARD_FILE_ROWS files, then an overflow marker', () => {
    const files = Array.from({ length: MAX_CARD_FILE_ROWS + 5 }, (_, i) => `f${i}.txt`);
    const text = buildFallbackText(files, 'https://wb.example.com/l');
    const lines = text.split('\n');
    const bullets = lines.filter((l) => l.startsWith('• '));
    expect(bullets).toHaveLength(MAX_CARD_FILE_ROWS);
    expect(text).toContain(`…等共 ${files.length} 个文件`);
    expect(lines[lines.length - 1]).toBe('https://wb.example.com/l');
  });
});

// ─── buildDeliveryCard (octo/v1 whitelist shape) ────────────────────────────

describe('buildDeliveryCard', () => {
  const deepLink = 'https://wb.example.com/workspace?bot=u1&session=ab&path=f.md';

  it('envelope: type=17, profile octo/v1, card_version 1.5, plain, NO render_profile', () => {
    const card = buildDeliveryCard({ files: ['f.md'], deepLink });
    expect(card.type).toBe(MESSAGE_TYPE_INTERACTIVE_CARD);
    expect(card.profile).toBe(CARD_PROFILE);
    expect(card.card_version).toBe(CARD_VERSION);
    expect(card.plain).toBe(buildFallbackText(['f.md'], deepLink));
    // render_profile is server-injected after cardmsg.Validate — never sent.
    expect('render_profile' in card).toBe(false);
  });

  it('card root has AdaptiveCard body + actions; every element is whitelisted', () => {
    const card = buildDeliveryCard({ files: ['a.md', 'b.md'], deepLink });
    expect(card.card.type).toBe('AdaptiveCard');
    expect(card.card.version).toBe(CARD_VERSION);
    expect(card.card.body.length).toBeGreaterThan(0);
    for (const el of card.card.body) {
      expect(WHITELIST_ELEMENTS.has(el.type as string)).toBe(true);
    }
  });

  it('actions: exactly ONE Action.OpenUrl whose url is the absolute deep link', () => {
    const card = buildDeliveryCard({ files: ['a.md'], deepLink });
    expect(card.card.actions).toHaveLength(1);
    const action = card.card.actions[0];
    expect(WHITELIST_ACTIONS.has(action.type as string)).toBe(true);
    expect(action.type).toBe('Action.OpenUrl');
    expect(action.url).toBe(deepLink);
    expect(/^https?:\/\//.test(action.url as string)).toBe(true);
  });

  it('FactSet lists ≤10 rows; >10 files adds an overflow TextBlock', () => {
    const files = Array.from({ length: MAX_CARD_FILE_ROWS + 3 }, (_, i) => `f${i}.txt`);
    const card = buildDeliveryCard({ files, deepLink });
    const factSet = card.card.body.find((el) => el.type === 'FactSet') as
      | { facts: Array<{ title: string; value: string }> }
      | undefined;
    expect(factSet).toBeDefined();
    expect(factSet!.facts).toHaveLength(MAX_CARD_FILE_ROWS);
    const overflow = card.card.body.filter((el) => el.type === 'TextBlock');
    expect(overflow.length).toBeGreaterThanOrEqual(2); // title + overflow note
  });

  it('escapes bracket syntax in file names (no link injection into AC markdown)', () => {
    const card = buildDeliveryCard({ files: ['x[y](javascript:alert(1)).md'], deepLink });
    const factSet = card.card.body.find((el) => el.type === 'FactSet') as {
      facts: Array<{ value: string }>;
    };
    // Both brackets are backslash-escaped, so CommonMark sees no `[…](…)` link
    // construct: no UNESCAPED '](' may survive in any markdown surface.
    expect(factSet.facts[0].value).toContain('\\]');
    expect(factSet.facts[0].value).toContain('\\[');
    expect(factSet.facts[0].value).not.toMatch(/(?<!\\)\]\(/);
  });

  it('stays within the server node budget even for the capped worst case', () => {
    const files = Array.from({ length: 50 }, (_, i) => `f${i}.txt`);
    const card = buildDeliveryCard({ files, deepLink });
    // title TextBlock + FactSet(+10 facts) + overflow TextBlock + 1 action
    const nodes =
      card.card.body.length +
      (card.card.body.find((el) => el.type === 'FactSet') as { facts: unknown[] })
        .facts.length +
      card.card.actions.length;
    expect(nodes).toBeLessThanOrEqual(200); // pkg/cardmsg MaxNodes
  });
});

// ─── deliverArtifacts (fetch-mocked end-to-end) ─────────────────────────────

describe('deliverArtifacts', () => {
  const fetchMock = vi.fn();
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  afterAll(() => {
    globalThis.fetch = originalFetch;
  });

  const base = {
    apiUrl: 'https://octo.example.com',
    botToken: 'bf_test',
    channelId: 'gid-1',
    channelType: ChannelType.Group,
    cwdBase: '',
    sessionHex: SESSION_HEX,
    publicBaseUrl: 'https://wb.example.com',
    robotUid: 'u_bot',
  };

  function jsonResponse(body: unknown): Response {
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  it('no diff → sends nothing (returns false, zero HTTP calls)', async () => {
    sandboxFile('untouched.txt');
    const before = snapshotTree(tmpBase, SESSION_HEX);

    const sent = await deliverArtifacts({ ...base, cwdBase: tmpBase, before });
    expect(sent).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('new file → ONE type-17 card posted to /v1/bot/sendMessage', async () => {
    const before = snapshotTree(tmpBase, SESSION_HEX);
    sandboxFile('out/report.md');
    fetchMock.mockResolvedValueOnce(jsonResponse({ message_id: '1' }));

    const sent = await deliverArtifacts({ ...base, cwdBase: tmpBase, before });
    expect(sent).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://octo.example.com/v1/bot/sendMessage');
    expect(init.headers.Authorization).toBe('Bearer bf_test');
    const body = JSON.parse(init.body);
    expect(body.channel_id).toBe('gid-1');
    expect(body.channel_type).toBe(ChannelType.Group);
    expect(body.client_msg_no).toBeTruthy();
    expect(body.payload.type).toBe(MESSAGE_TYPE_INTERACTIVE_CARD);
    expect(body.payload.profile).toBe(CARD_PROFILE);
    expect(body.payload.card_version).toBe(CARD_VERSION);
    expect('render_profile' in body.payload).toBe(false);
    expect(body.payload.card.actions[0].url).toBe(
      `https://wb.example.com/workspace?bot=u_bot&session=${SESSION_HEX}&path=out%2Freport.md`,
    );
  });

  it('card rejected (4xx) → degrades to plain text with the standalone deep link', async () => {
    const before = snapshotTree(tmpBase, SESSION_HEX);
    sandboxFile('notes.md');
    // First call (card) rejected — e.g. cardmsg.BotEnabled gate off.
    fetchMock.mockResolvedValueOnce(new Response('card disabled', { status: 400, statusText: 'Bad Request' }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ message_id: '2' }));

    const sent = await deliverArtifacts({ ...base, cwdBase: tmpBase, before });
    expect(sent).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const body2 = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(body2.payload.type).toBe(MessageType.Text);
    const content = body2.payload.content as string;
    const lines = content.split('\n');
    expect(lines[lines.length - 1]).toBe(
      `https://wb.example.com/workspace?bot=u_bot&session=${SESSION_HEX}&path=notes.md`,
    );
  });
});

// ─── gate (shouldDeliver) ────────────────────────────────────────────────

describe('shouldDeliver gate', () => {
  it('off without publicBaseUrl — nothing is ever sent', () => {
    expect(shouldDeliver({})).toBe(false);
    expect(shouldDeliver({ deliveryCard: { enabled: true } })).toBe(false);
  });

  it('on by default when publicBaseUrl is configured', () => {
    expect(shouldDeliver({ publicBaseUrl: 'https://wb.example.com' })).toBe(true);
    expect(
      shouldDeliver({ publicBaseUrl: 'https://wb.example.com', deliveryCard: {} }),
    ).toBe(true);
  });

  it('deliveryCard.enabled=false disarms the hook', () => {
    expect(
      shouldDeliver({ publicBaseUrl: 'https://wb.example.com', deliveryCard: { enabled: false } }),
    ).toBe(false);
  });
});

// ─── loadConfig validation ─────────────────────────────────────────────────

describe('loadConfig publicBaseUrl / deliveryCard', () => {
  let cfgDir: string;

  beforeEach(() => {
    cfgDir = mkdtempSync(join(tmpdir(), 'cc-delivery-cfg-'));
  });
  afterEach(() => {
    rmSync(cfgDir, { recursive: true, force: true });
  });

  function writeCfg(extra: Record<string, unknown>): string {
    const p = join(cfgDir, 'config.json');
    writeFileSync(
      p,
      JSON.stringify({ botToken: 'bf_test', apiUrl: 'https://api.test', ...extra }),
    );
    return p;
  }

  it('accepts an absolute http(s) publicBaseUrl', () => {
    const cfg = loadConfig(writeCfg({ publicBaseUrl: 'https://wb.example.com' }));
    expect(cfg.publicBaseUrl).toBe('https://wb.example.com');
    expect(shouldDeliver(cfg)).toBe(true);
  });

  it('rejects a relative publicBaseUrl at load', () => {
    expect(() => loadConfig(writeCfg({ publicBaseUrl: '/workbench' }))).toThrow(
      /publicBaseUrl/,
    );
  });

  it('rejects a non-http(s) scheme at load', () => {
    expect(() =>
      loadConfig(writeCfg({ publicBaseUrl: 'javascript:alert(1)' })),
    ).toThrow(/publicBaseUrl/);
  });

  it('deliveryCard.enabled=false survives load and disarms the gate', () => {
    const cfg = loadConfig(
      writeCfg({
        publicBaseUrl: 'https://wb.example.com',
        deliveryCard: { enabled: false },
      }),
    );
    expect(cfg.deliveryCard).toEqual({ enabled: false });
    expect(shouldDeliver(cfg)).toBe(false);
  });
});
