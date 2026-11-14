/**
 * Inbound image download for native multimodal (issue #86).
 *
 * Octo delivers images as URLs. To let the agent actually SEE an image (not just
 * a URL string), we download it INTO the session's cwd sandbox so the model can
 * open it with the Read tool (the agent can read image files).
 * Writing under the session cwd means:
 *  - the agent can reach it with a relative path,
 *  - it's isolated per session (same partition as history/memory), and
 *  - the existing 7-day cwd janitor reclaims it — no separate cleanup needed.
 *
 * Reuses the same SSRF/redirect/size hardening as the file-download path.
 *
 * Lumo runtime (#86 extension): the same safety-hardened fetch core
 * (`fetchInboundImageBytes`) also serves the lumo path, which forwards the
 * bytes to Lumo's /api/uploads endpoint instead of writing them to disk —
 * the security envelope (SSRF, per-hop auth scoping, MIME allowlist, size
 * cap) must be identical whichever runtime consumes the image.
 */

import { mkdir, unlink, writeFile } from 'node:fs/promises';
import { join, relative } from 'node:path';
import { randomUUID } from 'node:crypto';
import { assertPublicUrl, fetchWithRedirectGuard } from './url-policy.js';
import { isSameHost } from './inbound.js';

/** Subdir (under the session cwd) where downloaded inbound images land. */
export const INBOUND_MEDIA_DIR = '.cc-octo-media';

/** Per-image hard cap. the vision model rejects very large images; 5 MB
 *  matches the inbound file cap and is comfortably above any real chat image. */
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

/** Max images materialized from a single message (e.g. a RichText with many). */
export const MAX_IMAGES_PER_MESSAGE = 6;

const IMAGE_DOWNLOAD_TIMEOUT_MS = 30_000;

/**
 * Content-Type → file extension for the image formats the model can natively read.
 * A response whose type isn't here is rejected (we don't feed unknown blobs to
 * the model as "images").
 */
const ALLOWED_IMAGE_TYPES: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/jpg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
};

export type ImageDownloadResult =
  | { localPath: string; relPath: string }
  | { error: string };

/** In-memory counterpart of ImageDownloadResult: raw bytes + metadata. */
export type ImageBytesResult =
  | { bytes: Buffer; mime: string; ext: string }
  | { error: string };

/**
 * Safety-hardened image fetch: SSRF check, per-hop credential scoping, MIME
 * allowlist, streaming size cap. Returns the raw image bytes in memory.
 *
 * Shared by BOTH runtimes:
 *  - codex: `downloadInboundImage` calls this, then writes to the session cwd.
 *  - lumo:  the caller forwards the bytes to Lumo's /api/uploads endpoint.
 *
 * On any problem (SSRF reject, non-image type, oversize, HTTP error) returns
 * `{ error }` so the caller can fall back to the URL marker.
 */
export async function fetchInboundImageBytes(params: {
  url: string;
  botToken: string;
  apiUrl: string;
}): Promise<ImageBytesResult> {
  const { url, botToken, apiUrl } = params;

  // SSRF defense — reject private/loopback/link-local (and re-checked per hop
  // inside fetchWithRedirectGuard).
  try {
    await assertPublicUrl(url);
  } catch (err) {
    return { error: `拒绝下载: ${String(err)}` };
  }

  const signal = AbortSignal.timeout(IMAGE_DOWNLOAD_TIMEOUT_MS);
  let resp: Awaited<ReturnType<typeof fetchWithRedirectGuard>>;
  try {
    // Scope Authorization PER HOP: only send the bot token while the current
    // hop is same-host as apiUrl (a redirect to another host drops it).
    resp = await fetchWithRedirectGuard(url, (currentUrl) => {
      const headers: Record<string, string> = {};
      if (isSameHost(currentUrl, apiUrl)) headers.Authorization = `Bearer ${botToken}`;
      return { headers, signal };
    });
  } catch (err) {
    return { error: `下载失败: ${String(err)}` };
  }

  if (!resp.ok) return { error: `下载失败 HTTP ${resp.status}` };

  // Only accept image content types the model can read.
  const rawType = (resp.headers.get('content-type') ?? '').split(';')[0].trim().toLowerCase();
  const ext = ALLOWED_IMAGE_TYPES[rawType];
  if (!ext) return { error: `不支持的图片类型: ${rawType || '未知'}` };

  const body = resp.body;
  if (!body) return { error: '响应无内容' };

  // Stream into memory with a hard size cap.
  const reader = (body as unknown as { getReader: () => ReadableStreamDefaultReader<Uint8Array> }).getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_IMAGE_BYTES) {
        try { reader.cancel(); } catch { /* ignore */ }
        return { error: `图片超过大小上限 ${Math.round(MAX_IMAGE_BYTES / 1024 / 1024)}MB` };
      }
      chunks.push(value);
    }
  } catch (err) {
    return { error: `下载失败: ${String(err)}` };
  }

  if (total === 0) return { error: '图片为空' };

  return { bytes: Buffer.concat(chunks.map((c) => Buffer.from(c))), mime: rawType, ext };
}

/**
 * Download one image URL into `<cwdDir>/.cc-octo-media/`. Returns the absolute
 * path and the path relative to the cwd (what we show the agent). On any
 * problem (SSRF reject, non-image type, oversize, HTTP error) returns
 * `{ error }` so the caller can fall back to the URL marker.
 *
 * Codex-runtime path: the bytes are materialized on disk so the SDK agent can
 * Read them. (The lumo runtime forwards bytes to Lumo's upload API instead —
 * see fetchInboundImageBytes.)
 */
export async function downloadInboundImage(params: {
  url: string;
  cwdDir: string;
  botToken: string;
  apiUrl: string;
}): Promise<ImageDownloadResult> {
  const { url, cwdDir, botToken, apiUrl } = params;

  const r = await fetchInboundImageBytes({ url, botToken, apiUrl });
  if ('error' in r) return { error: r.error };

  const dir = join(cwdDir, INBOUND_MEDIA_DIR);
  await mkdir(dir, { recursive: true });
  const localPath = join(dir, `${randomUUID()}.${r.ext}`);

  try {
    await writeFile(localPath, r.bytes);
  } catch (err) {
    await unlink(localPath).catch(() => {});
    return { error: `下载失败: ${String(err)}` };
  }

  return { localPath, relPath: relative(cwdDir, localPath) };
}
