/**
 * Workbench auth (#24).
 *
 * Every `/workbench/api/*` request must carry the caller's Octo session token
 * in a BARE `token` header (NOT Authorization — that is Octo's convention, see
 * octo-server's AuthMiddleware). We resolve the token to a uid by calling
 * `GET <config.apiUrl>/v1/user/current` with the same `token` header, then the
 * route layer compares that uid against the target bot's `ownerUid` (403 on
 * mismatch). `/workbench/api/bots` only needs a valid user.
 *
 * Token → uid results are cached in-process for `ttlMs` (default 60s) so a
 * page load (many API calls) costs ONE upstream round-trip per token.
 *
 * Failure policy is fail-closed: any upstream error, malformed response, or
 * missing uid → 401. The workbench must never grant access on ambiguity.
 */

import type { IncomingMessage } from 'node:http';

/** Auth failures carry the HTTP status the route layer should respond with. */
export class AuthError extends Error {
  constructor(
    public readonly status: 401 | 403,
    message: string,
  ) {
    super(message);
    this.name = 'AuthError';
  }
}

export interface TokenAuthenticatorOptions {
  /** Resolve the Octo API base URL at call time (re-read, never stale). */
  apiUrl: () => string;
  /** Cache TTL in ms. Default 60_000 (task contract). */
  ttlMs?: number;
  /** Upstream request timeout in ms. Default 8_000. */
  timeoutMs?: number;
  /** fetch injection (tests). Defaults to global fetch. */
  fetchImpl?: typeof fetch;
  /** Clock injection (tests). Defaults to Date.now. */
  now?: () => number;
}

interface CacheEntry {
  uid: string;
  /** Epoch ms after which the entry must be re-validated upstream. */
  exp: number;
}

const DEFAULT_TTL_MS = 60_000;
const DEFAULT_TIMEOUT_MS = 8_000;
/** Bound the in-process cache; a sweep runs when this is exceeded. */
const MAX_CACHE_ENTRIES = 4096;
/**
 * Short negative-cache TTL for tokens the upstream REJECTED (401). Without it,
 * a flood of requests carrying the same invalid token would each trigger a
 * fresh upstream round-trip (amplification against octo-server's auth
 * endpoint). 5s is short enough that a token becoming valid is visible almost
 * immediately, yet long enough to collapse a retry storm into one upstream
 * call per token. Trade-off: fail-closed is unchanged (we only remember
 * denials, never grants), at the cost of up to 5s of staleness for a token
 * that flips invalid→valid.
 */
const NEGATIVE_TTL_MS = 5_000;

/**
 * Extract the uid from a `/v1/user/current` response body. Octo returns the
 * user detail object directly (`{"uid": "u123", …}`); some deployments wrap
 * payloads in an envelope, so `data.uid` is accepted as a fallback. A non-zero
 * `code` envelope marks an error response even if it came back 200.
 */
export function extractUid(body: unknown): string | undefined {
  if (body === null || typeof body !== 'object') return undefined;
  const obj = body as Record<string, unknown>;
  // Envelope error: { code: <non-zero>, msg: ... } without a usable uid.
  if (typeof obj.code === 'number' && obj.code !== 0) return undefined;
  const direct = obj.uid;
  if (typeof direct === 'string' && direct.length > 0) return direct;
  const data = obj.data;
  if (data !== null && typeof data === 'object') {
    const nested = (data as Record<string, unknown>).uid;
    if (typeof nested === 'string' && nested.length > 0) return nested;
  }
  return undefined;
}

export class TokenAuthenticator {
  private readonly cache = new Map<string, CacheEntry>();
  /** token → epoch ms until which an upstream 401 verdict is remembered. */
  private readonly negative = new Map<string, number>();
  private readonly ttlMs: number;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly now: () => number;

  constructor(private readonly opts: TokenAuthenticatorOptions) {
    this.ttlMs = opts.ttlMs ?? DEFAULT_TTL_MS;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.now = opts.now ?? Date.now;
  }

  /**
   * Resolve the request's `token` header to an Octo uid.
   *
   * @throws AuthError(401) on missing/invalid token or any upstream failure.
   */
  async authenticate(req: IncomingMessage): Promise<string> {
    // Node lowercases header names; `token` may arrive as a string or array.
    const raw = req.headers['token'];
    const token = Array.isArray(raw) ? raw[0] : raw;
    if (!token || token.trim() === '') {
      throw new AuthError(401, 'missing token');
    }

    const nowMs = this.now();
    const hit = this.cache.get(token);
    if (hit) {
      if (hit.exp > nowMs) return hit.uid;
      this.cache.delete(token); // expired — drop before re-validating
    }

    // Recently-rejected token → fail closed immediately, sparing upstream.
    const negExp = this.negative.get(token);
    if (negExp !== undefined) {
      if (negExp > nowMs) throw new AuthError(401, 'invalid token');
      this.negative.delete(token);
    }

    let apiUrl: string;
    try {
      apiUrl = this.opts.apiUrl().replace(/\/+$/, '');
    } catch {
      // Config unreadable (e.g. mid-rewrite by the watcher) — fail closed.
      throw new AuthError(401, 'token validation unavailable');
    }

    let resp: Response;
    try {
      resp = await this.fetchImpl(`${apiUrl}/v1/user/current`, {
        method: 'GET',
        // Octo reads the session token from the bare `token` header.
        headers: { token },
        // Never follow redirects with the credential — the apiUrl host is the
        // only legitimate consumer of this token.
        redirect: 'manual',
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch {
      throw new AuthError(401, 'token validation failed (upstream unreachable)');
    }

    // Non-2xx OR a redirect = not an authenticated user.
    if (!resp.ok) {
      this.rememberNegative(token, nowMs);
      throw new AuthError(401, 'invalid token');
    }

    let uid: string | undefined;
    try {
      uid = extractUid((await resp.json()) as unknown);
    } catch {
      throw new AuthError(401, 'invalid token (malformed upstream response)');
    }
    if (!uid) {
      throw new AuthError(401, 'invalid token (no uid)');
    }

    // Keep the Map bounded under many distinct tokens. Opportunistically drop
    // expired entries first; if the cap is STILL reached (every cached token
    // valid — e.g. a flood of fresh tokens), unconditionally evict the OLDEST
    // entry: Map iteration order is insertion order, so keys().next() is it.
    // This guarantees the cache never grows past MAX_CACHE_ENTRIES.
    if (this.cache.size >= MAX_CACHE_ENTRIES) {
      for (const [k, v] of this.cache) {
        if (v.exp <= nowMs) this.cache.delete(k);
      }
      if (this.cache.size >= MAX_CACHE_ENTRIES) {
        const oldest = this.cache.keys().next();
        if (!oldest.done) this.cache.delete(oldest.value);
      }
    }
    this.cache.set(token, { uid, exp: nowMs + this.ttlMs });
    return uid;
  }

  /** Bounded negative-cache insert (same oldest-eviction policy). */
  private rememberNegative(token: string, nowMs: number): void {
    if (this.negative.size >= MAX_CACHE_ENTRIES) {
      for (const [k, exp] of this.negative) {
        if (exp <= nowMs) this.negative.delete(k);
      }
      if (this.negative.size >= MAX_CACHE_ENTRIES) {
        const oldest = this.negative.keys().next();
        if (!oldest.done) this.negative.delete(oldest.value);
      }
    }
    this.negative.set(token, nowMs + NEGATIVE_TTL_MS);
  }

  /**
   * Ownership gate for per-bot routes: the authenticated uid must equal the
   * bot's registered ownerUid. A bot without a known owner 403s everyone
   * (fail closed).
   *
   * @throws AuthError(403) when the caller is not the owner.
   */
  assertOwner(uid: string, botOwnerUid: string | undefined): void {
    if (!botOwnerUid || uid !== botOwnerUid) {
      throw new AuthError(403, 'not the owner of this bot');
    }
  }

  /** Test/ops: number of currently cached (possibly expired) tokens. */
  cacheSize(): number {
    return this.cache.size;
  }

  /** Test/ops: number of negatively-cached (rejected) tokens. */
  negativeCacheSize(): number {
    return this.negative.size;
  }
}
