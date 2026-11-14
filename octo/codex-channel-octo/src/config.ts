/**
 * Configuration loading.
 *
 * Two-layer, bot-first model:
 *  - GLOBAL `~/.codex-channel-octo/config.json` — shared defaults + a `bots` list.
 *    Never holds a botToken.
 *  - PER-BOT `~/.codex-channel-octo/<id>/config.json` — that bot's botToken + any
 *    overrides. Each bot is a self-contained subtree:
 *      <baseDir>/<id>/{config.json, SOUL.md, data/, workspace/, memory/}
 *  - `baseDir` is the directory containing the global config.json. Per-bot dirs
 *    are DERIVED from `<baseDir>/<id>/…` (not separately configurable) so a bot
 *    can never point its data outside its own subtree.
 *
 * env overrides still apply to the shared/global layer.
 */

import { readFileSync, existsSync, statSync, realpathSync } from 'node:fs';
import { resolve as resolvePath, sep, dirname, join as pathJoin, isAbsolute } from 'node:path';
import { homedir } from 'node:os';
import { isAllowedApiUrl } from './url-policy.js';

/**
 * Default global config path: `~/.codex-channel-octo/config.json`. This is the
 * single, fixed production location (no env/CLI override). Tests pass an
 * explicit path, which also sets `baseDir` to that file's directory.
 */
export const DEFAULT_CONFIG_PATH = pathJoin(homedir(), '.codex-channel-octo', 'config.json');

export interface Config {
  botToken: string;
  apiUrl: string;
  /**
   * Base directory containing the global config.json. Every bot's subtree lives
   * at `<baseDir>/<botId>/…`. Defaults to `~/.codex-channel-octo` (the dir of
   * DEFAULT_CONFIG_PATH); when an explicit config path is passed, it is that
   * file's directory.
   */
  baseDir: string;
  /**
   * DERIVED (not user-configurable): per-session cwd sandbox base for THIS bot,
   * `<baseDir>/<botId>/workspace`. Each (DM peer | group channel) gets its own
   * hashed subdir under it via `cwd-resolver.resolveSessionCwd()`. Populated by
   * `resolveBotConfigs()`.
   */
  cwdBase?: string;
  /**
   * @deprecated Alias of `cwdBase`, kept in sync so hand-built Config objects
   * (tests, legacy consumers reading `config.cwd`) still compile.
   */
  cwd: string;
  /**
   * DERIVED (not user-configurable): SQLite/data dir for THIS bot,
   * `<baseDir>/<botId>/data`. Populated by `resolveBotConfigs()`.
   */
  dataDir: string;
  /**
   * DERIVED (not user-configurable): SDK auto-memory base for THIS bot,
   * `<baseDir>/<botId>/memory`. Each session gets a hashed subdir under it (same
   * partitioning as the cwd sandbox: group=shared per channel, DM=private per
   * peer). Separate from the cwd sandbox so the 7-day cwd TTL never reclaims
   * memory. Populated by `resolveBotConfigs()`.
   */
  memoryBase?: string;
  /**
   * v1.0: directory of per-group instruction files (`<groupId>.md`). When set,
   * a matching file's contents are injected into the system prompt as trusted
   * custom instructions for that group. Operator-controlled — must NOT be the
   * per-session cwd sandbox (which the agent can write). Unset = feature off.
   */
  groupConfigDir?: string;
  sdk: {
    /** Codex model id (e.g. gpt-5.5). Omitted → codex default. */
    model?: string;
    /**
     * Codex approval policy: 'never' | 'on-request' | 'on-failure' | 'untrusted'.
     * Default 'never' (headless — no human to answer prompts). NOTE: under
     * headless operation approval cannot be satisfied interactively, so the real
     * permission boundary is `sandboxMode`, NOT this.
     */
    approvalPolicy?: string;
    /**
     * Codex sandbox: 'read-only' | 'workspace-write' | 'danger-full-access'.
     * Default 'read-only'. IM input is untrusted, so the bot must not let an
     * arbitrary user drive Codex into writing local files by default. To allow
     * the bot to edit code, the operator must BOTH set `allowWorkspaceWrite:true`
     * AND `sandboxMode:'workspace-write'` (double switch, guards against misconfig).
     * 'danger-full-access' is rejected outright (see loadConfig validation).
     */
    sandboxMode?: string;
    /**
     * Gate for workspace-write: even if `sandboxMode:'workspace-write'` is set,
     * it only takes effect when this is true; otherwise it is downgraded to
     * read-only. Default false. The prompt security prefix is a SOFT constraint
     * (a model can be talked around it); the sandbox is the hard boundary.
     */
    allowWorkspaceWrite?: boolean;
    /**
     * Codex reasoning effort: 'minimal' | 'low' | 'medium' | 'high' | 'xhigh'.
     * Default 'medium'. NOTE 'minimal' conflicts with web search — when
     * webSearchEnabled is true, do not pair it with 'minimal'.
     */
    modelReasoningEffort?: string;
    /** Allow the agent network access. Default false (untrusted IM input). */
    networkAccessEnabled?: boolean;
    /** Enable web search tool. Default false. */
    webSearchEnabled?: boolean;
    /** Web search mode: 'disabled' | 'cached' | 'live'. Default 'disabled'. */
    webSearchMode?: string;
    /**
     * Per-bot CODEX_HOME (codex's auth/config/sessions/memory root). Default
     * `<baseDir>/<botId>/codex-home` so each bot is isolated and IM content does
     * NOT land in the operator's personal `~/.codex`. To deliberately share the
     * personal codex (so IM threads can be resumed from your own terminal), set
     * this to the personal `~/.codex` — opt-in only. Populated by
     * resolveBotConfigs() when unset.
     */
    codexHome?: string;
    /** Operator-provided global system instructions (prepended to the prompt). */
    systemPrompt?: string;
    /**
     * When true, the bot sends brief, REDACTED progress messages (command run,
     * file changed, tool called) as Codex works through a turn — so users see
     * activity during long turns. Default false. Env:
     * `CODEX_OCTO_SDK_TOOL_PROGRESS=true`. Progress is field-whitelisted (no
     * stdout/stderr, no diff, no full args) — see agent-bridge.
     */
    toolProgress?: boolean;
    /**
     * Override the Codex upstream base URL (e.g. self-hosted provider gateway).
     * Forwarded to `new Codex({ baseUrl })`.
     */
    codexBaseUrl?: string;
    /**
     * API key for the Codex upstream provider. Forwarded to `new Codex({ apiKey })`.
     * Written by the `configure` subcommand; persisted in the global config.
     */
    codexApiKey?: string;
    /**
     * Extra environment variables injected verbatim into the Codex subprocess (on
     * top of inherited process.env + CODEX_HOME). Generic and declarative. Use it
     * to give a bot's provider routing or its tools the env they need. Per-bot.
     */
    env?: Record<string, string>;
    /**
     * Extra directories added to the Codex sandbox's writable roots (SDK
     * ThreadOptions.additionalDirectories). Only meaningful under
     * sandboxMode:'workspace-write' + allowWorkspaceWrite:true; ignored under
     * read-only. Each entry MUST be an absolute path — relative or '~' entries
     * expand differently per runtime and would silently point at the wrong place
     * or escape the intended boundary. Validated in loadConfig. Default: none.
     * Use to grant the bot write access to a shared dir outside its cwd (e.g. a
     * cross-agent work bus).
     */
    additionalDirectories?: string[];
  };
  rateLimit: {
    maxPerMinute: number;
  };
  context: {
    maxContextChars: number;
    historyLimit: number;
  };
  /** Maximum response length in chars before truncation (Q32). */
  maxResponseChars: number;
  /**
   * Per-message dispatch timeout in ms (#141). Bounds the full handler
   * pipeline (agent query + stream) under the per-session lock. If a turn
   * hangs past this, the session lock is released (a hung turn would otherwise
   * block every subsequent message on that session forever) and the user gets
   * a one-shot apology. Does NOT cancel the in-flight turn — only unblocks the
   * queue. Default 5 minutes.
   */
  dispatchTimeoutMs: number;
  botBlocklist?: string[];
  /**
   * G14: Bots in this list are allowed to DM the bot even if their uid matches
   * the `_bot` heuristic. Use this to whitelist trusted bots.
   */
  allowedBotUids?: string[];
  /** Group IDs where the bot responds without being @mentioned (G12). */
  mentionFreeGroups?: string[];
  /**
   * v0.3 multi-bot: optional per-bot overrides. When present and non-empty, the
   * process runs ONE independent bot per entry, each with its own gateway,
   * router, store, and (by default) data directory — so bots never share history
   * or working dirs. Each entry inherits every top-level field and overrides the
   * listed ones; `botToken` is required per entry. When absent, the process runs
   * a single bot from the top-level fields exactly as before.
   *
   * Resolved into concrete per-bot Config objects by `resolveBotConfigs()`.
   */
  bots?: BotOverride[];
  /**
   * v0.3 multi-bot: stable identifier for THIS bot, used to namespace its data
   * directory and logs when running multiple bots. Defaults to `default` for the
   * single-bot case. Populated by `resolveBotConfigs()`.
   */
  botId?: string;
  /**
   * #86: media CDN host (no scheme), prefetched at startup from the upload-
   * credentials STS response (`cdnBaseUrl`). Octo serves media from a separate
   * CDN host than `apiUrl`; inbound media URLs on this host are allowed by
   * buildMediaUrl. Runtime-populated (not from the config file); undefined until
   * the prefetch succeeds, in which case only same-apiUrl-host media is allowed.
   */
  mediaCdnHost?: string;
  /**
   * #86 follow-up: additional hosts allowed to serve inbound media.
   *
   * `mediaCdnHost` is derived at runtime from the STS upload-credentials
   * response (`cdnBaseUrl`). Deployments whose Octo server has no COS/STS
   * configured (e.g. self-hosted MinIO) never populate it, so EVERY inbound
   * image URL gets rejected by `buildMediaUrl` and the agent only sees a
   * `[图片]` placeholder. This field lets an operator declare the real media
   * host(s) explicitly instead of depending on that prefetch succeeding.
   *
   * Host-only entries (no scheme, no path, no wildcard). Matching is
   * case-insensitive and compares `URL.host` (so a port, if any, must be
   * included). This widens the ALLOW list for media fetches only — the
   * download path still runs `assertPublicUrl` (SSRF) and still sends the
   * bot token only to the apiUrl host.
   */
  mediaAllowedHosts?: string[];
  /**
   * Web workbench (#24): built-in read-only ops console served INSIDE the
   * gateway process (it needs direct BotManager access for per-bot restarts —
   * not a separate entry process). Defaults: enabled=true, listenPort=28090.
   * The server binds 0.0.0.0 because upstream deployments reach it through the
   * docker host-gateway bridge IP, not loopback. See src/workbench/.
   */
  workbench?: {
    /** Master switch. Omitted → enabled (true). */
    enabled?: boolean;
    /** HTTP listen port. Omitted → 28090. */
    listenPort?: number;
  };
  /**
   * #32 delivery card: EXTERNALLY reachable base URL of the workbench web UI
   * (absolute http(s), e.g. https://workbench.example.com). When set, after a
   * turn that produced/modified files in the session sandbox, the bot posts a
   * delivery message (type-17 interactive card, plain-text fallback) to the
   * originating conversation, with deep links pointing here:
   *   <publicBaseUrl>/workspace?bot=<robotUid>&session=<hex>&path=<file>
   * Unset → the delivery-card hook is off. Validated in loadConfig (must be an
   * absolute http(s) URL — it is embedded into card Action.OpenUrl targets,
   * which the server only accepts as absolute http/https).
   */
  publicBaseUrl?: string;
  /**
   * #32 delivery card feature flags. `enabled` omitted → true (the hook is on
   * whenever publicBaseUrl is configured); set { enabled: false } to keep
   * publicBaseUrl (deep links) without auto-posting delivery messages.
   */
  deliveryCard?: {
    enabled?: boolean;
  };
  /**
   * Agent runtime backend. `'codex'` (default, when omitted) drives OpenAI Codex
   * via the SDK (agent-bridge); `'lumo'` drives a Lumo Work server over its HTTP
   * SSE API (lumo-bridge). Omitted → codex, so existing configs are unaffected.
   */
  runtime?: 'codex' | 'lumo';
  /**
   * Lumo Work backend settings — only used when `runtime === 'lumo'`. `baseUrl`
   * is the absolute http(s) origin of the Lumo server (validated with the same
   * loopback/SSRF policy as apiUrl when runtime is lumo). The timeouts bound a
   * single streamed turn: `idleTimeoutMs` resets on every received byte
   * (default 30000), `maxTurnMs` is the absolute ceiling (default 900000).
   */
  lumo?: {
    baseUrl: string;
    idleTimeoutMs?: number;
    maxTurnMs?: number;
  };
}

/**
 * One bot's entry. In the two-layer model the global config's `bots` array
 * lists which bots to run (by `id`); each bot's real settings — including its
 * required `botToken` — live in `<baseDir>/<id>/config.json`, which is merged
 * OVER both the global shared fields and any inline fields here (per-dir wins).
 *
 * Per-bot directories are NOT configurable here: they are always derived as
 * `<baseDir>/<id>/{data,workspace,memory}` so a bot cannot escape its subtree.
 */
export interface BotOverride {
  /**
   * Stable id — also the bot's subtree name under `baseDir`. Required in the
   * two-layer model (it selects `<baseDir>/<id>/config.json`). Must be a
   * conservative slug: letters, digits, dot, underscore, hyphen — no path
   * separators (it becomes a path segment).
   */
  id?: string;
  /**
   * Optional here — normally provided by the per-bot `<id>/config.json`. If set
   * inline it is used unless the per-bot file overrides it.
   */
  botToken?: string;
  apiUrl?: string;
  model?: string;
  systemPrompt?: string;
  botBlocklist?: string[];
  allowedBotUids?: string[];
  mentionFreeGroups?: string[];
  /** #86 follow-up: extra inbound-media hosts (see Config.mediaAllowedHosts). */
  mediaAllowedHosts?: string[];
  runtime?: 'codex' | 'lumo';
  lumo?: Config['lumo'];
}

type PartialConfig = {
  botToken?: string;
  apiUrl?: string;
  groupConfigDir?: string;
  sdk?: Partial<Config['sdk']>;
  rateLimit?: Partial<Config['rateLimit']>;
  context?: Partial<Config['context']>;
  maxResponseChars?: number;
  dispatchTimeoutMs?: number;
  botBlocklist?: string[];
  allowedBotUids?: string[];
  mentionFreeGroups?: string[];
  bots?: BotOverride[];
  workbench?: Config['workbench'];
  publicBaseUrl?: string;
  deliveryCard?: Config['deliveryCard'];
  mediaAllowedHosts?: string[];
  runtime?: 'codex' | 'lumo';
  lumo?: Config['lumo'];
};

function defaults(): Config {
  return {
    botToken: '',
    apiUrl: '',
    // baseDir is set by loadConfig() from the config path's directory; the
    // per-bot dirs below are DERIVED in resolveBotConfigs() as
    // <baseDir>/<botId>/{workspace,data,memory}. Left empty here.
    baseDir: '',
    cwdBase: '',
    cwd: '',
    dataDir: '',
    memoryBase: '',
    sdk: {
      // IM input is untrusted → default to the safe sandbox. Operators opt into
      // write access deliberately (allowWorkspaceWrite + sandboxMode).
      sandboxMode: 'read-only',
      allowWorkspaceWrite: false,
      approvalPolicy: 'never',
      modelReasoningEffort: 'medium',
      networkAccessEnabled: false,
      webSearchEnabled: false,
      webSearchMode: 'disabled',
    },
    rateLimit: {
      maxPerMinute: 5,
    },
    context: {
      maxContextChars: 6000,
      historyLimit: 40,
    },
    maxResponseChars: 524_288, // 512 KB (Q32)
    dispatchTimeoutMs: 300_000, // 5 min (#141)
    // #24: workbench on by default, port 28090 (see Config.workbench).
    workbench: { enabled: true, listenPort: 28090 },
  };
}

function readConfigFile(configFilePath: string): PartialConfig {
  if (!existsSync(configFilePath)) {
    return {};
  }

  // Q12: Warn if config file is readable by group/others (contains botToken).
  try {
    const stat = statSync(configFilePath);
    const mode = stat.mode & 0o777;
    if (mode & 0o077) {
      console.warn(
        `[codex-channel-octo] WARNING: ${configFilePath} has mode ${mode.toString(8)} — ` +
        `secrets may be exposed to other users. Fix with: chmod 600 ${configFilePath}`,
      );
    }
  } catch {
    // Best-effort check — don't block startup if stat fails.
  }

  const raw = readFileSync(configFilePath, 'utf-8');
  let parsed: Record<string, unknown> & PartialConfig;
  try {
    parsed = JSON.parse(raw) as Record<string, unknown> & PartialConfig;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`Failed to parse config file ${configFilePath}: ${msg}`);
  }
  // Strip top-level keys starting with "_" (e.g. _comment).
  const cleaned: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(parsed)) {
    if (!k.startsWith('_')) cleaned[k] = v;
  }
  return cleaned as PartialConfig;
}

function mergeConfig(base: Config, override: PartialConfig): Config {
  return {
    botToken: override.botToken ?? base.botToken,
    apiUrl: override.apiUrl ?? base.apiUrl,
    // baseDir + derived dirs are filled by loadConfig()/resolveBotConfigs(),
    // not by config-file merge.
    baseDir: base.baseDir,
    cwdBase: base.cwdBase,
    cwd: base.cwd,
    dataDir: base.dataDir,
    memoryBase: base.memoryBase,
    groupConfigDir: override.groupConfigDir ?? base.groupConfigDir,
    sdk: {
      ...base.sdk,
      ...(override.sdk ?? {}),
    },
    rateLimit: {
      ...base.rateLimit,
      ...(override.rateLimit ?? {}),
    },
    context: {
      ...base.context,
      ...(override.context ?? {}),
    },
    maxResponseChars: override.maxResponseChars ?? base.maxResponseChars,
    dispatchTimeoutMs: override.dispatchTimeoutMs ?? base.dispatchTimeoutMs,
    botBlocklist: override.botBlocklist ?? base.botBlocklist,
    allowedBotUids: override.allowedBotUids ?? base.allowedBotUids,
    mentionFreeGroups: override.mentionFreeGroups ?? base.mentionFreeGroups,
    bots: override.bots ?? base.bots,
    workbench: override.workbench ?? base.workbench,
    publicBaseUrl: override.publicBaseUrl ?? base.publicBaseUrl,
    deliveryCard: override.deliveryCard ?? base.deliveryCard,
    // #86 follow-up: operator-declared inbound-media hosts.
    mediaAllowedHosts: override.mediaAllowedHosts ?? base.mediaAllowedHosts,
    runtime: override.runtime ?? base.runtime,
    lumo: override.lumo ?? base.lumo,
  };
}

/**
 * SSRF protection for apiUrl: implemented in url-policy.ts (isAllowedApiUrl).
 * S6 fix: now rejects https://127.0.0.1 too — https doesn't make a private
 * address safe (could be a self-signed mitmproxy).
 */

export function loadConfig(configPath?: string): Config {
  const path = configPath ?? DEFAULT_CONFIG_PATH;
  // Migration aid: if the fixed global config is missing but a legacy
  // ./config.json exists in the cwd, point the operator at the move rather than
  // failing later with a cryptic "Missing required config: apiUrl".
  if (configPath === undefined && !existsSync(path) && existsSync('./config.json')) {
    throw new Error(
      `No config at ${path}, but ./config.json exists. The config location moved: ` +
      `codex-channel-octo now loads ~/.codex-channel-octo/config.json (shared, no token) plus ` +
      `~/.codex-channel-octo/<botId>/config.json (per-bot token). Move your settings there ` +
      `(see config.example.json / config.bot.example.json).`,
    );
  }
  const fileCfg = readConfigFile(path);
  // Config comes ONLY from config.json (global + per-bot layers) — there is no
  // environment-variable override path. The Codex upstream routing
  // (sdk.codexBaseUrl / sdk.codexApiKey) is set in config.json and forwarded to
  // the Codex subprocess by agent-bridge.
  const final = mergeConfig(defaults(), fileCfg);

  // baseDir = the directory containing the global config.json. Every bot's
  // subtree lives at <baseDir>/<botId>/…. resolveBotConfigs() derives the
  // per-bot dirs from this.
  final.baseDir = dirname(resolvePath(path));

  // apiUrl is shared and required at the global layer (a per-bot config.json may
  // still override it, re-checked per bot in resolveBotConfigs). botToken is NOT
  // validated here — it lives in each bot's <id>/config.json.
  if (!final.apiUrl) {
    throw new Error('Missing required config: apiUrl (set CODEX_OCTO_API_URL or config.json)');
  }
  if (!isAllowedApiUrl(final.apiUrl)) {
    throw new Error(
      `Unsafe apiUrl: ${final.apiUrl} — must be https:// or http://localhost/http://127.0.0.1 (SSRF protection)`,
    );
  }
  // The Codex upstream endpoint receives the API key and all prompt / response
  // content, so it gets the same SSRF policy as apiUrl.
  if (final.sdk.codexBaseUrl && !isAllowedApiUrl(final.sdk.codexBaseUrl)) {
    throw new Error(
      `Unsafe sdk.codexBaseUrl: ${final.sdk.codexBaseUrl} — must be https:// ` +
      `or http://localhost/http://127.0.0.1 (SSRF protection)`,
    );
  }
  // danger-full-access lets the model touch any file under $HOME (~/.ssh,
  // keychain). IM input is untrusted — never allow it.
  if (final.sdk.sandboxMode === 'danger-full-access') {
    throw new Error(
      `Unsafe sdk.sandboxMode: 'danger-full-access' is not allowed (untrusted IM input). ` +
      `Use 'read-only' (default) or 'workspace-write' with allowWorkspaceWrite:true.`,
    );
  }
  // additionalDirectories extend the writable sandbox — validate the global
  // layer here. Per-bot overrides are re-validated in resolveBotConfigs().
  assertAdditionalDirectories('config', final.sdk.additionalDirectories, [
    { label: 'the config/SOUL/data tree (baseDir)', path: final.baseDir },
    { label: 'groupConfigDir (trusted group instructions)', path: final.groupConfigDir },
  ]);

  // #32: publicBaseUrl is embedded into delivery-card Action.OpenUrl targets;
  // the card validator only accepts absolute http/https URLs, so fail fast at
  // load instead of sending cards the server rejects.
  assertPublicBaseUrl(final.publicBaseUrl);

  // #86 follow-up: normalize + validate the operator-declared media hosts once
  // at load so a malformed entry fails at boot instead of silently dropping
  // every inbound image.
  final.mediaAllowedHosts = normalizeMediaAllowedHosts(final.mediaAllowedHosts);

  return final;
}

/**
 * Validate `mediaAllowedHosts` (Config.mediaAllowedHosts): an optional list of
 * bare hosts (no scheme, no path, no wildcard) that may serve inbound media.
 *
 * Rejects anything carrying a scheme, slash, or whitespace so an entry can
 * never widen the allow list beyond a single host (e.g. `evil.com/../` or
 * `*@evil.com`). Returns lowercased entries, or `undefined` when the list is
 * absent/empty.
 */
export function normalizeMediaAllowedHosts(hosts: string[] | undefined): string[] | undefined {
  if (hosts === undefined) return undefined;
  if (!Array.isArray(hosts)) {
    throw new Error('Invalid mediaAllowedHosts: must be an array of host names (e.g. ["cdn.example.com"])');
  }
  const out: string[] = [];
  for (const entry of hosts) {
    const raw = typeof entry === 'string' ? entry.trim() : '';
    if (raw === '') continue;
    if (/[\s/\\]/.test(raw) || raw.includes('://') || raw.startsWith('*')) {
      throw new Error(
        `Invalid mediaAllowedHosts entry: ${JSON.stringify(entry)} — expected a bare host ` +
        `(e.g. "cdn.example.com" or "127.0.0.1:9000"), with no scheme, path, or wildcard`,
      );
    }
    const host = raw.toLowerCase();
    if (!out.includes(host)) out.push(host);
  }
  return out.length > 0 ? out : undefined;
}

/**
 * Validate `publicBaseUrl` (#32): when present it must be an absolute http(s)
 * URL. A relative or foreign-scheme value would produce delivery-card links
 * the server rejects (or worse, a javascript: surface), so reject at boot.
 */
function assertPublicBaseUrl(publicBaseUrl: string | undefined): void {
  if (publicBaseUrl === undefined) return;
  let ok = false;
  try {
    const u = new URL(publicBaseUrl);
    ok = u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    ok = false;
  }
  if (!ok) {
    throw new Error(
      `Invalid publicBaseUrl: ${publicBaseUrl} — must be an absolute http:// or https:// URL`,
    );
  }
}

/**
 * Enforce that `groupConfigDir` (whose files are injected UNSANITIZED into the
 * system prompt) is not the same as, nor nested under, the agent-writable
 * `cwdBase`. Otherwise a user-driven agent could write its own future
 * system-prompt instructions.
 *
 * Uses realpathSync.native for paths that exist (so symlinks can't dodge the
 * boundary) and falls back to lexical resolve() for not-yet-created dirs.
 */
function assertGroupConfigDirOutsideCwd(cfg: Config): void {
  if (!cfg.groupConfigDir) return;
  const cwdBase = cfg.cwdBase ?? cfg.cwd;
  const cwdBaseResolved = canonicalize(cwdBase);
  const groupDirResolved = canonicalize(cfg.groupConfigDir);
  if (groupDirResolved === cwdBaseResolved || isPathInside(groupDirResolved, cwdBaseResolved)) {
    throw new Error(
      `Unsafe groupConfigDir: ${cfg.groupConfigDir} is the same as or nested under ` +
      `cwdBase (${cwdBase}). It must be operator-controlled and outside the ` +
      `agent-writable sandbox, since its files are injected into the system prompt.`,
    );
  }
}

/** Resolve to a real path when it exists (defeats symlink dodges), else lexical. */

function canonicalize(p: string): string {
  try {
    return realpathSync.native(p);
  } catch {
    return resolvePath(p);
  }
}

/** True when `child` is strictly inside `parent` (both already resolved). */
function isPathInside(child: string, parent: string): boolean {
  const parentWithSep = parent.endsWith(sep) ? parent : parent + sep;
  return child.startsWith(parentWithSep);
}

/**
 * v0.3 multi-bot: expand a loaded Config into one concrete Config per bot.
 *
 * - Single-bot (no `bots`): returns `[config]` with `botId` defaulted to
 *   `default`, unchanged otherwise — fully backward compatible.
 * - Multi-bot: returns one Config per `bots[]` entry. Each inherits the base
 *   config and applies its overrides. To guarantee bots never share history,
 *   cwd, or lock files, each bot's `dataDir` and `cwdBase` are namespaced by its
 *   id UNLESS the entry sets them explicitly.
 *
 * Throws on missing/duplicate bot tokens or duplicate ids (fail fast at boot).
 */
/**
 * Expand a loaded GLOBAL config into one concrete Config per bot.
 *
 * Two-layer, bot-first model:
 * - Single-bot (no `bots`): one bot with id `default`. Its token/overrides come
 *   from the global config and/or `<baseDir>/default/config.json`.
 * - Multi-bot: one Config per `bots[]` entry (selected by `id`). For each, the
 *   effective config is: global shared fields ⊕ inline `bots[]` fields ⊕
 *   `<baseDir>/<id>/config.json` (per-dir file wins).
 *
 * Every bot's directories are DERIVED (never configurable):
 *   data      = <baseDir>/<id>/data
 *   workspace = <baseDir>/<id>/workspace   (cwdBase)
 *   memory    = <baseDir>/<id>/memory
 * and its personality from `<baseDir>/<id>/SOUL.md` (overrides systemPrompt).
 *
 * Throws on missing/duplicate tokens, duplicate ids, invalid id slugs, or unsafe
 * apiUrl (fail fast at boot).
 */
export function resolveBotConfigs(config: Config): Config[] {
  // Zero-bot idle: no bots[] list and no global botToken — and no token in the
  // default per-bot file either. Return [] so the gateway can run idle (online,
  // no bots) until the first bot is provisioned, instead of throwing. A legacy
  // single bot may keep its token only in <baseDir>/default/config.json (read by
  // the synthesized "default" entry below), so check that before idling.
  const hasInlineBots = !!(config.bots && config.bots.length > 0);
  if (!hasInlineBots && !config.botToken) {
    const defaultPerBot = readConfigFile(pathJoin(config.baseDir, 'default', 'config.json'));
    if (!defaultPerBot.botToken) {
      return [];
    }
  }
  // Single-bot: synthesize one entry with id "default".
  const entries: BotOverride[] =
    config.bots && config.bots.length > 0
      ? config.bots
      : [{ id: 'default', botToken: config.botToken || undefined }];

  const seenIds = new Set<string>();
  const seenTokens = new Set<string>();
  const resolvedBots = entries.map((bot, i) => {
    const id = bot.id ?? `bot${i}`;
    // The id becomes a path segment for the bot's subtree, so restrict it to a
    // conservative slug — otherwise ids like "../ops" or "a/b" could escape or
    // alias the intended directory, defeating isolation.
    if (!/^[a-zA-Z0-9._-]+$/.test(id) || id === '.' || id === '..') {
      throw new Error(
        `Bot "${id}": invalid id — use only letters, digits, dot, underscore, hyphen (no path separators)`,
      );
    }
    if (seenIds.has(id)) {
      throw new Error(`Duplicate bot id "${id}" — ids must be unique`);
    }
    seenIds.add(id);

    // Derive the bot's self-contained subtree under baseDir.
    const botRoot = pathJoin(config.baseDir, id);
    const botDataDir = pathJoin(botRoot, 'data');
    const botCwdBase = pathJoin(botRoot, 'workspace');
    const botMemoryBase = pathJoin(botRoot, 'memory');
    // Per-bot CODEX_HOME so each bot's codex auth/config/sessions/memory are
    // isolated and IM content never lands in the operator's personal ~/.codex.
    // Operator can opt into sharing by setting sdk.codexHome to ~/.codex.
    const botCodexHome = pathJoin(botRoot, 'codex-home');

    // Per-bot config.json (in the bot's own subtree) is the highest-priority
    // layer: global shared ⊕ inline bots[] entry ⊕ <baseDir>/<id>/config.json.
    const perBotFile = readConfigFile(pathJoin(botRoot, 'config.json'));
    const botToken = perBotFile.botToken ?? bot.botToken ?? '';
    if (!botToken) {
      throw new Error(
        `Bot "${id}": missing botToken — set it in ${pathJoin(botRoot, 'config.json')}`,
      );
    }
    if (seenTokens.has(botToken)) {
      throw new Error(`Duplicate botToken across bots — each bot needs a distinct token`);
    }
    seenTokens.add(botToken);

    // openclaw-style SOUL.md in the bot's subtree overrides systemPrompt (which
    // may come from the per-bot file, the inline entry, or the shared config).
    const botSoul = loadSoul(botRoot);
    const sharedSystemPrompt = config.sdk.systemPrompt;
    const botSystemPrompt =
      botSoul ?? perBotFile.sdk?.systemPrompt ?? bot.systemPrompt ?? sharedSystemPrompt;

    const apiUrl = perBotFile.apiUrl ?? bot.apiUrl ?? config.apiUrl;
    const model = perBotFile.sdk?.model ?? bot.model ?? config.sdk.model;

    const resolved: Config = {
      ...config,
      bots: undefined, // a per-bot config is single-bot
      botId: id,
      botToken,
      apiUrl,
      baseDir: config.baseDir,
      dataDir: botDataDir,
      cwdBase: botCwdBase,
      cwd: botCwdBase,
      memoryBase: botMemoryBase,
      botBlocklist: perBotFile.botBlocklist ?? bot.botBlocklist ?? config.botBlocklist,
      allowedBotUids: perBotFile.allowedBotUids ?? bot.allowedBotUids ?? config.allowedBotUids,
      mentionFreeGroups:
        perBotFile.mentionFreeGroups ?? bot.mentionFreeGroups ?? config.mentionFreeGroups,
      groupConfigDir: perBotFile.groupConfigDir ?? config.groupConfigDir,
      // #32: delivery-card config — per-bot file may override the global layer.
      publicBaseUrl: perBotFile.publicBaseUrl ?? config.publicBaseUrl,
      deliveryCard: perBotFile.deliveryCard ?? config.deliveryCard,
      // #86 follow-up: inbound-media hosts — per-bot file wins over the inline
      // entry, which wins over the shared global layer.
      mediaAllowedHosts:
        perBotFile.mediaAllowedHosts ?? bot.mediaAllowedHosts ?? config.mediaAllowedHosts,
      // Runtime backend + Lumo settings — per-bot file wins over the inline
      // entry, which wins over the shared global layer.
      runtime: perBotFile.runtime ?? bot.runtime ?? config.runtime,
      lumo: perBotFile.lumo ?? bot.lumo ?? config.lumo,
      sdk: {
        ...config.sdk,
        ...(perBotFile.sdk ?? {}),
        ...(model !== undefined ? { model } : {}),
        ...(botSystemPrompt !== undefined ? { systemPrompt: botSystemPrompt } : {}),
        // CODEX_HOME defaults to the bot's own subtree; an explicit per-bot
        // sdk.codexHome (e.g. ~/.codex for opt-in sharing) wins. Expand a leading
        // ~ — env values are NOT shell-expanded, so a literal "~/.codex" would
        // create a bogus dir named "~" instead of sharing the personal home.
        codexHome: expandHome(perBotFile.sdk?.codexHome ?? config.sdk.codexHome) ?? botCodexHome,
      },
    };
    if (!isAllowedApiUrl(resolved.apiUrl)) {
      throw new Error(`Bot "${id}": unsafe apiUrl ${resolved.apiUrl} (SSRF protection)`);
    }
    // Lumo runtime: only when the EFFECTIVE runtime is 'lumo' do we require and
    // validate lumo.baseUrl. The codex path (default) is left byte-for-byte
    // unchanged — an unset/invalid lumo block is inert unless runtime selects it.
    if (resolved.runtime === 'lumo') {
      const baseUrl = resolved.lumo?.baseUrl;
      if (!baseUrl) {
        throw new Error(
          `Bot "${id}": runtime 'lumo' requires lumo.baseUrl (set it in ${pathJoin(botRoot, 'config.json')})`,
        );
      }
      // Same loopback/SSRF policy as apiUrl: absolute http(s), no private host
      // (isAllowedApiUrl rejects relative/foreign schemes and private IPs).
      if (!isAllowedApiUrl(baseUrl)) {
        throw new Error(
          `Bot "${id}": unsafe lumo.baseUrl ${baseUrl} — must be https:// or ` +
          `http://localhost/http://127.0.0.1 (SSRF protection)`,
        );
      }
    }
    // #32: a per-bot publicBaseUrl override bypasses loadConfig's check — re-validate.
    try {
      assertPublicBaseUrl(resolved.publicBaseUrl);
    } catch (err) {
      throw new Error(`Bot "${id}": ${err instanceof Error ? err.message : String(err)}`);
    }
    // #86 follow-up: a per-bot mediaAllowedHosts override bypasses loadConfig's
    // normalization — re-validate it here so a malformed entry fails at boot
    // instead of silently dropping every inbound image.
    try {
      resolved.mediaAllowedHosts = normalizeMediaAllowedHosts(resolved.mediaAllowedHosts);
    } catch (err) {
      throw new Error(`Bot "${id}": ${err instanceof Error ? err.message : String(err)}`);
    }
    // per-bot sdk can override codexBaseUrl/sandboxMode — re-validate here so a
    // bad per-bot value fails at boot, not silently (loadConfig only saw the
    // global layer).
    if (resolved.sdk.codexBaseUrl && !isAllowedApiUrl(resolved.sdk.codexBaseUrl)) {
      throw new Error(`Bot "${id}": unsafe sdk.codexBaseUrl ${resolved.sdk.codexBaseUrl} (SSRF protection)`);
    }
    if (resolved.sdk.sandboxMode === 'danger-full-access') {
      throw new Error(`Bot "${id}": sdk.sandboxMode 'danger-full-access' is not allowed (untrusted IM input)`);
    }
    // per-bot sdk can also override additionalDirectories — re-validate so a
    // per-bot relative / '~' entry can't slip past the global-only check, and
    // reject any entry that overlaps this bot's trust anchors: its writable cwd
    // (AGENTS.md), the config/SOUL tree, the group-config dir, its memory dir
    // (re-injected into future turns), and codexHome (auth.json / config.toml —
    // credentials; may be relocated outside baseDir, e.g. a shared ~/.codex).
    assertAdditionalDirectories(id, resolved.sdk.additionalDirectories, [
      { label: "the bot's writable cwd", path: resolved.cwdBase ?? resolved.cwd },
      { label: 'the config/SOUL/data tree (baseDir)', path: resolved.baseDir },
      { label: 'groupConfigDir (trusted group instructions)', path: resolved.groupConfigDir },
      { label: 'the bot memory directory', path: resolved.memoryBase },
      { label: 'codexHome (agent credentials / config.toml)', path: resolved.sdk.codexHome },
    ]);
    // Fail-fast on misspelled enums so a typo surfaces at boot, not on every
    // message (where buildThreadOptions would throw inside the handler and the
    // bot would look online but fail every turn).
    assertSdkEnums(id, resolved.sdk);
    // GROUP.md trust boundary: groupConfigDir must not be the bot's writable cwd.
    assertGroupConfigDirOutsideCwd(resolved);
    return resolved;
  });

  return resolvedBots;
}

/**
 * Expand a leading `~` or `~/` to the user's home directory. Env var values are
 * not shell-expanded, so a config like `codexHome: "~/.codex"` must be resolved
 * here or it would create a literal directory named "~". Returns undefined for
 * undefined input (caller falls back to the derived default).
 */
export function expandHome(p: string | undefined): string | undefined {
  if (p === undefined) return undefined;
  if (p === '~') return homedir();
  if (p.startsWith('~/')) return pathJoin(homedir(), p.slice(2));
  return p;
}

/**
 * Validate `sdk.additionalDirectories`: every entry must be an explicit
 * absolute path. Relative / '~' entries expand per-runtime and could resolve
 * outside the intended sandbox boundary, so reject them rather than guess.
 * Called on both the global merged config (loadConfig) and each per-bot config
 * (resolveBotConfigs) — a per-bot override must not slip past the global check.
 *
 * `trustAnchors` are directories that establish the agent's trusted inputs: the
 * session cwd (where AGENTS.md — the frozen security prefix + SOUL/GROUP text —
 * is written), the operator config/SOUL/data tree, and the group-instructions
 * dir (injected UNSANITIZED as trusted). The sandboxed agent is driven by
 * untrusted IM input, so a writable root that overlaps any anchor would let the
 * agent overwrite the very files that govern its own trust boundary — a
 * trust-escape, the same class of risk the code already hard-rejects for
 * `danger-full-access`. Reject overlap in EITHER direction (entry contains an
 * anchor, or sits inside one).
 */
function assertAdditionalDirectories(
  id: string,
  dirs: string[] | undefined,
  trustAnchors: Array<{ label: string; path: string | undefined }> = [],
): void {
  // additionalDirectories comes from untyped JSON. A bare string would iterate
  // char-by-char (misleading "unsafe entry 'f'") and a non-iterable would throw
  // a raw TypeError below — fail closed with an actionable message instead.
  if (dirs !== undefined && !Array.isArray(dirs)) {
    throw new Error(
      `Bot "${id}": sdk.additionalDirectories must be an array of absolute paths (got ${typeof dirs}).`,
    );
  }
  for (const dir of dirs ?? []) {
    if (typeof dir !== 'string' || !isAbsolute(dir) || dir.startsWith('~')) {
      throw new Error(
        `Bot "${id}": unsafe sdk.additionalDirectories entry ${JSON.stringify(dir)} — must be an absolute path (no '~' or relative).`,
      );
    }
    // A '..' segment resolves per-runtime to a directory the literal path does
    // not name (e.g. "/srv/bus/../../etc"), sidestepping the overlap checks
    // below. Reject rather than silently normalize, so the granted root is
    // exactly what the operator wrote. Split on BOTH separators: on Windows a
    // slash-separated entry would otherwise slip past a sep-only split.
    if (dir.split(/[\\/]/).includes('..')) {
      throw new Error(
        `Bot "${id}": unsafe sdk.additionalDirectories entry ${JSON.stringify(dir)} — must not contain '..' path segments.`,
      );
    }
    const entry = canonicalize(dir);
    for (const anchor of trustAnchors) {
      if (!anchor.path) continue;
      const a = canonicalize(anchor.path);
      if (entry === a || isPathInside(entry, a) || isPathInside(a, entry)) {
        throw new Error(
          `Bot "${id}": unsafe sdk.additionalDirectories entry ${JSON.stringify(dir)} — ` +
          `overlaps ${anchor.label} (${anchor.path}). An agent-writable root must not contain, ` +
          `equal, or sit inside a trusted instruction/config location, or untrusted IM input could ` +
          `drive the agent to overwrite its own trust anchors.`,
        );
      }
    }
  }
}

/**
 * Validate the codex sdk enum fields at boot so a misspelled value fails fast
 * (instead of throwing inside the per-message handler). Mirrors the allowed
 * sets in agent-bridge; kept here (no import) to avoid a config↔bridge cycle.
 */
function assertSdkEnums(id: string, sdk: Config['sdk']): void {
  const check = (field: string, value: string | undefined, allowed: string[]): void => {
    if (value !== undefined && !allowed.includes(value)) {
      throw new Error(`Bot "${id}": invalid sdk.${field} '${value}' (allowed: ${allowed.join(', ')})`);
    }
  };
  check('sandboxMode', sdk.sandboxMode, ['read-only', 'workspace-write', 'danger-full-access']);
  check('approvalPolicy', sdk.approvalPolicy, ['never', 'on-request', 'on-failure', 'untrusted']);
  check('modelReasoningEffort', sdk.modelReasoningEffort, ['minimal', 'low', 'medium', 'high', 'xhigh']);
  check('webSearchMode', sdk.webSearchMode, ['disabled', 'cached', 'live']);
}

/**
 * v1.1: openclaw-style per-bot personality. Read `<botRoot>/SOUL.md` if it
 * exists and return its trimmed contents as the bot's "soul" (voice/stance/
 * boundaries), to be composed into the agent system prompt. Mirrors openclaw's
 * SOUL.md: a file you edit, not a config string. When the file is absent or
 * empty, returns undefined so the caller falls back to the `systemPrompt`
 * config string. Best-effort — a read error never blocks startup.
 */
export function loadSoul(botRoot: string): string | undefined {
  const path = pathJoin(botRoot, 'SOUL.md');
  if (!existsSync(path)) return undefined;
  try {
    const content = readFileSync(path, 'utf-8').trim();
    return content.length > 0 ? content : undefined;
  } catch (err) {
    console.warn(
      `[codex-channel-octo] WARNING: failed to read ${path}: ${err instanceof Error ? err.message : String(err)}`,
    );
    return undefined;
  }
}
