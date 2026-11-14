/**
 * #32 Delivery-card hook — announce files a turn produced in the session sandbox.
 *
 * Flow (wired in index.ts handleMessage):
 *   1. BEFORE the agent turn: `snapshotTree()` records `relPath → mtimeMs` for
 *      every file in the session cwd (bounded depth/node budget, dot-dirs like
 *      `.cc-octo-sessions` skipped).
 *   2. AFTER a successful turn: re-snapshot, `diffSnapshots()` reports files
 *      added or whose mtime changed, and `deliverArtifacts()` posts ONE delivery
 *      message to the originating conversation:
 *        - preferred: InteractiveCard(=17) via sendPayload;
 *        - fallback: plain text with the absolute deep link via sendMessage
 *          (e.g. when the server rejects the card — BotEnabled gate off).
 *
 * Card shape follows the verified octo-server contract (pkg/cardmsg): envelope
 * `{type:17, card:<AdaptiveCard>, plain, card_version:"1.5", profile:"octo/v1"}`
 * — `render_profile` is server-injected and MUST NOT be sent; card root uses
 * `body` / `actions`; only whitelist elements (TextBlock/FactSet) and
 * Action.OpenUrl with an absolute http(s) url; ≤200 nodes, depth ≤16.
 */

import { listTree } from './workbench/fs-api.js';
import { sendPayload, sendMessage } from './octo/api.js';
import type { ChannelType } from './octo/types.js';

/** InteractiveCard message type (wire const; octo-web MessageContentTypeConst). */
export const MESSAGE_TYPE_INTERACTIVE_CARD = 17;

/** Card profile / version accepted by octo-server pkg/cardmsg (Decision 10). */
export const CARD_PROFILE = 'octo/v1';
export const CARD_VERSION = '1.5';

/** Server walker budgets (pkg/cardmsg/cardmsg.go MaxNodes / MaxDepth). */
export const MAX_CARD_NODES = 200;

/** Max files listed inside the card; beyond this only a "等 n 个文件" tail. */
export const MAX_CARD_FILE_ROWS = 10;

/**
 * snapshotTree budget: agent-produced trees can be arbitrarily hostile; the
 * walk stops collecting once this many entries were visited (bounded cost per
 * turn, and the card only ever shows the first 10 anyway).
 */
export const SNAPSHOT_MAX_NODES = 200;

/** Snapshot of one session sandbox: regular-file relPath → mtime (ms). */
export type TreeSnapshot = Map<string, number>;

/**
 * Walk the session sandbox `<cwdBase>/<sessionHex>` level by level (reusing
 * workbench fs-api's jailed `listTree`) and collect `relPath → mtimeMs` for
 * every regular file.
 *
 * Bounds: recursion stops at `maxDepth` levels below the session root
 * (default 3) and after SNAPSHOT_MAX_NODES visited entries. Names starting
 * with '.' (e.g. the `.cc-octo-sessions` registry dir) are skipped. relPaths
 * use '/' separators regardless of host OS (they feed URL query params).
 * Never throws — an unreadable tree yields an empty snapshot (the caller
 * treats that as "nothing to diff against").
 */
export function snapshotTree(
  cwdBase: string,
  sessionHex: string,
  maxDepth = 3,
): TreeSnapshot {
  const out: TreeSnapshot = new Map();
  let budget = SNAPSHOT_MAX_NODES;

  const walk = (relDir: string, depth: number): void => {
    if (budget <= 0) return;
    let entries;
    try {
      entries = listTree(cwdBase, sessionHex, relDir);
    } catch {
      return; // raced away / unreadable — partial snapshot is fine
    }
    for (const entry of entries) {
      if (budget <= 0) return;
      budget--;
      if (entry.name.startsWith('.')) continue; // dot-dirs & dot-files: not deliverables
      const rel = relDir === '' ? entry.name : `${relDir}/${entry.name}`;
      if (entry.type === 'dir') {
        // depth counts path segments below the session root; dirs recurse
        // while their children stay within maxDepth.
        if (depth < maxDepth) walk(rel, depth + 1);
        continue;
      }
      if (depth > maxDepth) continue;
      // listTree reports lstat mtime as an ISO string; convert back to ms.
      const mtimeMs = Date.parse(entry.mtime);
      if (!Number.isNaN(mtimeMs)) out.set(rel, mtimeMs);
    }
  };

  walk('', 1);
  return out;
}

/**
 * Files added or modified between two snapshots (files only — snapshots never
 * contain directory entries; deletions are NOT deliverables and are ignored).
 * Sorted for stable display order.
 */
export function diffSnapshots(before: TreeSnapshot, after: TreeSnapshot): string[] {
  const changed: string[] = [];
  for (const [rel, mtime] of after) {
    const prev = before.get(rel);
    if (prev === undefined || prev !== mtime) changed.push(rel);
  }
  changed.sort();
  return changed;
}

/**
 * Escape a sandbox-derived file name for AC markdown surfaces (TextBlock.text
 * / Fact title+value all render an AC markdown subset). A hostile file name
 * like `x](javascript:alert(1))` assembled into `[…](…)` shapes would inject a
 * link the server's allowlist either rejects (whole card fails) or — for
 * absolute http targets — renders live. Backslash-escaping the bracket pair
 * neutralizes link/image syntax without changing the visible text.
 */
function escapeCardText(s: string): string {
  return s.replace(/([\\[\]])/g, '\\$1');
}

/** The full InteractiveCard(=17) message payload for /v1/bot/sendMessage. */
export interface DeliveryCardPayload {
  type: number;
  profile: string;
  card_version: string;
  plain: string;
  card: {
    type: 'AdaptiveCard';
    version: string;
    body: Array<Record<string, unknown>>;
    actions: Array<Record<string, unknown>>;
  };
  /** Makes the envelope assignable to the API's free-form payload map. */
  [key: string]: unknown;
}

/**
 * Build the type-17 delivery-card payload per the octo/v1 schema.
 *
 * body: title TextBlock → FactSet (≤ MAX_CARD_FILE_ROWS facts, title='file
 * n', value=relPath) → optional overflow TextBlock ("等 n 个文件");
 * actions: a single Action.OpenUrl whose url is the absolute deep link.
 * `plain` carries the fallback text (digest/preview/copy source).
 */
export function buildDeliveryCard(params: {
  files: string[];
  deepLink: string;
}): DeliveryCardPayload {
  const { files, deepLink } = params;
  const listed = files.slice(0, MAX_CARD_FILE_ROWS);
  const overflow = files.length - listed.length;

  const body: Array<Record<string, unknown>> = [
    {
      type: 'TextBlock',
      text: `📂 本轮交付：${files.length} 个文件`,
      weight: 'Bolder',
      size: 'Medium',
    },
    {
      type: 'FactSet',
      facts: listed.map((rel, i) => ({
        title: `文件 ${i + 1}`,
        value: escapeCardText(rel),
      })),
    },
  ];
  if (overflow > 0) {
    body.push({ type: 'TextBlock', text: `等 ${files.length} 个文件（仅显示前 ${MAX_CARD_FILE_ROWS} 个）`, isSubtle: true });
  }

  const plain = buildFallbackText(files, deepLink);
  return {
    type: MESSAGE_TYPE_INTERACTIVE_CARD,
    profile: CARD_PROFILE,
    card_version: CARD_VERSION,
    plain,
    // render_profile is deliberately absent — the server injects it after
    // cardmsg.Validate (send.go); callers must not send it.
    card: {
      type: 'AdaptiveCard',
      version: CARD_VERSION,
      body,
      actions: [{ type: 'Action.OpenUrl', title: '在工作台查看', url: deepLink }],
    },
  };
}

/**
 * Plain-text delivery message (also the card's `plain`):
 * `📂 本轮交付：n 个文件` + one line per file (≤10, then "…等共 n 个文件") +
 * the deep link on its OWN line.
 */
export function buildFallbackText(files: string[], deepLink: string): string {
  const lines: string[] = [`📂 本轮交付：${files.length} 个文件`];
  const listed = files.slice(0, MAX_CARD_FILE_ROWS);
  for (const rel of listed) lines.push(`• ${rel}`);
  if (files.length > listed.length) {
    lines.push(`…等共 ${files.length} 个文件`);
  }
  lines.push(deepLink); // deep link standalone
  return lines.join('\n');
}

/**
 * Gate predicate (#32): the delivery hook is armed only when `publicBaseUrl`
 * is configured AND `deliveryCard.enabled` is not explicitly false (omitted →
 * true). Pure function so tests can assert the gate without the full pipeline.
 */
export function shouldDeliver(cfg: {
  publicBaseUrl?: string;
  deliveryCard?: { enabled?: boolean };
}): boolean {
  return !!cfg.publicBaseUrl && cfg.deliveryCard?.enabled !== false;
}

/** Deep link into the workbench workspace view for one delivered file. */
export function buildDeepLink(params: {
  publicBaseUrl: string;
  robotUid: string;
  sessionHex: string;
  firstFile: string;
}): string {
  const base = params.publicBaseUrl.replace(/\/+$/, '');
  return (
    `${base}/workspace?bot=${encodeURIComponent(params.robotUid)}` +
    `&session=${encodeURIComponent(params.sessionHex)}` +
    `&path=${encodeURIComponent(params.firstFile)}`
  );
}

/**
 * Post the delivery message for this turn's changed files (fire-and-forget
 * from handleMessage — every failure path here only logs, never throws into
 * the main chain; the catch in deliverArtifacts is the caller's backstop).
 *
 * Re-scans the sandbox and diffs against `before`; no diff → sends nothing.
 * Card first; if sendPayload throws (e.g. server-side BotEnabled gate rejects
 * type-17) fall back to plain text via sendMessage.
 *
 * Returns true when a message was sent (test seam).
 */
export async function deliverArtifacts(params: {
  apiUrl: string;
  botToken: string;
  channelId: string;
  channelType: ChannelType;
  cwdBase: string;
  sessionHex: string;
  before: TreeSnapshot;
  publicBaseUrl: string;
  robotUid: string;
}): Promise<boolean> {
  const after = snapshotTree(params.cwdBase, params.sessionHex);
  const files = diffSnapshots(params.before, after);
  if (files.length === 0) return false;

  const deepLink = buildDeepLink({
    publicBaseUrl: params.publicBaseUrl,
    robotUid: params.robotUid,
    sessionHex: params.sessionHex,
    firstFile: files[0],
  });

  try {
    await sendPayload({
      apiUrl: params.apiUrl,
      botToken: params.botToken,
      channelId: params.channelId,
      channelType: params.channelType,
      payload: buildDeliveryCard({ files, deepLink }),
    });
    return true;
  } catch (err) {
    // Degradation: plain text with the absolute deep link.
    console.warn(
      `[codex-channel-octo] delivery card rejected, falling back to text: ${err instanceof Error ? err.message : String(err)}`,
    );
    await sendMessage({
      apiUrl: params.apiUrl,
      botToken: params.botToken,
      channelId: params.channelId,
      channelType: params.channelType,
      content: buildFallbackText(files, deepLink),
    });
    return true;
  }
}
