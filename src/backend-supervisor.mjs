// Runtime-agnostic in-process supervisor for the sutando BACKEND services.
//
// This generalizes `bash src/startup.sh`. startup.sh is a dev-machine boot
// script: it hard-exits without GEMINI_API_KEY, reads .env from the repo root
// (read-only in a packaged build, not the writable workspace), opens a browser
// tab, compiles + runs a native menubar app, and `exec`s a tmux CLI. None of
// that belongs in a release build embedded by a desktop host.
//
// Instead we boot only the curated backend services the UI consumes, each as
// its own child process, with per-service gates/onReady, port-ownership
// management, readiness waits, and a supervise/restart loop:
//
//   credential-proxy :7846  Claude quota tracking; on ready sets
//                           ANTHROPIC_BASE_URL for the other services.
//   collector        :4000  local observability/metering ingest (auth-gated).
//   web-client       :8080  conversation HTTP API + SSE  (the ?api= origin)
//   voice-agent      :9900  Gemini Live WebSocket        (the ?ws= origin)
//   agent-api        :7843  python task API              (the ?agent-api= origin)
//   dashboard        :7844  python status dashboard      (the ?dashboard= origin)
//   screen-capture   :7845  python screenshot server     (vision / "describe my screen")
//   conversation-server :3100  Twilio Media Streams + Gemini Live (opt-in on Twilio creds)
//
// Plus the optional channel bridges (telegram / discord / slack) as port-less
// `daemon` services — gated on their channel token + python deps, adopted if
// already running, supervised with restart but no port probe. Opt out with
// SUTANDO_NO_BRIDGES=1.
//
// Central fix vs startup.sh: sutando services resolve .env relative to cwd (the
// read-only code checkout), NOT the writable workspace. voice-agent.ts does
// `import 'dotenv/config'`; web-client.ts and agent-api.py read process.env
// directly. So we read the workspace .env ourselves and pass a BUILT env object
// to each child. dotenv never overrides already-set process.env keys, so our
// injected values (e.g. a managed GEMINI_API_KEY) win.
//
// ----------------------------------------------------------------------------
// Host interface — the SAME supervisor embeds in any runtime (an Electron main
// process, a Tauri Node sidecar, a bare Node CLI). The host supplies a thin
// adapter object `host` with these fields/methods; the supervisor contains NO
// runtime-specific imports (no `require('electron')`).
//
//   host.packaged            boolean
//                            True when running from a packaged/bundled build.
//                            Governs strict port/tmux ownership + runtime-bin
//                            derivation. (was Electron `app.isPackaged`)
//
//   host.resourcesPath       string | null | undefined
//                            Absolute path to the bundle's resources dir. When
//                            packaged, RUNTIME_BIN = <resourcesPath>/runtime/bin
//                            (bundled node/python3/tmux). Null/absent when
//                            unpackaged. (was Electron `process.resourcesPath`)
//
//   host.screenAccessStatus()  -> string
//                            macOS Screen Recording authorization status, one of
//                            'granted' | 'denied' | 'not-determined' |
//                            'restricted'. Only consulted on darwin; may throw
//                            (treated as non-blocking). (was Electron
//                            systemPreferences.getMediaAccessStatus('screen'))
//
//   host.backendDir()        -> string
//                            Absolute path to the code checkout / bundled repo
//                            root. Used as child cwd and to resolve service
//                            entries + bundled scripts.
//
//   host.resolveWorkspace()  -> string
//                            Absolute path to the writable workspace (logs/,
//                            results/, state/, .env, .claude-sutando, …).
//
//   host.claudeConfigDir()   -> string
//                            Absolute path to the per-workspace Claude config
//                            home (CLAUDE_CONFIG_DIR, e.g.
//                            <workspace>/.claude-sutando).
//
//   host.envFilePath()       -> string
//                            Absolute path to the workspace .env (Settings
//                            writes the user's keys here).
//
//   host.readCloudAuth()     -> { signedIn: boolean, token?: string, apiBase?: string }
//                            Current cloud session state. Drives the collector
//                            gate + optional metering/observability env. MAY
//                            THROW when not signed in — every caller guards it.
//
//   host.resolveBinary(name) -> string | null
//                            Resolve an executable by name across the host's
//                            standard search dirs, or null if not found.
//
//   host.parseEnv(text)      -> Record<string,string>
//                            Parse .env file contents into a plain object
//                            (dotenv semantics). Kept host-provided so the
//                            workspace-env store is parsed identically to the
//                            rest of the app.
//
//   host.safeRead(path)      -> string
//                            Read a file's text, returning '' on any error.
//
// Emitted events (host may subscribe via onEvent) are generic and stable:
//   service-up | service-down | service-error | service-skipped |
//   service-retry | service-replacing
// ----------------------------------------------------------------------------

import path from 'node:path';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import util from 'node:util';
import { spawn, spawnSync } from 'node:child_process';

// ----------------------------------------------------------------------------
// Service registry — single source of truth for start order, env, readiness.
// ----------------------------------------------------------------------------

const PROXY_PORT = 7846;
// Observability collector — the single local ingestion point all metering flows
// through (voice/phone realtime, Claude Code hooks + OTel). Auth-gated: it runs
// only while signed in, so usage capture + upstream forwarding follow the cloud
// session — sign-in starts it, sign-out stops it (see relaunchCollector() and the
// gate on its service def). SUTANDO_OBS_ENDPOINT (set in buildBaseEnv) tells the
// realtime clients where to POST; this port must match it.
const OBS_PORT = 4000;

export const SERVICES = [
  {
    name: 'credential-proxy',
    port: PROXY_PORT,
    kind: 'node',
    entry: 'skills/quota-tracker/scripts/credential-proxy.ts',
    optional: true, // failure is non-fatal — Claude just connects directly
    // Once the proxy is listening, route the other services' Claude calls
    // through it (read at child process start, so it must be set first).
    onReady: (baseEnv) => {
      baseEnv.ANTHROPIC_BASE_URL = `http://localhost:${PROXY_PORT}`;
    },
  },
  // Booted before voice-agent/web-client so its /ingest endpoint is listening
  // when the realtime clients fire. Optional: metering is best-effort, so a
  // failed collector never blocks boot (the realtime client just drops).
  // Auth-gated: skipped (not failed) while signed out, so a logged-out app runs
  // no collector at all; relaunchCollector() re-evaluates this on sign-in/out.
  {
    name: 'collector',
    port: OBS_PORT,
    kind: 'node',
    entry: 'src/observability/boot.ts',
    optional: true,
    gate: (host) => {
      try {
        return host.readCloudAuth().signedIn
          ? { ok: true }
          : { ok: false, reason: 'signed out — metering paused' };
      } catch {
        return { ok: false, reason: 'signed out — metering paused' };
      }
    },
  },
  { name: 'web-client', port: 8080, kind: 'node', entry: 'src/web-client.ts' },
  { name: 'voice-agent', port: 9900, kind: 'node', entry: 'src/voice-agent.ts', requires: ['GEMINI_API_KEY'] },
  { name: 'agent-api', port: 7843, kind: 'python', entry: 'src/agent-api.py' },
  // Status dashboard embedded by the Dashboard pane (DashboardPage.tsx →
  // ?dashboard= origin). The supervisor must boot it or the pane shows
  // "Service not reachable". Stdlib http.server, optional like the others.
  { name: 'dashboard', port: 7844, kind: 'python', entry: 'src/dashboard.py', optional: true },
  // Screenshot server for vision tools (screenshot-explain, "describe my screen",
  // feedback screen-attach). Spawned NON-detached (noDetach) so it stays a direct,
  // non-disclaimed descendant of the host process — macOS then attributes the
  // screen-recording responsibility to the host app, so one Screen Recording
  // grant covers it (a launchd/disclaimed spawn would have broken this
  // responsible-process roll-up). Gated on the grant: starting without it yields
  // black-PNG denials + a stale :7845 (mirrors startup.sh's PERM_OK skip).
  {
    name: 'screen-capture',
    port: 7845,
    kind: 'python',
    entry: 'src/screen-capture-server.py',
    optional: true,
    noDetach: true,
    gate: (host) => {
      try {
        if (process.platform !== 'darwin') return { ok: true };
        const st = host.screenAccessStatus();
        return st === 'granted' ? { ok: true } : { ok: false, reason: 'Screen Recording not granted' };
      } catch {
        return { ok: true }; // never block boot on a probe failure
      }
    },
  },
  // Phone conversation server (Twilio Media Streams + Gemini Live, :3100).
  // Opt-in: only boots when Twilio creds are present (entered in Settings → API
  // keys, stored in the workspace .env). It self-manages its public tunnel
  // (ngrok via NGROK_AUTHTOKEN/NGROK_DOMAIN, or an explicit TWILIO_WEBHOOK_URL),
  // so the supervisor just launches it. Skipped (not failed) when Twilio isn't
  // configured; 'requires' surfaces a clear error if Twilio is set but the
  // Gemini key isn't.
  {
    name: 'conversation-server',
    port: 3100,
    kind: 'node',
    entry: 'skills/phone-conversation/scripts/conversation-server.ts',
    optional: true,
    requires: ['GEMINI_API_KEY'],
    gate: (_host, env) =>
      env && String(env.TWILIO_ACCOUNT_SID || '').trim()
        ? { ok: true }
        : { ok: false, reason: 'no Twilio credentials (Settings → API keys)' },
  },
  // Optional channel bridges. They're port-less (telegram polls outbound,
  // discord/slack are gateway/socket clients), so they run via the `daemon`
  // path: no waitForPort, readiness = survived DAEMON_GRACE_MS. Each is gated
  // on its channel token (<claude-home>/channels/<channel>/.env) and on a
  // python3 that actually imports `pyDeps` — the PATH python3 (often a pyenv
  // shim) is frequently missing discord.py / slack_bolt. A failed gate skips the
  // bridge (optional → non-fatal), it does not crash-loop. Opt out via
  // SUTANDO_NO_BRIDGES=1.
  { name: 'telegram-bridge', kind: 'python', entry: 'src/telegram-bridge.py', daemon: true, optional: true, channel: 'telegram', tokenKey: 'TELEGRAM_BOT_TOKEN', pyDeps: [] },
  { name: 'discord-bridge', kind: 'python', entry: 'src/discord-bridge.py', daemon: true, optional: true, channel: 'discord', tokenKey: 'DISCORD_BOT_TOKEN', pyDeps: ['discord'] },
  { name: 'slack-bridge', kind: 'python', entry: 'src/slack-bridge.py', daemon: true, optional: true, channel: 'slack', tokenKey: 'SLACK_BOT_TOKEN', pyDeps: ['slack_bolt'] },
];

const READY_TIMEOUT_MS = 8000;
const PROXY_READY_TIMEOUT_MS = 4000;
// Port-less daemons (the channel bridges) have no port to probe — treat them as
// up if they survive this grace without exiting. A bad interpreter / import
// error exits well within it and trips the normal backoff/restart path.
const DAEMON_GRACE_MS = 1500;
const STORM_WINDOW_MS = 60_000;
const STORM_MAX_RESTARTS = 5;
const BACKOFF_BASE_MS = 1000;
const BACKOFF_CAP_MS = 30_000;
const KILL_GRACE_MS = 3000;
const TAIL_LINES = 50;

const CRITICAL_SERVICE_NAMES = new Set([
  'credential-proxy',
  'web-client',
  'voice-agent',
  'agent-api',
]);

// ----------------------------------------------------------------------------
// Module state
// ----------------------------------------------------------------------------

let currentHost = null;
let started = false; // true while we are actively managing services
let PACKAGED = false;
let BACKEND_DIR = null;
let RUNTIME_BIN = null;
let NODE_BIN = 'node';
let PYTHON_BIN = 'python3';
let BRIDGE_VENV_PY = null; // workspace venv with slack_bolt/discord.py (see ensure-bridge-deps.sh)
let CLAUDE_CONFIG_DIR = null; // per-workspace Claude Code home (<workspace>/.claude-sutando) — see buildBaseEnv

/** name -> record */
const state = new Map();
const listeners = new Set();

function emit(evt) {
  for (const fn of listeners) {
    try {
      fn(evt);
    } catch {
      /* a bad listener must not break the supervisor */
    }
  }
}

// ----------------------------------------------------------------------------
// Paths / binaries
// ----------------------------------------------------------------------------

function computeDirs(host) {
  const packaged = !!(host && host.packaged);
  PACKAGED = packaged;
  BACKEND_DIR = host.backendDir();
  RUNTIME_BIN = packaged && host.resourcesPath ? path.join(host.resourcesPath, 'runtime', 'bin') : null;
  NODE_BIN = resolveExecutable('node', RUNTIME_BIN ? path.join(RUNTIME_BIN, 'node') : null) || 'node';
  PYTHON_BIN = host.resolveBinary('python3') || (fs.existsSync('/usr/bin/python3') ? '/usr/bin/python3' : 'python3');
  // Bridge deps (slack_bolt/discord.py) are installed into a workspace venv by
  // scripts/ensure-bridge-deps.sh; this is the interpreter that has them, and we
  // prefer it in the bridge-interpreter probe (pythonCandidates).
  BRIDGE_VENV_PY = path.join(host.resolveWorkspace(), 'runtime', 'bridge-venv', 'bin', 'python3');
  CLAUDE_CONFIG_DIR = host.claudeConfigDir();
}

function canonicalPath(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return path.resolve(p);
  }
}

function samePath(a, b) {
  return canonicalPath(a) === canonicalPath(b);
}

function strictRuntimeOwnership() {
  // A packaged build should own the fixed local ports and core tmux session.
  // Silently adopting a dev checkout creates split-brain: the UI writes to one
  // workspace while Core CLI watches another. Dev can restore adoption with
  // SUTANDO_ADOPT_FOREIGN_PORTS=1 when intentionally sharing a backend.
  return PACKAGED && process.env.SUTANDO_ADOPT_FOREIGN_PORTS !== '1';
}

function portOwnerPids(port) {
  try {
    return spawnSync('lsof', ['-ti', `TCP:${port}`, '-sTCP:LISTEN'], {
      encoding: 'utf8',
      timeout: 3000,
    }).stdout
      .split(/\s+/)
      .map((s) => Number(s))
      .filter((n) => Number.isFinite(n) && n > 0);
  } catch {
    return [];
  }
}

function processCwd(pid) {
  try {
    const out = spawnSync('lsof', ['-a', '-p', String(pid), '-d', 'cwd', '-Fn'], {
      encoding: 'utf8',
      timeout: 3000,
    }).stdout;
    const line = out.split('\n').find((s) => s.startsWith('n'));
    return line ? line.slice(1) : null;
  } catch {
    return null;
  }
}

function portOwners(port) {
  return portOwnerPids(port).map((pid) => ({ pid, cwd: processCwd(pid) }));
}

async function waitForPortFree(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await portInUse(port))) return true;
    await sleep(150);
  }
  return !(await portInUse(port));
}

async function replaceForeignPortOwner(def) {
  if (!strictRuntimeOwnership()) return { replaced: false };
  const owners = portOwners(def.port);
  if (!owners.length) return { replaced: false };
  const foreign = owners.filter((o) => !o.cwd || !samePath(o.cwd, BACKEND_DIR));
  if (!foreign.length) return { replaced: false };

  const detail = foreign
    .map((o) => `pid=${o.pid}${o.cwd ? ` cwd=${o.cwd}` : ''}`)
    .join(', ');
  console.warn(`[supervisor] ${def.name}: replacing foreign :${def.port} owner(s): ${detail}`);
  emit({
    type: 'service-replacing',
    svc: def.name,
    detail: `foreign :${def.port} owner(s): ${detail}`,
  });

  for (const o of foreign) {
    try {
      process.kill(o.pid, 'SIGTERM');
    } catch {
      /* already gone */
    }
  }
  if (await waitForPortFree(def.port, KILL_GRACE_MS)) return { replaced: true };

  for (const o of foreign) {
    try {
      process.kill(o.pid, 'SIGKILL');
    } catch {
      /* already gone */
    }
  }
  if (await waitForPortFree(def.port, 1200)) return { replaced: true };
  return { replaced: false, error: `foreign process still owns :${def.port}` };
}

// Prefer an explicit candidate (e.g. the bundled runtime), then the host's
// standard search dirs, then fall back to the bare name (resolved via PATH at
// spawn).
function resolveExecutable(name, preferred) {
  if (preferred && fs.existsSync(preferred)) return preferred;
  return currentHost ? currentHost.resolveBinary(name) : null;
}

// Build a PATH a Dock-launched app can use: bundled runtime first, then the
// usual locations where python3 / claude live (a GUI launch only inherits
// /usr/bin:/bin:/usr/sbin:/sbin).
function buildPath() {
  const extra = [];
  if (RUNTIME_BIN) extra.push(RUNTIME_BIN);
  extra.push(
    path.join(os.homedir(), '.local', 'bin'),
    '/usr/local/bin',
    '/opt/homebrew/bin',
    path.join(os.homedir(), '.npm-global', 'bin'),
  );
  const seen = new Set();
  const merged = [];
  for (const p of [...extra, ...(process.env.PATH || '').split(':')]) {
    if (p && !seen.has(p)) {
      seen.add(p);
      merged.push(p);
    }
  }
  return merged.join(':');
}

function buildBaseEnv(host) {
  const workspace = host.resolveWorkspace();
  const fileEnv = host.parseEnv(host.safeRead(host.envFilePath())); // workspace .env (Settings writes here)
  // workspace .env overrides process.env — it's the authoritative store for the
  // user's keys (GEMINI_API_KEY, ANTHROPIC_API_KEY, …).
  //
  // We deliberately do NOT set SUTANDO_WORKSPACE: it was removed from the
  // resolver in v0.8 (#1440) — the backend now ignores it and prints a
  // deprecation warning per process. Services resolve the workspace from
  // sutando.config.json themselves (the value we computed above is the same one
  // they'll derive). Override with SUTANDO_WORKSPACE_DIR for a launch, or
  // sutando.config.local.json for a durable per-clone setting.
  //
  // WORKSPACE_DIR is voice-agent.ts's own env name for the state workspace (it
  // predates the workspace contract and is its ONLY backend consumer). Without
  // it, voice-agent falls back to `new URL('..', import.meta.url)` — the
  // read-only bundle, URL-encoded (%20 for spaces) — and crashes acquiring its
  // .voice-agent.pid lock (fatal ENOENT/EACCES) when the app runs from a
  // space-containing or read-only path. Point it at the resolved workspace so
  // pidfile/config/results land there.
  //
  // CLAUDE_CONFIG_DIR pins every child (init.sh, start-cli.sh's core agent,
  // bridges, health-check, skills/install.sh) to the workspace-scoped Claude
  // home (<workspace>/.claude-sutando) — the post-#1454 contract. The supervisor
  // replaced `bash src/startup.sh` (which a shell wrapper would otherwise set
  // this from), so WE must set it; otherwise channels / skills / sessions /
  // memory fall back to the global ~/.claude/ and the backend logs a
  // "CLAUDE_CONFIG_DIR not set" banner on every helper call.
  const env = {
    ...process.env,
    ...fileEnv,
    WORKSPACE_DIR: workspace,
    CLAUDE_CONFIG_DIR: CLAUDE_CONFIG_DIR || host.claudeConfigDir(),
  };
  env.PATH = buildPath();
  // Observability wiring. SUTANDO_OBS_ENDPOINT is the ingest base the CC obs hook
  // + voice/phone realtime clients POST to (obs-hook.sh → `${endpoint}/ingest/
  // claude-code-hooks`, realtime.ts → `${endpoint}/ingest/realtime`). When
  // nothing set it and the host is signed in, derive it from the cloud apiBase so
  // a packaged build captures to the cloud out of the box; a workspace .env (or a
  // local-collector override) still wins. SUTANDO_OBS_PORT remains the local
  // collector's bind for runs that DO override the endpoint back to localhost.
  if (!env.SUTANDO_OBS_PORT) env.SUTANDO_OBS_PORT = String(OBS_PORT);
  if (!env.SUTANDO_OBS_ENDPOINT) {
    try {
      const base = String(host.readCloudAuth().apiBase || '').replace(/\/+$/, '');
      if (base) env.SUTANDO_OBS_ENDPOINT = `${base}/api/usage/v2`;
    } catch {
      /* not signed in / no cloud-auth → leave unset; a workspace .env can set it */
    }
  }
  // Metering export — the collector forwards collected usage to the cloud's
  // /api/usage/v2. Defaults derive from cloud-auth (same apiBase + Bearer the
  // cloud client uses), but EACH var is independently overridable from the
  // workspace .env / launch env — so a local-cloud test build can point just the
  // endpoint elsewhere and still get the Bearer auth, e.g.:
  //   SUTANDO_METERING_ENDPOINT=http://localhost:3737/api/usage/v2
  // Only when signed in; the token is captured at backend start, so a sign-in
  // after boot needs a backend restart to take effect.
  try {
    const cloud = host.readCloudAuth();
    if (cloud.signedIn && cloud.token) {
      const base = String(cloud.apiBase).replace(/\/+$/, '');
      if (!env.SUTANDO_METERING_ENABLED) env.SUTANDO_METERING_ENABLED = '1';
      if (!env.SUTANDO_METERING_ENDPOINT) env.SUTANDO_METERING_ENDPOINT = `${base}/api/usage/v2`;
      if (!env.SUTANDO_METERING_HEADERS) env.SUTANDO_METERING_HEADERS = JSON.stringify({ Authorization: `Bearer ${cloud.token}` });
    }
  } catch {
    /* not signed in / no cloud-auth → export stays off unless .env sets it; the local ledger still captures */
  }
  return env;
}

// ----------------------------------------------------------------------------
// Port helpers
// ----------------------------------------------------------------------------

function portInUse(port) {
  return new Promise((resolve) => {
    const sock = net.connect({ host: '127.0.0.1', port }, () => {
      sock.destroy();
      resolve(true);
    });
    sock.on('error', () => resolve(false));
    sock.setTimeout(800, () => {
      sock.destroy();
      resolve(false);
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Poll until the port listens, the child exits, or we time out.
async function waitForPort(rec, timeoutMs) {
  const child = rec.child;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (rec.child !== child || (child && child.exitCode != null)) return false; // exited / replaced
    if (await portInUse(rec.def.port)) return true;
    await sleep(250);
  }
  return false;
}

// ----------------------------------------------------------------------------
// Logging
// ----------------------------------------------------------------------------

function logsDir(host) {
  return path.join(host.resolveWorkspace(), 'logs');
}

// Where the supervisor's OWN diagnostics (console.log/error '[supervisor] …')
// are mirrored. Default: <workspace>/logs/backend-supervisor.log (same dir as
// the per-service logs). Override with SUTANDO_SUPERVISOR_LOG=/abs/path.log.
function supervisorLogPath(host) {
  return process.env.SUTANDO_SUPERVISOR_LOG || path.join(logsDir(host), 'backend-supervisor.log');
}

// Tee the supervisor's own stdout/stderr to a file. Without this, the
// '[supervisor] …' lines go only to the host process's stdout, which is lost
// when the app is launched from Finder (no attached terminal). We mirror them to
// supervisorLogPath while still passing through to the original console, so dev
// runs are unchanged. Idempotent + best-effort: any failure leaves console
// untouched.
let supervisorLogStream = null;

function initSupervisorLog(host) {
  if (supervisorLogStream) return; // already wired (superviseBackend may be re-entered)
  let dest;
  try {
    dest = supervisorLogPath(host);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    supervisorLogStream = fs.createWriteStream(dest, { flags: 'a' });
  } catch {
    supervisorLogStream = null; // couldn't open — leave console as-is
    return;
  }
  const tee = (orig) => (...args) => {
    try {
      const line = args.map((a) => (typeof a === 'string' ? a : util.inspect(a))).join(' ');
      supervisorLogStream.write(`[${new Date().toISOString()}] ${line}\n`);
    } catch {
      /* never let logging break the supervisor */
    }
    orig(...args);
  };
  console.log = tee(console.log.bind(console));
  console.warn = tee(console.warn.bind(console));
  console.error = tee(console.error.bind(console));
  console.log(`[supervisor] supervisor log → ${dest}`);
}

function pushTail(rec, buf) {
  const text = buf.toString('utf8');
  for (const line of text.split('\n')) {
    if (line.trim()) rec.logTail.push(line);
  }
  if (rec.logTail.length > TAIL_LINES) rec.logTail.splice(0, rec.logTail.length - TAIL_LINES);
}

function tailError(rec) {
  for (let i = rec.logTail.length - 1; i >= 0; i--) {
    if (rec.logTail[i].trim()) return rec.logTail[i].trim();
  }
  return null;
}

// ----------------------------------------------------------------------------
// Records
// ----------------------------------------------------------------------------

function recordFor(def) {
  let rec = state.get(def.name);
  if (!rec) {
    rec = {
      def,
      child: null,
      env: null,
      state: 'idle', // idle | starting | ready | adopted | backoff | failed | stopped
      restarts: 0,
      restartTimes: [],
      lastError: null,
      logTail: [],
      logStream: null,
      timer: null,
      stopping: false,
    };
    state.set(def.name, rec);
  }
  return rec;
}

function closeLog(rec) {
  if (rec.logStream) {
    try {
      rec.logStream.end();
    } catch {
      /* ignore */
    }
    rec.logStream = null;
  }
}

// ----------------------------------------------------------------------------
// Spawn + readiness
// ----------------------------------------------------------------------------

function spawnArgs(def) {
  const entryAbs = path.join(BACKEND_DIR, def.entry);
  if (def.kind === 'python') {
    return { cmd: PYTHON_BIN, args: [entryAbs], entryAbs, ok: fs.existsSync(entryAbs) };
  }
  // node service via the bundled tsx CLI — invoke node directly so we never hit
  // npx's install/network path on a read-only bundle.
  const tsxCli = path.join(BACKEND_DIR, 'node_modules', 'tsx', 'dist', 'cli.mjs');
  return {
    cmd: NODE_BIN,
    args: [tsxCli, entryAbs],
    entryAbs,
    ok: fs.existsSync(tsxCli) && fs.existsSync(entryAbs),
  };
}

// node:sqlite (imported by src/conversation-store.ts, which voice-agent pulls
// in) is gated behind --experimental-sqlite on the bundled Node 22.x runtime —
// without it the service throws ERR_UNKNOWN_BUILTIN_MODULE at load, crash-loops,
// and never opens its port. tsx forks a child node to run the entry, so a CLI
// flag on the parent node never reaches the fork — the flag must travel via
// NODE_OPTIONS, which the fork inherits. Scope it to OUR node services (spawned
// with the bundled runtime that supports the flag). It must NOT leak to the core
// agent's node via baseEnv: that runs a user-installed `claude` whose node may
// be <22.5, where --experimental-sqlite is rejected and would refuse to start.
function nodeChildEnv(def, env) {
  if (def.kind !== 'node') return env;
  const cur = String(env.NODE_OPTIONS || '');
  if (cur.includes('--experimental-sqlite')) return env;
  return { ...env, NODE_OPTIONS: (cur ? cur + ' ' : '') + '--experimental-sqlite' };
}

// ----------------------------------------------------------------------------
// Daemon (port-less) helpers — channel bridges
// ----------------------------------------------------------------------------

// Path to a channel's .env under the per-workspace Claude home (post-#1454).
// Falls back to ~/.claude/ only if CLAUDE_CONFIG_DIR wasn't computed yet.
function channelEnvFile(channel) {
  const home = CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
  return path.join(home, 'channels', channel, '.env');
}

// True if <claude-home>/channels/<channel>/.env defines a non-empty <key>.
// Mirrors startup.sh's `grep -q "<KEY>=" .env` gate; the bridge reads that file
// itself (via sutando-config.sh claude-home-path, which honors CLAUDE_CONFIG_DIR).
function channelTokenPresent(channel, key) {
  try {
    return new RegExp(`^\\s*${key}=\\S`, 'm').test(fs.readFileSync(channelEnvFile(channel), 'utf8'));
  } catch {
    return false;
  }
}

// Best-effort ordered python3 candidates. The PATH python3 is often a pyenv
// shim missing discord.py / slack_bolt, so we probe several and pick the first
// that imports the bridge's deps.
function pythonCandidates() {
  const raw = [
    BRIDGE_VENV_PY, // the venv populated with slack_bolt/discord.py — preferred for bridges
    RUNTIME_BIN ? path.join(RUNTIME_BIN, 'python3') : null,
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3',
    PYTHON_BIN,
    path.join(os.homedir(), '.pyenv', 'shims', 'python3'),
    '/usr/bin/python3',
  ];
  const seen = new Set();
  return raw.filter((p) => p && !seen.has(p) && (seen.add(p), true));
}

// Does `py` import every module in `mods`? (empty `mods` → any runnable python).
function pythonHasModules(py, mods) {
  try {
    const code =
      'import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in sys.argv[1:]) else 1)';
    return spawnSync(py, ['-c', code, ...mods], { stdio: 'ignore', timeout: 5000 }).status === 0;
  } catch {
    return false;
  }
}

// First candidate interpreter that satisfies `mods`, or null.
function resolveBridgePython(mods) {
  return pythonCandidates().find((py) => pythonHasModules(py, mods)) || null;
}

// Is a bridge process already running outside our supervision (e.g. left over
// from a prior session)? Adopting it avoids a double-spawn — two bridges on one
// channel means duplicate message handling / double replies.
function daemonRunning(entry) {
  try {
    return spawnSync('pgrep', ['-f', entry], { stdio: 'ignore', timeout: 3000 }).status === 0;
  } catch {
    return false;
  }
}

// Gate a daemon: opt-out flag, channel token, then a python with its deps.
// Returns { ok:true, python } to launch, or { ok:false, reason } to skip.
function gateDaemon(def) {
  if (process.env.SUTANDO_NO_BRIDGES === '1') return { ok: false, reason: 'disabled (SUTANDO_NO_BRIDGES=1)' };
  if (def.tokenKey && !channelTokenPresent(def.channel, def.tokenKey)) {
    return { ok: false, reason: `no ${def.channel} token (${channelEnvFile(def.channel)})` };
  }
  const deps = def.pyDeps || [];
  const python = resolveBridgePython(deps);
  if (!python) {
    return { ok: false, reason: `no python3 with: ${deps.join(', ') || 'python3'} (pip install ${deps.join(' ')})` };
  }
  return { ok: true, python };
}

// Open the per-service append log (shared by port + daemon launch).
function openLog(rec) {
  try {
    fs.mkdirSync(logsDir(currentHost), { recursive: true });
    rec.logStream = fs.createWriteStream(path.join(logsDir(currentHost), `${rec.def.name}.log`), { flags: 'a' });
  } catch {
    rec.logStream = null;
  }
}

// Wire stdout/stderr → log + tail, and the exit handler → restart, for a freshly
// spawned child. Shared by port + daemon launch so both behave identically.
function attachChild(rec, child) {
  rec.child = child;
  const onData = (buf) => {
    if (rec.logStream) {
      try {
        rec.logStream.write(buf);
      } catch {
        /* ignore */
      }
    }
    pushTail(rec, buf);
  };
  child.stdout.on('data', onData);
  child.stderr.on('data', onData);
  child.on('error', (e) => {
    rec.lastError = String(e && e.message ? e.message : e);
  });
  child.on('exit', (code, signal) => {
    if (rec.child === child) rec.child = null;
    closeLog(rec);
    if (rec.stopping) {
      rec.state = 'stopped';
      return;
    }
    rec.lastError = tailError(rec) || `exited (code ${code}${signal ? `, ${signal}` : ''})`;
    emit({ type: 'service-down', svc: rec.def.name, detail: rec.lastError });
    scheduleRestart(rec);
  });
}

// Poll until the child exits or the grace elapses; resolves to liveness.
async function waitForAlive(rec, child, graceMs) {
  const deadline = Date.now() + graceMs;
  while (Date.now() < deadline) {
    if (rec.child !== child || child.exitCode != null) return false;
    await sleep(250);
  }
  return rec.child === child && child.exitCode == null;
}

// Launch (or relaunch) a single service. Assumes rec.env is set. Resolves once
// the service is ready/adopted (true) or failed to come up (false).
async function launch(rec) {
  const def = rec.def;
  rec.stopping = false;

  if (def.daemon) return launchDaemon(rec);

  // In packaged builds, take ownership of fixed local ports before optional
  // service gates run. Otherwise a gated/disabled packaged service can leave a
  // stale dev checkout serving that endpoint.
  if (await portInUse(def.port)) {
    const takeover = await replaceForeignPortOwner(def);
    if (takeover.error) {
      rec.state = 'failed';
      rec.lastError = takeover.error;
      emit({ type: 'service-error', svc: def.name, code: 'foreign-port-owner', detail: rec.lastError });
      return false;
    }
  }

  // Capability gate (e.g. screen-capture needs the Screen Recording grant).
  // Skip — not an error — so the UI can explain instead of crash-looping.
  if (def.gate) {
    const g = def.gate(currentHost, rec.env);
    if (!g.ok) {
      rec.state = 'skipped';
      rec.lastError = g.reason || 'skipped';
      emit({ type: 'service-skipped', svc: def.name, detail: rec.lastError });
      return false;
    }
  }

  // If the port is still occupied after the packaged-app ownership check,
  // treat it as intentionally adopted.
  if (await portInUse(def.port)) {
    rec.state = 'adopted';
    rec.lastError = null;
    if (def.onReady) def.onReady(rec.env);
    emit({ type: 'service-up', svc: def.name, detail: 'adopted (port already in use)' });
    return true;
  }

  // Required env (e.g. GEMINI_API_KEY for voice) — gate before spawning so a
  // missing key surfaces in the UI instead of a crash-loop.
  if (def.requires) {
    const missing = def.requires.filter((k) => !String(rec.env[k] ?? '').trim());
    if (missing.length) {
      rec.state = 'failed';
      rec.lastError = `${missing.join(', ')} not set`;
      emit({ type: 'service-error', svc: def.name, code: 'missing-env', detail: rec.lastError, keys: missing });
      return false;
    }
  }

  const { cmd, args, entryAbs, ok } = spawnArgs(def);
  if (!ok) {
    rec.state = 'failed';
    rec.lastError = `entry not found: ${entryAbs}`;
    emit({ type: 'service-error', svc: def.name, code: 'missing-entry', detail: rec.lastError });
    return false;
  }

  openLog(rec);

  rec.state = 'starting';
  let child;
  try {
    child = spawn(cmd, args, {
      cwd: BACKEND_DIR,
      env: nodeChildEnv(def, rec.env),
      // Own process group → group-kill on shutdown (tsx forks a child node).
      // Opt out (noDetach) for screen-capture so it stays a non-disclaimed
      // descendant of the host and inherits the host app's TCC responsibility;
      // killGroup falls back to a direct child.kill() for those.
      detached: !def.noDetach,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (e) {
    rec.state = 'failed';
    rec.lastError = String(e && e.message ? e.message : e);
    closeLog(rec);
    emit({ type: 'service-error', svc: def.name, code: 'spawn-failed', detail: rec.lastError });
    return false;
  }
  attachChild(rec, child);

  const timeout = def.port === PROXY_PORT ? PROXY_READY_TIMEOUT_MS : READY_TIMEOUT_MS;
  const ready = await waitForPort(rec, timeout);
  if (ready) {
    rec.state = 'ready';
    rec.lastError = null;
    if (def.onReady) def.onReady(rec.env);
    emit({ type: 'service-up', svc: def.name, detail: `listening on :${def.port}` });
    return true;
  }

  // Still in 'starting' (no port) — if the process is alive it's stuck; the exit
  // handler covers the already-exited case (it schedules a restart).
  if (rec.child === child && child.exitCode == null) {
    rec.lastError = `did not open :${def.port} within ${timeout}ms`;
    killGroup(child); // exit handler will schedule the restart
  }
  return false;
}

// Launch a port-less daemon (channel bridge). Same restart/log machinery as a
// port service, but gated (token + python deps), adopts an already-running
// instance, and uses an alive-after-grace readiness check instead of a port.
async function launchDaemon(rec) {
  const def = rec.def;

  const gate = gateDaemon(def);
  if (!gate.ok) {
    rec.state = 'skipped';
    rec.lastError = gate.reason;
    emit({ type: 'service-skipped', svc: def.name, detail: gate.reason });
    return false;
  }

  if (daemonRunning(def.entry)) {
    rec.state = 'adopted';
    rec.lastError = null;
    emit({ type: 'service-up', svc: def.name, detail: 'adopted (already running)' });
    return true;
  }

  const entryAbs = path.join(BACKEND_DIR, def.entry);
  if (!fs.existsSync(entryAbs)) {
    rec.state = 'failed';
    rec.lastError = `entry not found: ${entryAbs}`;
    emit({ type: 'service-error', svc: def.name, code: 'missing-entry', detail: rec.lastError });
    return false;
  }

  openLog(rec);
  rec.state = 'starting';
  let child;
  try {
    child = spawn(gate.python, [entryAbs], {
      cwd: BACKEND_DIR,
      env: rec.env,
      detached: true, // own process group → group-kill on shutdown
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (e) {
    rec.state = 'failed';
    rec.lastError = String(e && e.message ? e.message : e);
    closeLog(rec);
    emit({ type: 'service-error', svc: def.name, code: 'spawn-failed', detail: rec.lastError });
    return false;
  }
  attachChild(rec, child);

  const alive = await waitForAlive(rec, child, DAEMON_GRACE_MS);
  if (alive) {
    rec.state = 'ready';
    rec.lastError = null;
    emit({ type: 'service-up', svc: def.name, detail: 'running (daemon)' });
    return true;
  }
  // Exited within the grace → the exit handler already scheduled a restart.
  return false;
}

function scheduleRestart(rec) {
  if (rec.stopping || !started) return;
  const now = Date.now();
  rec.restarts++;
  rec.restartTimes.push(now);
  rec.restartTimes = rec.restartTimes.filter((t) => now - t < STORM_WINDOW_MS);

  if (rec.restartTimes.length >= STORM_MAX_RESTARTS) {
    rec.state = 'failed';
    rec.lastError = rec.lastError || 'crashed repeatedly (storm breaker tripped)';
    emit({ type: 'service-error', svc: rec.def.name, code: 'crash-loop', detail: rec.lastError });
    return;
  }

  const n = rec.restartTimes.length;
  const cap = Math.min(BACKOFF_BASE_MS * 2 ** (n - 1), BACKOFF_CAP_MS);
  const jitter = cap * 0.2 * (Math.random() * 2 - 1);
  const delay = Math.max(250, Math.round(cap + jitter));
  rec.state = 'backoff';
  rec.timer = setTimeout(() => {
    rec.timer = null;
    if (!rec.stopping && started) void launch(rec);
  }, delay);
}

// ----------------------------------------------------------------------------
// Shutdown
// ----------------------------------------------------------------------------

function killGroup(child, signal) {
  if (!child || child.exitCode != null) return;
  try {
    process.kill(-child.pid, signal || 'SIGTERM'); // negative pid → whole group
  } catch {
    try {
      child.kill(signal || 'SIGTERM');
    } catch {
      /* already gone */
    }
  }
}

function stopOne(rec) {
  return new Promise((resolve) => {
    rec.stopping = true;
    if (rec.timer) {
      clearTimeout(rec.timer);
      rec.timer = null;
    }
    const child = rec.child;
    if (!child || child.exitCode != null) {
      rec.state = 'stopped';
      closeLog(rec);
      return resolve();
    }
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      rec.state = 'stopped';
      closeLog(rec);
      resolve();
    };
    child.once('exit', finish);
    killGroup(child, 'SIGTERM');
    setTimeout(() => {
      if (done) return;
      killGroup(child, 'SIGKILL');
      setTimeout(finish, 300);
    }, KILL_GRACE_MS);
  });
}

// ----------------------------------------------------------------------------
// init.sh --auto (idempotent dir/seed bootstrap; never `npm install`)
// ----------------------------------------------------------------------------

function runInitAuto(baseEnv) {
  return new Promise((resolve) => {
    const script = path.join(BACKEND_DIR, 'src', 'init.sh');
    if (!fs.existsSync(script)) return resolve();
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    try {
      const p = spawn('bash', [script, '--auto'], { cwd: BACKEND_DIR, env: baseEnv, stdio: 'ignore' });
      p.on('error', finish);
      p.on('exit', finish);
      setTimeout(finish, 20_000); // never block boot on a hung bootstrap
    } catch {
      finish();
    }
  });
}

// ----------------------------------------------------------------------------
// Per-workspace Claude config dir (CLAUDE_CONFIG_DIR = <workspace>/.claude-sutando)
// ----------------------------------------------------------------------------

// Post-#1454, every Sutando service resolves channels / skills / sessions /
// memory / auth under CLAUDE_CONFIG_DIR (workspace-scoped) instead of the global
// ~/.claude/. We set CLAUDE_CONFIG_DIR in baseEnv, but on FIRST boot that target
// is empty — which would orphan an existing user's channel tokens, Claude auth,
// and memory. sutando-shell-setup.sh --import does a non-destructive, idempotent
// rsync of ~/.claude → <workspace>/.claude-sutando to carry that state forward
// (it auto-proceeds when stdin isn't a TTY). We run it ONCE — skipped when the
// target already exists — and always ensure the dir exists afterward. Best-effort
// + timeboxed: a fresh install with no ~/.claude just no-ops. Must precede
// runSkillsInstall (so bundled skills overlay into the populated dir) and
// launchCore (so the core agent sees the imported channels/sessions/memory).
function ensureClaudeConfigDir(baseEnv) {
  return new Promise((resolve) => {
    const target = baseEnv.CLAUDE_CONFIG_DIR;
    if (!target) return resolve();
    let firstBoot = false;
    try {
      firstBoot = !fs.existsSync(target);
    } catch {
      firstBoot = true;
    }
    const ensureDir = () => {
      try {
        fs.mkdirSync(target, { recursive: true });
      } catch {
        /* best effort */
      }
    };
    if (!firstBoot) {
      ensureDir();
      return resolve();
    }
    const script = path.join(BACKEND_DIR, 'scripts', 'sutando-shell-setup.sh');
    if (!fs.existsSync(script)) {
      ensureDir();
      return resolve();
    }
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      ensureDir(); // create the dir even if the import failed/short-circuited
      resolve();
    };
    try {
      console.log(`[supervisor] first boot — importing ~/.claude → ${target} (sutando-shell-setup --import)`);
      const p = spawn('bash', [script, '--import'], { cwd: BACKEND_DIR, env: baseEnv, stdio: 'ignore' });
      p.on('error', finish);
      p.on('exit', finish);
      setTimeout(finish, 60_000); // rsync of ~/.claude can take a bit; never block boot forever
    } catch {
      finish();
    }
  });
}

// ----------------------------------------------------------------------------
// skills/install.sh (idempotently symlinks bundled skills → CLAUDE_CONFIG_DIR/skills/)
// ----------------------------------------------------------------------------

// claude looks up slash commands (e.g. /schedule-crons, which start-cli.sh
// feeds the core agent) in <claude-home>/skills/, which a fresh user has empty.
// The bundled app ships skills at BACKEND_DIR/skills/ but they're invisible to
// the CLI until symlinked into the user's config dir. Without this, fresh
// installs crash-loop the core agent with "Unknown skill: schedule-crons".
// install.sh is idempotent + non-destructive (leaves existing symlinks/dirs
// alone), so it's safe to run every boot. Must precede launchCore.
function runSkillsInstall(baseEnv) {
  return new Promise((resolve) => {
    const script = path.join(BACKEND_DIR, 'skills', 'install.sh');
    if (!fs.existsSync(script)) return resolve();
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    try {
      const p = spawn('bash', [script], { cwd: BACKEND_DIR, env: baseEnv, stdio: 'ignore' });
      p.on('error', finish);
      p.on('exit', finish);
      setTimeout(finish, 20_000); // never block boot on a hung symlink sweep
    } catch {
      finish();
    }
  });
}

// ----------------------------------------------------------------------------
// Channel-bridge Python deps (slack_bolt / discord.py)
// ----------------------------------------------------------------------------

// A fresh Mac has neither slack_bolt nor discord.py, so the Slack + Discord
// bridges are skipped at boot (gateDaemon can't find an interpreter with the
// dep) and Slack looks "completely broken". ensure-bridge-deps.sh populates a
// workspace venv (BRIDGE_VENV_PY) with both. We run it in the BACKGROUND so
// first boot stays fast, then relaunch any bridge that was skipped purely for a
// missing dep. Idempotent + best-effort: a no-op once the venv is populated; an
// offline failure just leaves the bridges skipped (surfaced in the UI).
// Telegram is stdlib-only and unaffected.
function ensureBridgeDepsThenLaunch(baseEnv) {
  const script = path.join(BACKEND_DIR, 'scripts', 'ensure-bridge-deps.sh');
  if (!fs.existsSync(script)) return;
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    for (const def of SERVICES) {
      if (!def.daemon || !(def.pyDeps && def.pyDeps.length)) continue;
      const rec = state.get(def.name);
      // Retry ONLY bridges skipped for a missing interpreter/dep — not ones
      // skipped for a missing channel token (those need the user's token first).
      if (rec && rec.state === 'skipped' && /no python3 with/.test(rec.lastError || '')) {
        rec.env = { ...baseEnv };
        emit({ type: 'service-retry', svc: def.name, detail: 'bridge deps installed — retrying' });
        void launch(rec);
      }
    }
  };
  try {
    const p = spawn('bash', [script], { cwd: BACKEND_DIR, env: baseEnv, stdio: 'ignore' });
    p.on('error', finish);
    p.on('exit', finish);
    setTimeout(finish, 180_000); // safety cap — relaunch even if the installer hangs
  } catch {
    finish();
  }
}

// ----------------------------------------------------------------------------
// Memory dir — pin SUTANDO_MEMORY_DIR to ONE stable store (survives upgrades)
// ----------------------------------------------------------------------------

// Sutando's long-term memory lives at <claude-home>/projects/<slug>/memory,
// where <slug> is derived from the agent's working directory. Because the core
// agent's cwd is the repo path — which MOVES across upgrades and relocations —
// every move spawns a fresh, empty store and orphans the previous one. That is
// the "memories lost on upgrade" bug. (The TS voice-agent and Python
// health-check defaults can even derive DIFFERENT slugs, fragmenting memory
// within a single install.)
//
// Fix: pin SUTANDO_MEMORY_DIR to one path-independent store that every child
// inherits — both the TS and Python defaults honor the env var, as does the core
// agent via its CLAUDE.md. Default ~/.sutando/memory (parallels the workspace at
// ~/.sutando/repo/workspace); override via the workspace .env. Then point Claude
// Code's own cwd-keyed auto-memory dir at the same store via a symlink — but only
// when that dir doesn't already exist, so we never disturb a populated store.
function ensureMemoryDir(baseEnv) {
  let memDir = (baseEnv.SUTANDO_MEMORY_DIR || '').trim();
  memDir = memDir
    ? memDir.replace(/^~(?=$|\/)/, os.homedir())
    : path.join(os.homedir(), '.sutando', 'memory');
  try {
    fs.mkdirSync(memDir, { recursive: true });
  } catch (e) {
    console.error('[supervisor] memory dir mkdir failed:', e && e.message);
  }
  baseEnv.SUTANDO_MEMORY_DIR = memDir;

  // Converge Claude Code's cwd-keyed auto-memory onto the same store. Claude
  // slugifies the absolute cwd with BOTH '/' and '.' replaced by '-'. Post-#1454
  // its projects/ dir lives under CLAUDE_CONFIG_DIR (the per-workspace home),
  // not the global ~/.claude/, so resolve the base from baseEnv.
  try {
    const claudeHome = baseEnv.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
    const slug = path.resolve(BACKEND_DIR).replace(/[/.]/g, '-');
    const cwdMemDir = path.join(claudeHome, 'projects', slug, 'memory');
    if (path.resolve(cwdMemDir) === path.resolve(memDir)) return;

    const mdFiles = (dir) => {
      try {
        return fs.readdirSync(dir).filter((f) => f.endsWith('.md'));
      } catch {
        return [];
      }
    };

    if (!fs.existsSync(cwdMemDir)) {
      // Fresh cwd — a new install, or a path that moved on upgrade/relocation.
      // Point its auto-memory straight at the pinned store so it never fragments.
      fs.mkdirSync(path.dirname(cwdMemDir), { recursive: true });
      fs.symlinkSync(memDir, cwdMemDir);
      console.log(`[supervisor] linked cwd auto-memory ${cwdMemDir} → ${memDir}`);
    } else if (mdFiles(memDir).length === 0 && mdFiles(cwdMemDir).length > 0) {
      // First boot of a memory-pinning build for an upgrader who already had
      // cwd-keyed memory: carry it forward into the pinned store so nothing is
      // orphaned. Copy-only — the original dir is left intact.
      for (const f of mdFiles(cwdMemDir)) {
        const dst = path.join(memDir, f);
        if (!fs.existsSync(dst)) fs.copyFileSync(path.join(cwdMemDir, f), dst);
      }
      console.log(`[supervisor] seeded pinned memory ${memDir} from ${cwdMemDir}`);
    }
  } catch (e) {
    console.error('[supervisor] memory convergence failed (non-fatal):', e && e.message);
  }
}

// ----------------------------------------------------------------------------
// Core agent — Claude Code in a detached tmux session (sutando-core)
// ----------------------------------------------------------------------------

// This is what the Core CLI pane attaches to and what actually runs the agent
// (tasks, crons). It's a tmux session, not a port-bound service, so we fire the
// canonical launcher (scripts/start-cli.sh) and let it manage tmux: it's
// idempotent (attaches/skips if already running) and takes its detached branch
// automatically when stdio isn't a TTY. We don't health-track it (tmux owns its
// lifecycle while running), but stop() kills the session on shutdown via
// stopCore() so quitting the host tears everything down. Opt out with
// SUTANDO_NO_CORE=1.
//
// baseEnv carries ANTHROPIC_BASE_URL (set once the proxy is up), so the core
// agent's Claude calls route through the credential proxy for quota tracking.
// Needs `claude` (user-installed) + tmux (bundled in runtime/bin, on PATH).
function launchCore(baseEnv) {
  if (process.env.SUTANDO_NO_CORE === '1') return;
  const script = path.join(BACKEND_DIR, 'scripts', 'start-cli.sh');
  if (!fs.existsSync(script)) return;
  try {
    // start-cli.sh cd's to its own $REPO (the code checkout / app bundle) on
    // entry, so the spawn cwd below doesn't survive. Pass the stable working dir
    // explicitly via SUTANDO_CLAUDE_WORKING_DIR: the same resolved workspace the
    // bridges and Settings use. In packaged builds this is the stable
    // ~/.sutando/repo/workspace; in dev an explicit SUTANDO_BACKEND_DIR checkout
    // can resolve to its own <repo>/workspace. Unset in upstream OSS → no
    // override; we opt in here.
    const coreWorkingDir = baseEnv.WORKSPACE_DIR || currentHost.resolveWorkspace();
    console.log('[supervisor] launch core', coreWorkingDir);
    const coreEnv = { ...baseEnv, SUTANDO_CLAUDE_WORKING_DIR: coreWorkingDir };
    const restart = replaceMismatchedCoreSession(coreWorkingDir);
    if (restart) {
      console.warn(`[supervisor] core session cwd mismatch; restarting ${CORE_TMUX_SESSION} for ${coreWorkingDir}`);
    }
    const p = spawn('bash', restart ? [script, '--restart'] : [script], {
      cwd: coreWorkingDir,
      env: coreEnv,
      stdio: 'ignore',
      detached: true,
    });
    p.on('error', (e) => console.error('[supervisor] core agent launch failed:', e && e.message));
    p.unref();
    console.log('[supervisor] core agent (sutando-core) launch requested');
  } catch (e) {
    console.error('[supervisor] core agent launch failed:', e && e.message);
  }
}

// The sutando-core tmux session (the Claude Code core agent) that launchCore /
// start-cli.sh create. Socket + session name MUST match scripts/start-cli.sh.
const CORE_TMUX_SOCKET = '/tmp/sutando-tmux.sock';
const CORE_TMUX_SESSION = 'sutando-core';

function coreSessionCwd() {
  try {
    const tmux = resolveExecutable('tmux', RUNTIME_BIN ? path.join(RUNTIME_BIN, 'tmux') : null) || 'tmux';
    const out = spawnSync(
      tmux,
      ['-S', CORE_TMUX_SOCKET, 'display-message', '-p', '-t', CORE_TMUX_SESSION, '#{pane_current_path}'],
      { encoding: 'utf8', timeout: 3000 },
    );
    if (out.status !== 0) return null;
    return out.stdout.trim() || null;
  } catch {
    return null;
  }
}

// Both launch paths (launchCore → start-cli.sh) run `claude --name <session>`,
// so argv carries this marker. We key the health probe off argv rather than
// tmux's #{pane_current_command}: that format reports the kernel's exec name
// (p_comm) — the basename of the file actually exec'd — and the native Claude
// Code installer points `claude` at a version-named binary
// (~/.local/share/claude/versions/2.1.209). tmux therefore reports "2.1.209",
// never "claude", so a `command !== 'claude'` check called a HEALTHY core
// unhealthy and the watchdog restarted it every tick. argv survives the
// versioned filename (and any future rename of the installed binary).
const CORE_ARGV_MARKER = `--name ${CORE_TMUX_SESSION}`;
const CORE_PROC_SCAN_DEPTH = 2; // pane pid + descendants, in case a shell owns the pane

// The probes below take `run` so tests can drive them off a fake tmux/ps/pgrep;
// production always gets this default.
function runCapture(file, args, options) {
  return spawnSync(file, args, { encoding: 'utf8', timeout: 3000, ...options });
}

function processArgs(pid, run = runCapture) {
  try {
    const out = run('ps', ['-p', String(pid), '-o', 'args=']);
    if (out.status !== 0) return null;
    return out.stdout.trim() || null;
  } catch {
    return null;
  }
}

function childPids(pid, run = runCapture) {
  try {
    const out = run('pgrep', ['-P', String(pid)]);
    if (out.status !== 0) return [];
    return out.stdout
      .split('\n')
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isInteger(n) && n > 0);
  } catch {
    return [];
  }
}

function isCoreArgv(args) {
  if (!args) return false;
  if (args.includes(CORE_ARGV_MARKER)) return true;
  const argv0 = args.trim().split(/\s+/)[0] || '';
  return path.basename(argv0) === 'claude';
}

function coreProcessRunning(panePid, run = runCapture) {
  let frontier = [panePid];
  for (let depth = 0; depth <= CORE_PROC_SCAN_DEPTH && frontier.length; depth++) {
    for (const pid of frontier) {
      if (isCoreArgv(processArgs(pid, run))) return true;
    }
    frontier = frontier.flatMap((pid) => childPids(pid, run));
  }
  return false;
}

function coreSessionInfo({ run = runCapture, processRunning = coreProcessRunning } = {}) {
  try {
    const tmux = resolveExecutable('tmux', RUNTIME_BIN ? path.join(RUNTIME_BIN, 'tmux') : null) || 'tmux';
    // SPACE separators, never control characters. This probe used to join the
    // fields with \t — and a Dock/launchd-launched app has no LANG/LC_* in its
    // environment, so tmux runs under the C locale and sanitizes control chars
    // in display-message output: the tabs came back as "_", Number("7857_0_/…")
    // was NaN, and every healthy core was reported "pane is dead" and restarted
    // on each watchdog tick. Terminal launches inherit a UTF-8 locale, which is
    // why only packaged/Dock runs stormed. pid and dead are space-free, so the
    // greedy tail keeps a path with spaces intact.
    const out = run(
      tmux,
      ['-S', CORE_TMUX_SOCKET, 'display-message', '-p', '-t', CORE_TMUX_SESSION, '#{pane_pid} #{pane_dead} #{pane_current_path}'],
      { env: { ...process.env, PATH: buildPath() } },
    );
    if (out.status !== 0) {
      const detail = (out.stderr || out.stdout || '').trim();
      return { exists: false, ok: false, reason: detail || 'tmux session missing' };
    }
    const m = /^(\d+) ([01]) (.*)$/.exec((out.stdout || '').trim());
    if (!m) {
      // display-message exited 0, so the session and pane exist — the OUTPUT is
      // what we couldn't read. Both restart storms this probe has caused came
      // from treating a probe-side surprise as a dead core; a genuinely dead
      // pane is caught above (session gone → status ≠ 0) or below (dead flag).
      // Fail open: skipping one health check beats killing a working core.
      return { exists: true, ok: true, pid: null, cwd: null, reason: `probe output unrecognized (treated as healthy): ${JSON.stringify((out.stdout || '').trim().slice(0, 80))}` };
    }
    const [, pidRaw, dead, cwd] = m;
    const pid = Number(pidRaw);
    if (dead === '1' || pid <= 0) {
      return { exists: true, ok: false, pid: null, cwd: cwd || null, reason: 'pane is dead' };
    }
    if (!processRunning(pid)) {
      return { exists: true, ok: false, pid, cwd: cwd || null, reason: `pane pid ${pid} is not running the core agent` };
    }
    return { exists: true, ok: true, pid, cwd: cwd || null, reason: 'running' };
  } catch (e) {
    return { exists: false, ok: false, reason: e && e.message ? e.message : 'tmux probe failed' };
  }
}

function coreSessionNeedsRestart(expectedCwd) {
  if (!strictRuntimeOwnership()) return false;
  const cwd = coreSessionCwd();
  return !!cwd && !samePath(cwd, expectedCwd);
}

function replaceMismatchedCoreSession(expectedCwd) {
  if (!coreSessionNeedsRestart(expectedCwd)) return false;
  try {
    const tmux = resolveExecutable('tmux', RUNTIME_BIN ? path.join(RUNTIME_BIN, 'tmux') : null) || 'tmux';
    const out = spawnSync(
      tmux,
      ['-S', CORE_TMUX_SOCKET, 'kill-session', '-t', CORE_TMUX_SESSION],
      { encoding: 'utf8', timeout: 3000, env: { ...process.env, PATH: buildPath() } },
    );
    if (out.status !== 0) {
      const detail = (out.stderr || out.stdout || '').trim();
      console.warn(`[supervisor] core session cwd mismatch; tmux kill-session failed${detail ? `: ${detail}` : ''}`);
    }
  } catch (e) {
    console.warn('[supervisor] core session cwd mismatch; tmux kill-session failed:', e && e.message);
  }
  return true;
}

// Kill the core-agent tmux session on shutdown so quitting the host stops
// EVERYTHING. launchCore fire-and-forgets the session, so without this a quit
// leaves a Claude running and burning quota. Gated on SUTANDO_NO_CORE so an
// instance that didn't manage the core won't kill one the user started
// independently. Never hangs shutdown.
function stopCore() {
  return new Promise((resolve) => {
    if (process.env.SUTANDO_NO_CORE === '1') return resolve();
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    try {
      const tmux = resolveExecutable('tmux', RUNTIME_BIN ? path.join(RUNTIME_BIN, 'tmux') : null) || 'tmux';
      const p = spawn(tmux, ['-S', CORE_TMUX_SOCKET, 'kill-session', '-t', CORE_TMUX_SESSION], {
        env: { ...process.env, PATH: buildPath() },
        stdio: 'ignore',
      });
      p.on('error', finish); // tmux missing or no server running → nothing to kill
      p.on('exit', finish);
      setTimeout(finish, 3000);
    } catch {
      finish();
    }
  });
}

// ----------------------------------------------------------------------------
// Core watchdog — external recovery + owner alert when the core wedges
// ----------------------------------------------------------------------------

// launchCore is fire-and-forget — the supervisor doesn't health-track the core
// (tmux owns its lifecycle). But the core agent can WEDGE: process alive, session
// frozen (e.g. a dead model socket), silently no longer draining tasks. A frozen
// core can't self-detect — the health-check that reads its heartbeat runs INSIDE
// the core. health-check.py detects the wedge externally (heartbeat ticking but
// the task queue not draining) and, with these flags, restarts the core
// (--recover-core: guarded by a confirm window + 30-min cooldown + hourly cap)
// and DMs the owner on Slack (--notify-slack: stdlib urllib, core-independent).
// We run it from HERE — the host process, which is external to the core, so a
// frozen core can't suppress it. We deliberately omit --fix/--emit-task: the
// supervisor owns service lifecycle (so --fix would fight it) and --emit-task
// needs a live core.
let watchdogTimer = null;
let criticalReconcileRunning = false;
let lastCoreRecoveryAt = 0;
const WATCHDOG_INTERVAL_MS = envMs('SUTANDO_WATCHDOG_INTERVAL_MS', 2 * 60_000);
const WATCHDOG_MAX_RUN_MS = 90_000;
const CORE_RECOVERY_COOLDOWN_MS = 2 * 60_000;

function envMs(name, fallback) {
  const n = Number(process.env[name]);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function missingRequiredEnv(def, env) {
  if (!def.requires) return [];
  return def.requires.filter((k) => !String(env && env[k] != null ? env[k] : '').trim());
}

function criticalGateBlocked(def, env) {
  if (!def.gate) return null;
  try {
    const g = def.gate(currentHost, env);
    return g.ok ? null : (g.reason || 'gated off');
  } catch {
    return null;
  }
}

async function reconcileCriticalPortService(def, baseEnv) {
  const rec = recordFor(def);
  if (rec.timer || rec.state === 'starting' || rec.state === 'backoff') return;
  if (rec.state === 'failed' && rec.restartTimes.length >= STORM_MAX_RESTARTS) return;
  if (await portInUse(def.port)) return;

  const env = rec.env || { ...baseEnv };
  const missing = missingRequiredEnv(def, env);
  if (missing.length) return;
  const gateReason = criticalGateBlocked(def, env);
  if (gateReason) return;

  console.warn(`[supervisor] watchdog: ${def.name} is not listening on :${def.port}; restarting`);
  emit({ type: 'service-retry', svc: def.name, detail: `watchdog restart: :${def.port} not listening` });

  if (rec.child && rec.child.exitCode == null) {
    await stopOne(rec);
  }
  rec.env = env;
  rec.stopping = false;
  await launch(rec);
}

async function reconcileCore(baseEnv) {
  if (process.env.SUTANDO_NO_CORE === '1') return;
  const expectedCwd = baseEnv.WORKSPACE_DIR || currentHost.resolveWorkspace();
  const info = coreSessionInfo();
  const cwdMismatch = info.ok && strictRuntimeOwnership() && info.cwd && !samePath(info.cwd, expectedCwd);
  if (info.ok && !cwdMismatch) return;

  const now = Date.now();
  if (now - lastCoreRecoveryAt < CORE_RECOVERY_COOLDOWN_MS) return;
  lastCoreRecoveryAt = now;

  const reason = cwdMismatch
    ? `cwd mismatch (${info.cwd} != ${expectedCwd})`
    : info.reason;
  console.warn(`[supervisor] watchdog: ${CORE_TMUX_SESSION} unhealthy (${reason}); restarting core`);
  emit({ type: 'service-retry', svc: CORE_TMUX_SESSION, detail: `watchdog restart: ${reason}` });

  await stopCore();
  launchCore(baseEnv);
}

async function reconcileCriticalServices(baseEnv) {
  if (criticalReconcileRunning || !started) return;
  criticalReconcileRunning = true;
  try {
    await reconcileCore(baseEnv);
    for (const def of SERVICES) {
      if (!def.port || !CRITICAL_SERVICE_NAMES.has(def.name)) continue;
      await reconcileCriticalPortService(def, baseEnv);
    }
  } finally {
    criticalReconcileRunning = false;
  }
}

function runHealthWatchdog(baseEnv) {
  const script = path.join(BACKEND_DIR, 'src', 'health-check.py');
  if (!fs.existsSync(script)) return;
  try {
    const p = spawn(PYTHON_BIN, [script, '--recover-core', '--notify-slack'], {
      cwd: BACKEND_DIR,
      env: baseEnv,
      stdio: 'ignore',
    });
    p.on('error', () => {}); // python missing / transient — try again next tick
    const kill = setTimeout(() => {
      try {
        p.kill();
      } catch {
        /* already gone */
      }
    }, WATCHDOG_MAX_RUN_MS);
    p.on('exit', () => clearTimeout(kill));
  } catch {
    /* best effort — never let the watchdog throw into the supervisor */
  }
}

function startWatchdog(baseEnv) {
  // Tie to core management: if we don't run the core (SUTANDO_NO_CORE), don't try
  // to recover it. SUTANDO_NO_WATCHDOG is a separate opt-out.
  if (process.env.SUTANDO_NO_CORE === '1' || process.env.SUTANDO_NO_WATCHDOG === '1') return;
  stopWatchdog();
  // First tick after one interval, so the core has time to come up (no
  // false-positive "wedged" during boot).
  watchdogTimer = setInterval(() => {
    void reconcileCriticalServices(baseEnv);
    runHealthWatchdog(baseEnv);
  }, WATCHDOG_INTERVAL_MS);
  if (watchdogTimer.unref) watchdogTimer.unref();
}

function stopWatchdog() {
  if (watchdogTimer) {
    clearInterval(watchdogTimer);
    watchdogTimer = null;
  }
}

// ----------------------------------------------------------------------------
// Public API
// ----------------------------------------------------------------------------

async function superviseBackend(host) {
  if (started) return status();
  currentHost = host;
  initSupervisorLog(host); // mirror our own stdout/stderr → <workspace>/logs/backend-supervisor.log
  computeDirs(host);
  started = true;

  const baseEnv = buildBaseEnv(host);
  // Seed the per-workspace Claude config dir (CLAUDE_CONFIG_DIR) from ~/.claude on
  // first boot so channels/skills/sessions/memory/auth carry forward. Must run
  // before memory convergence + skills install + the core agent (see
  // ensureClaudeConfigDir).
  await ensureClaudeConfigDir(baseEnv);
  // Pin SUTANDO_MEMORY_DIR before any child inherits baseEnv, so memory no longer
  // fragments by cwd across upgrades (see ensureMemoryDir).
  ensureMemoryDir(baseEnv);
  await runInitAuto(baseEnv);
  // Seed <claude-home>/skills/ so the core agent can resolve /schedule-crons et
  // al. (must precede launchCore — see runSkillsInstall).
  await runSkillsInstall(baseEnv);

  // 1. Credential proxy first — once it's up, ANTHROPIC_BASE_URL is added to
  //    baseEnv so the other services route Claude calls through it.
  const proxyDef = SERVICES.find((s) => s.port === PROXY_PORT);
  if (proxyDef) {
    const rec = recordFor(proxyDef);
    rec.env = baseEnv; // proxy mutates baseEnv via onReady
    await launch(rec);
  }

  // 2. Everyone else, in parallel, each with a snapshot of baseEnv (now carrying
  //    ANTHROPIC_BASE_URL if the proxy came up).
  const others = SERVICES.filter((s) => s.port !== PROXY_PORT);
  await Promise.all(
    others.map((def) => {
      const rec = recordFor(def);
      rec.env = { ...baseEnv };
      return launch(rec);
    }),
  );

  // 3. The core agent (Claude Code in tmux) — what the Core CLI pane attaches
  //    to and what runs tasks/crons. Fire-and-forget; tmux owns its lifecycle.
  launchCore(baseEnv);

  // Fresh Macs lack slack_bolt/discord.py, so the Slack + Discord bridges above
  // were skipped. Install them into a workspace venv in the background, then
  // bring those bridges up with no manual restart. No-op once populated.
  ensureBridgeDepsThenLaunch(baseEnv);

  // External core watchdog (recover-on-wedge + Slack alert). See startWatchdog.
  startWatchdog(baseEnv);

  const summary = status();
  console.log('[supervisor] supervisor started:', JSON.stringify({ count: summary.count, errors: summary.errors }));
  return summary;
}

async function stop() {
  started = false;
  stopWatchdog();
  await Promise.all([...state.values()].map((rec) => stopOne(rec)));
  await stopCore();
}

async function restart(host) {
  await stop();
  return superviseBackend(host || currentHost);
}

// Relaunch a single channel bridge (e.g. right after the user saves its token in
// Settings) so it re-gates on the now-present token + deps and comes up without
// a full "Restart services". Idempotent: stops any running instance first. No-op
// if the supervisor isn't managing or the channel has no bridge.
async function relaunchChannelBridge(channelId) {
  if (!started) return { ok: false, detail: 'supervisor not managing services' };
  const def = SERVICES.find((s) => s.daemon && s.channel === channelId);
  if (!def) return { ok: false, detail: `no bridge for channel "${channelId}"` };
  const rec = recordFor(def);
  await stopOne(rec);
  rec.env = currentHost ? buildBaseEnv(currentHost) : rec.env;
  const ok = await launch(rec);
  return { ok, state: rec.state, detail: rec.lastError || (ok ? 'running' : 'not started') };
}

// Bring the collector up or down to match the current cloud-auth state. Called by
// the sign-in / sign-out flow so usage forwarding follows the signed-in token
// WITHOUT a full "Restart services". The collector captures its metering env
// (endpoint + Bearer) at launch, so a sign-in after boot needs this relaunch to
// pick up the new token; on sign-out the gate skips it, so this stops it.
// Idempotent: stops any running instance first. No-op if the supervisor isn't
// managing services yet.
async function relaunchCollector() {
  if (!started) return { ok: false, detail: 'supervisor not managing services' };
  const def = SERVICES.find((s) => s.name === 'collector');
  if (!def) return { ok: false, detail: 'no collector service' };
  const rec = recordFor(def);
  await stopOne(rec);
  // Rebuild env so a fresh sign-in's token + apiBase land in the metering vars.
  rec.env = currentHost ? buildBaseEnv(currentHost) : rec.env;
  const ok = await launch(rec); // gate skips it (→ stopped) while signed out
  return { ok, state: rec.state, detail: rec.lastError || (ok ? 'running' : rec.state) };
}

// Relaunch the phone conversation-server in place (e.g. right after the user
// saves Twilio creds in Settings) so it re-gates on the now-present credentials
// and comes up without a full "Restart services". Idempotent: stops any running
// instance first. No-op if the supervisor isn't managing services yet.
async function relaunchConversationServer() {
  if (!started) return { ok: false, detail: 'supervisor not managing services' };
  const def = SERVICES.find((s) => s.name === 'conversation-server');
  if (!def) return { ok: false, detail: 'no conversation-server' };
  const rec = recordFor(def);
  await stopOne(rec);
  rec.env = currentHost ? buildBaseEnv(currentHost) : rec.env;
  const ok = await launch(rec);
  return { ok, state: rec.state, detail: rec.lastError || (ok ? 'running' : rec.state) };
}

// Restart ONLY the core agent (the sutando-core tmux session) in place — kill
// the session, then re-fire start-cli.sh. Used after a skill/config change so
// the core reloads WITHOUT a full app relaunch. The Core CLI pane auto-reconnects
// (terminal-server attaches to the fresh session). Voice/bridges are untouched.
// No-op if this instance isn't managing the core.
async function relaunchCore(host) {
  if (!started) return { ok: false, detail: 'core not managed by this instance' };
  if (process.env.SUTANDO_NO_CORE === '1') return { ok: false, detail: 'core disabled (SUTANDO_NO_CORE)' };
  const h = host || currentHost;
  if (!h) return { ok: false, detail: 'no host context' };
  await stopCore();
  launchCore(buildBaseEnv(h));
  return { ok: true, detail: 'core agent reloading' };
}

function status() {
  const services = [...state.values()].map((rec) => ({
    name: rec.def.name,
    port: rec.def.port,
    state: rec.state,
    pid: rec.child ? rec.child.pid : null,
    restarts: rec.restarts,
    lastError: rec.lastError,
  }));
  const up = services.filter((s) => s.state === 'ready' || s.state === 'adopted');
  // Gated-off optional services (a bridge with no token, screen-capture without
  // the Screen Recording grant) are 'skipped' — neither up nor an error, and
  // they shouldn't count against `expected` (so the UI reads e.g. 5/5).
  const skipped = services.filter((s) => s.state === 'skipped');
  const errors = services
    .filter((s) => s.state === 'failed')
    .map((s) => {
      const rec = state.get(s.name);
      const codeEvt = rec && rec.def.requires ? 'missing-env' : 'failed';
      return { name: s.name, code: codeEvt, detail: s.lastError };
    });
  return {
    managed: started,
    services,
    count: up.length,
    expected: SERVICES.length - skipped.length,
    labels: up.map((s) => s.name),
    skipped: skipped.map((s) => ({ name: s.name, detail: s.lastError })),
    errors,
  };
}

function onEvent(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export {
  superviseBackend,
  stop,
  restart,
  status,
  onEvent,
  relaunchChannelBridge,
  relaunchCollector,
  relaunchConversationServer,
  relaunchCore,
  coreSessionInfo,
  coreProcessRunning,
};
