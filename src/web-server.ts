/**
 * HTTP server for the Sutando desktop + remote browser conversation page.
 *
 * Endpoints:
 *   GET  /, /v2[/*]         — Vite-built React bundle (client/dist). The two
 *                             roots serve the same SPA so existing bookmarks
 *                             pointing at /v2 keep working. A fresh checkout
 *                             without `pnpm build:client` returns 503 with a
 *                             pointer to the build command (no longer falls
 *                             back to inline HTML — PR-C step 6 deleted it).
 *   GET  /sse               — Server-Sent Events: `agent-state`, `toggle-voice`,
 *                             `toggle-mute` — consumed by the page.
 *   GET  /sse-status        — JSON snapshot { muted, voiceConnected, state, label, clients }.
 *   GET  /voice-mode        — JSON { mode: 'active' | 'meeting' }.
 *   GET  /mute-state        — Read/write the browser + tool agent-state tracks.
 *                             Used by main.swift, voice-agent.ts tool hooks, and the page.
 *   GET  /toggle, /mute     — Broadcast SSE events (driven by menu-bar hotkeys).
 *   POST /note-viewing      — Write /tmp/sutando-note-viewing.json (consumed by the agent).
 *   GET  /paidsubscriptions/data  — subscription-scanner skill state JSON.
 *   POST /paidsubscriptions/scan  — queue an out-of-cycle scan (localhost-only).
 *   *    /vision/{state,start,stop,frame} — proxy to the voice-agent vision server.
 *
 * Lifecycle: started by voice-agent.ts in the same process — replaces the old
 * standalone `com.sutando.web-client` launchd service. One Node process owns
 * both the WebSocket (PORT) and the HTTP server (CLIENT_PORT).
 *
 * Architecture rule (CLAUDE.md § Where does new code belong?): this is a voice-
 * session concern (web client wiring), step 3 of the decision guide — lives in
 * core (`src/`), not under skills.
 */

import { createServer } from 'node:http';
import { writeFileSync, readFileSync, statSync, existsSync, mkdirSync, chmodSync } from 'node:fs';
import { extname, normalize, sep, join } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { readTmuxStatus } from './tmux-status.js';
import { statePath, statePathEnsured } from './state-paths.js';

// Dist directory for the React bundle (`client/`). Resolved once at module
// load — web-server.ts lives in `src/`, so `../client/dist` lands at the
// workspace root. PR-C step 6 retired the inline HTML fallback; `pnpm
// build:client` must run for `/` to render anything.
const CLIENT_DIST_DIR = fileURLToPath(new URL('../client/dist/', import.meta.url));

// Repo root — web-server.ts lives in `src/`, so `../` is the checkout root.
// The subscription-scanner skill's state file lives in the repo tree
// (gitignored — personal financial data), populated by the agent on a scan.
const REPO_DIR = fileURLToPath(new URL('../', import.meta.url));
const SUBSCRIPTIONS_FILE = join(
	REPO_DIR,
	'skills',
	'subscription-scanner',
	'state',
	'subscriptions.json'
);

// Slack bridge credential file — shared convention with Discord/Telegram.
const SLACK_ENV_PATH = join(homedir(), '.claude', 'channels', 'slack', '.env');

const STATIC_MIME_TYPES: Record<string, string> = {
	'.html': 'text/html; charset=utf-8',
	'.js': 'application/javascript; charset=utf-8',
	'.mjs': 'application/javascript; charset=utf-8',
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
};

/**
 * Resolve a request path under `/` or `/v2/` to a file inside `client/dist/`.
 * Returns null when the path escapes the dist root (defense against
 * `/../etc/passwd` style traversal) or when the file is missing — either
 * case falls through to a 404 in the caller.
 */
function resolveDistFile(relPathRaw: string): string | null {
	const relPath = relPathRaw.replace(/^\/+/, '') || 'index.html';
	const normalized = normalize(relPath);
	if (normalized.startsWith('..') || normalized.includes(`..${sep}`)) return null;
	const abs = CLIENT_DIST_DIR + normalized;
	try {
		const stat = statSync(abs);
		if (!stat.isFile()) return null;
		return abs;
	} catch {
		return null;
	}
}

/**
 * Serve a file from `client/dist/`. Returns true when the response was
 * written, false when the file wasn't found (caller decides the fallback).
 *
 * `spaFallback` (default true) controls the "file missing → index.html"
 * behavior. Disable it for asset-shaped requests (e.g. /assets/*.js) where
 * silently substituting index.html would hand the browser HTML for what it
 * thinks is JS — the bug behind the PR-C-step-5 blank page.
 */
function serveDistFile(
	rel: string,
	res: import('node:http').ServerResponse,
	opts: { spaFallback?: boolean } = {}
): boolean {
	const { spaFallback = true } = opts;
	const file = spaFallback
		? (resolveDistFile(rel) ?? resolveDistFile('index.html'))
		: resolveDistFile(rel);
	if (!file) return false;
	const mime = STATIC_MIME_TYPES[extname(file).toLowerCase()] ?? 'application/octet-stream';
	try {
		const body = readFileSync(file);
		res.writeHead(200, {
			'Content-Type': mime,
			// Hashed asset filenames from Vite are safe to cache long-term;
			// index.html must always be fresh so SPA route changes ship.
			'Cache-Control': file.endsWith('index.html')
				? 'no-cache, no-store, must-revalidate'
				: 'public, max-age=31536000, immutable',
		});
		res.end(body);
		return true;
	} catch {
		res.writeHead(500);
		res.end('Failed to read static asset');
		return true;
	}
}

export interface WebServerOptions {
	/** Port for the HTTP server (default: 8080, matches the legacy CLIENT_PORT env). */
	port: number;
	/** Bind address (default: '0.0.0.0' — accept remote browser connections). */
	host: string;
	/** WebSocket port that the served page connects back to (default: voice-agent's PORT, 9900). */
	wsPort: number;
}

/**
 * Start the HTTP server on `opts.host:opts.port`. Returns the underlying
 * `http.Server` so callers can close() it during teardown (used by the
 * agent-state-endpoint test in `tests/`).
 *
 * Side effect: spins up an SSE heartbeat interval (30s) and an internal
 * setTimeout per /mute-state?state=seeing call. Calling startWebServer more
 * than once in the same process is unsupported.
 */
export function startWebServer(opts: WebServerOptions): import('node:http').Server {
	const HTTP_PORT = opts.port;
	const HTTP_HOST = opts.host;
	const WS_PORT = opts.wsPort;

	// SSE clients for remote toggle
	const sseClients: import('node:http').ServerResponse[] = [];
	// Server-side state tracking for menu bar indicator
	let _muteState = false;
	let _voiceState = false;
	// Semantic agent state. Two independent tracks:
	//   - _browserState: what the browser derives from local signals
	//     (connected+unmuted → listening, audio RMS → speaking, disconnected → idle).
	//     Refreshed ~1x/second by reportAgentState in the page.
	//   - _toolState: set by server-side tool code (voice-agent onToolCall →
	//     'working', screen-capture → 'seeing'). Only tool code writes this.
	// Effective state (returned by /sse-status + broadcast via SSE) is the
	// tool track when non-idle, else the browser track. This prevents the
	// browser's 1s poll from overwriting a tool-originated 'working' back
	// to 'listening' — the bug Chi hit after the SSE bridge shipped.
	type AgentState = 'idle' | 'listening' | 'speaking' | 'working' | 'seeing';
	let _browserState: AgentState = 'idle';
	let _toolState: AgentState = 'idle';
	// Optional label for the tool track, e.g. the specific tool name
	// ('describe_screen') or core-status step. Surfaced by /sse-status so
	// the menu-bar tooltip can say "running describe_screen" instead of
	// the generic "running a tool".
	let _toolLabel: string = '';
	// Saves the tool-track state at the moment seeing is set, so after the
	// seeing TTL expires we revert to whatever tool was running BEFORE the
	// capture — most commonly 'working'. Previously seeing → idle, which
	// killed the working pulse mid-tool and made Chi think seeing "happened
	// long after" (in fact, the next working state was the next tool call,
	// which registers as a new pulse well after seeing cleared the first).
	let _preSeeingToolState: AgentState = 'idle';
	// Timestamp of the last transition INTO 'seeing'. Screen capture is transient
	// (sub-second), so we want the 'seeing' state to flash briefly then auto-
	// revert. Without auto-revert, a single /capture call pins state=seeing
	// forever if nothing else POSTs.
	let _seeingUntil = 0;

	// Read `core-status.json` (written by the proactive loop + Claude Code passes)
	// to surface core work as a `working` state when no other track is active.
	// Without this, the menu bar stays solid while Claude Code is processing a
	// Discord/voice task even though the user would want to see that signal.
	// Lightweight: just a file read (one syscall) per /sse-status poll every 3s.
	// Read core-status.json and return { running, step, stale }.
	// - `running`: CLI is mid-pass (status == "running" and ts within grace).
	// - `step`: tooltip label when no tool label is set.
	// - `stale`: the file is unreliable — either older than 60s on disk, or
	//   status=="running" with ts older than 60s (a proactive-loop pass that
	//   crashed between step 0 write and idle write leaves a "running" sentinel
	//   that mtime moves but content lies). When stale, consumers should fall
	//   back to the tmux pane scrape for a fresh signal.
	const CORE_STATUS_STALE_SECONDS = 60;
	function readCoreStatus(): { running: boolean; step: string; stale: boolean } {
		try {
			const statusFile = statePath('core-status.json');
			const raw = readFileSync(statusFile, 'utf-8');
			const s = JSON.parse(raw) as { status?: string; ts?: number; step?: string };
			const nowSec = Date.now() / 1000;
			let stale = false;
			try {
				const mtimeSec = statSync(statusFile).mtimeMs / 1000;
				if (nowSec - mtimeSec > CORE_STATUS_STALE_SECONDS) stale = true;
			} catch { stale = true; }
			// "Running with old ts" → loop likely crashed mid-pass, treat as stale.
			if (s.status === 'running' && typeof s.ts === 'number' && nowSec - s.ts > CORE_STATUS_STALE_SECONDS) {
				stale = true;
			}
			if (s.status !== 'running') return { running: false, step: '', stale };
			if (typeof s.ts === 'number' && nowSec - s.ts > 600) return { running: false, step: '', stale };
			return { running: true, step: typeof s.step === 'string' ? s.step : '', stale };
		} catch {
			return { running: false, step: '', stale: true };
		}
	}
	function coreIsRunning(): boolean { return readCoreStatus().running; }

	const VOICE_STATE_STALE_SECONDS = 120;
	function readVoiceState(): boolean | null {
		try {
			const raw = readFileSync(statePath('voice-state.json'), 'utf-8');
			const s = JSON.parse(raw) as { connected?: boolean; ts?: number };
			const nowSec = Date.now() / 1000;
			if (typeof s.ts === 'number' && nowSec - s.ts > VOICE_STATE_STALE_SECONDS && s.connected) {
				return null;
			}
			return typeof s.connected === 'boolean' ? s.connected : null;
		} catch {
			return null;
		}
	}

	function effectiveAgentState(): AgentState {
		if (_toolState === 'seeing' && Date.now() > _seeingUntil) {
			// Revert to pre-seeing tool state (usually 'working' if a tool was
			// running when the capture fired). Falling straight to 'idle' here
			// would kill the working pulse mid-tool.
			_toolState = _preSeeingToolState;
			_preSeeingToolState = 'idle';
		}
		if (_toolState !== 'idle') return _toolState;
		// Core-agent (Claude Code proactive-loop / task pass) running beats the
		// browser track — if core is actively doing work, that's the truer state
		// than "user is currently speaking". Chi's 2026-04-19 ask: "when working
		// and listening at the same time, working should be the state". Previously
		// _browserState short-circuited here and the core track only ran when the
		// user was silent, so core-work during an active turn never surfaced.
		const core = readCoreStatus();
		if (core.running) return 'working';
		// Core is idle OR the file is stale. If stale, ask the tmux scrape for a
		// hint — useful when a pass crashed before writing idle, or when the CLI
		// is actively doing something but forgot to write. Fresh-idle file wins
		// over tmux to prevent the scrape from spuriously lighting up the pulse.
		if (core.stale) {
			const scrape = readTmuxStatus();
			if (scrape.state === 'working') return 'working';
		}
		if (_browserState !== 'idle') return _browserState;
		return 'idle';
	}

	// Heartbeat: ping every 30s, remove clients that fail to write (stale connections).
	// .unref() so a lone heartbeat doesn't block Node from exiting when the
	// caller has closed the server (notably the agent-state-endpoint test —
	// pre-PR-A this lived in a subprocess that got SIGKILL'd, post-PR-A the
	// in-process timer would otherwise pin the test runner for 30s+).
	const heartbeat = setInterval(() => {
		for (let i = sseClients.length - 1; i >= 0; i--) {
			try {
				sseClients[i].write(':\n\n'); // SSE comment = keep-alive ping
			} catch {
				sseClients.splice(i, 1);
			}
		}
	}, 30_000);
	heartbeat.unref();

	const server = createServer((req, res) => {
		const url = new URL(req.url || '/', `http://${req.headers.host}`);

		if (url.pathname === '/sse') {
			res.writeHead(200, {
				'Content-Type': 'text/event-stream',
				'Cache-Control': 'no-cache',
				'Connection': 'keep-alive',
				'Access-Control-Allow-Origin': '*',
			});
			res.write(':\n\n'); // heartbeat
			// Send current agent state immediately so freshly-connected clients
			// don't display stale DOM classes from the previous session.
			// Before this, a browser that reconnected SSE after a server
			// restart kept whatever .working/.seeing class it had when the
			// previous connection dropped — producing the "web UI shows working
			// but menu bar shows listening" inconsistency Chi hit today.
			try {
				res.write(`event: agent-state\ndata: ${effectiveAgentState()}\n\n`);
			} catch {}
			sseClients.push(res);
			req.on('close', () => {
				const idx = sseClients.indexOf(res);
				if (idx >= 0) sseClients.splice(idx, 1);
			});
			return;
		}

		// SSE client count + mute/voice/agent state (safe for diagnostics + menu bar indicator)
		if (url.pathname === '/sse-status') {
			const eff = effectiveAgentState();
			// Derive label: tool-track → _toolLabel; core-fallback → step from
			// core-status.json. Empty otherwise. Swift menu-bar tooltip uses
			// this for precision (e.g. "running describe_screen" vs generic
			// "running a tool"). Per Chi's ask 2026-04-18: "running a tool is
			// not precise."
			let label = '';
			if (_toolState !== 'idle') {
				label = _toolLabel;
			} else if (eff === 'working') {
				// Core-agent working — surface the step label regardless of
				// whether the user's mic is hot. Previously gated on
				// `_browserState === 'idle'`, which meant the tooltip stayed
				// generic ("running a tool") whenever the voice tab was open.
				// After PR #465 flipped the precedence (core beats browser),
				// this gate became stale — keep the label in sync with the
				// state it describes.
				const core = readCoreStatus();
				label = core.step;
				// If core is stale and we're in fallback territory, prefer the
				// tmux-scrape label (usually a tool name) over the stale step.
				if (core.stale && !core.step) {
					const scrape = readTmuxStatus();
					if (scrape.state === 'working') label = scrape.label;
				}
			}
			// voice-state.json (written by voice-agent on connect/disconnect) is
			// authoritative. Fall back to the browser-reported _voiceState cache
			// if the file is missing or stale (see readVoiceState doc).
			const vs = readVoiceState();
			const voiceConnected = vs !== null ? vs : _voiceState;
			res.writeHead(200, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({
				clients: sseClients.length,
				muted: _muteState,
				voiceConnected,
				state: eff,
				label,
			}));
			return;
		}

		// Voice-agent mode sentinel (state/voice-mode.txt written by voice-agent
		// on switch_mode / zoom-auto-flip). Returns "active" or "meeting".
		// Falls back to "active" if the file is missing. Combined with the
		// presenter badge poll to render the composite 3-mode badge in the UI.
		if (url.pathname === '/voice-mode') {
			let mode = 'active';
			try {
				const raw = readFileSync(statePath('state/voice-mode.txt'), 'utf-8').trim();
				if (raw === 'meeting' || raw === 'active') mode = raw;
			} catch {}
			res.writeHead(200, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({ mode }));
			return;
		}

		// Mute + voice + agent state report. `source=tool` writes the tool
		// track (working/seeing) and takes precedence; everything else
		// writes the browser track (idle/listening/speaking).
		if (url.pathname === '/mute-state') {
			const mState = url.searchParams.get('muted');
			const vState = url.searchParams.get('voice');
			const aState = url.searchParams.get('state');
			const source = url.searchParams.get('source'); // 'tool' | null (browser)
			if (mState !== null) _muteState = mState === 'true';
			if (vState !== null) _voiceState = vState === 'true';
			if (aState === 'idle' || aState === 'listening' || aState === 'speaking' || aState === 'working' || aState === 'seeing') {
				const prevEffective = effectiveAgentState();
				if (source === 'tool') {
					const labelParam = url.searchParams.get('label');
					if (aState === 'seeing') {
						// Remember the tool state before overlaying seeing — most
						// often 'working' when a describe_screen tool is in flight.
						// Without this, the post-TTL revert would drop to idle
						// mid-tool and kill the working pulse, which is what Chi
						// hit ("fast blinking for seeing happened long after").
						if (_toolState !== 'seeing') _preSeeingToolState = _toolState;
						_toolState = 'seeing';
						if (labelParam) _toolLabel = labelParam;
						const ttlParam = url.searchParams.get('ttl_ms');
						const ttl = ttlParam ? parseInt(ttlParam, 10) : 3000;
						// Upper-bound via RelationalComparison, which CodeQL's
						// js/resource-exhaustion recognizes as UpperBoundsCheckSanitizerGuard.
						// #489 used `Math.min(ttl, MAX)`, which CodeQL treats as a
						// numeric passthrough (isNumericFlowStep) and therefore does
						// NOT close alert #43. `ttl <= MAX ? ttl : MAX` is the
						// equivalent clamp expressed as a relational guard.
						const MAX_TTL_MS = 60000;
						const ttlMs = (isFinite(ttl) && ttl > 0)
							? (ttl <= MAX_TTL_MS ? ttl : MAX_TTL_MS)
							: 3000;
						_seeingUntil = Date.now() + ttlMs;
						// Schedule an auto-revert broadcast so the browser clears
						// .seeing even if nothing else POSTs a state update.
						setTimeout(() => {
							if (_toolState === 'seeing' && Date.now() >= _seeingUntil) {
								_toolState = _preSeeingToolState;
								_preSeeingToolState = 'idle';
								const eff = effectiveAgentState();
								for (const client of sseClients) {
									try { client.write(`event: agent-state\ndata: ${eff}\n\n`); } catch {}
								}
							}
						}, ttlMs + 50);
					} else {
						// working or clear. Reset the pre-seeing memory too so a
						// future seeing knows its true predecessor, not the stale
						// one from a previous tool sequence.
						_toolState = aState === 'working' ? 'working' : 'idle';
						_preSeeingToolState = 'idle';
						if (aState === 'working' && labelParam) _toolLabel = labelParam;
						else if (aState !== 'working') _toolLabel = '';
					}
				} else {
					// Browser can't legitimately know working/seeing — those
					// originate server-side. Clamp to listening if mislabeled
					// so a confused browser can't trample the tool track.
					_browserState = (aState === 'working' || aState === 'seeing') ? 'listening' : aState;
				}
				const nextEffective = effectiveAgentState();
				if (prevEffective !== nextEffective) {
					for (const client of sseClients) {
						try { client.write(`event: agent-state\ndata: ${nextEffective}\n\n`); } catch {}
					}
				}
			}
			res.writeHead(200, { 'Content-Type': 'application/json' });
			const vs2 = readVoiceState();
			res.end(JSON.stringify({ muted: _muteState, voiceConnected: vs2 !== null ? vs2 : _voiceState, state: effectiveAgentState() }));
			return;
		}

		if (url.pathname === '/toggle' || url.pathname === '/mute') {
			const event = url.pathname === '/toggle' ? 'toggle-voice' : 'toggle-mute';
			for (const client of sseClients) {
				client.write(`event: ${event}\ndata: 1\n\n`);
			}
			res.writeHead(200, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({ ok: true, event, clients: sseClients.length }));
			return;
		}

		// Note view event from the in-page note reader. Writes the current slug +
		// content to /tmp/sutando-note-viewing.json; the voice-agent's
		// startNoteViewingWatcher picks it up and injects into Gemini so the
		// assistant can answer questions about whatever the user is looking at.
		if (url.pathname === '/note-viewing' && req.method === 'POST') {
			const chunks: Buffer[] = [];
			req.on('data', (c: Buffer) => chunks.push(c));
			req.on('end', () => {
				try {
					const body = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
					if (!body.slug || typeof body.content !== 'string') {
						res.writeHead(400, { 'Content-Type': 'application/json' });
						res.end(JSON.stringify({ error: 'slug and content required' }));
						return;
					}
					const event = { slug: body.slug, content: body.content, ts: new Date().toISOString() };
					writeFileSync('/tmp/sutando-note-viewing.json', JSON.stringify(event));
					res.writeHead(200, { 'Content-Type': 'application/json' });
					res.end(JSON.stringify({ ok: true }));
				} catch (e) {
					res.writeHead(400, { 'Content-Type': 'application/json' });
					res.end(JSON.stringify({ error: e instanceof Error ? e.message : 'parse failed' }));
				}
			});
			return;
		}

		// Slack bridge credentials. Mirrors the Discord/Telegram convention —
		// tokens live in `~/.claude/channels/slack/.env`, not the repo .env.
		// GET reports only whether each token is set (never echoes the value);
		// POST writes the file 0600. slack-bridge.py loads this file on start.
		if (url.pathname === '/settings/slack' && req.method === 'GET') {
			let botConfigured = false;
			let appConfigured = false;
			try {
				if (existsSync(SLACK_ENV_PATH)) {
					const env = readFileSync(SLACK_ENV_PATH, 'utf-8');
					botConfigured = /^SLACK_BOT_TOKEN=.+/m.test(env);
					appConfigured = /^SLACK_APP_TOKEN=.+/m.test(env);
				}
			} catch { /* report not-configured on any read error */ }
			res.writeHead(200, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({ botConfigured, appConfigured }));
			return;
		}

		if (url.pathname === '/settings/slack' && req.method === 'POST') {
			const chunks: Buffer[] = [];
			req.on('data', (c: Buffer) => chunks.push(c));
			req.on('end', () => {
				try {
					const body = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
					const botToken = typeof body.botToken === 'string' ? body.botToken.trim() : '';
					const appToken = typeof body.appToken === 'string' ? body.appToken.trim() : '';
					if (!botToken.startsWith('xoxb-') || !appToken.startsWith('xapp-')) {
						res.writeHead(400, { 'Content-Type': 'application/json' });
						res.end(JSON.stringify({ error: 'Bot token must start with "xoxb-" and app token with "xapp-".' }));
						return;
					}
					mkdirSync(join(homedir(), '.claude', 'channels', 'slack'), { recursive: true });
					writeFileSync(SLACK_ENV_PATH, `SLACK_BOT_TOKEN=${botToken}\nSLACK_APP_TOKEN=${appToken}\n`);
					chmodSync(SLACK_ENV_PATH, 0o600);
					res.writeHead(200, { 'Content-Type': 'application/json' });
					res.end(JSON.stringify({ ok: true }));
				} catch (e) {
					res.writeHead(400, { 'Content-Type': 'application/json' });
					res.end(JSON.stringify({ error: e instanceof Error ? e.message : 'parse failed' }));
				}
			});
			return;
		}

		// Paid-subscriptions dashboard data. Reads the subscription-scanner
		// skill's state file (gitignored — populated by the agent during a
		// scan). Returns an empty shape when the file is absent so the client
		// renders a clean "no scan yet" state instead of erroring.
		if (url.pathname === '/paidsubscriptions/data' && req.method === 'GET') {
			let data: unknown = { last_scan: null, subscriptions: [], scan_history: [] };
			try {
				data = JSON.parse(readFileSync(SUBSCRIPTIONS_FILE, 'utf-8'));
			} catch { /* missing or unparseable → default empty shape */ }
			res.writeHead(200, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify(data));
			return;
		}

		// Queue an out-of-cycle subscription scan. LOCALHOST-ONLY: the server
		// binds 0.0.0.0, so without this gate anyone on the LAN (or a
		// tailscale-funnel'd public URL) could enqueue an owner-tier task that
		// the watcher runs with full agent privileges. Check the socket peer
		// address directly — X-Forwarded-For is spoofable behind any proxy.
		// The task body points at scan-prompt.md by path rather than inlining
		// it, so a line in that file shaped like a task header (`source:`,
		// `access_tier:`) can never be parsed as a real header.
		if (url.pathname === '/paidsubscriptions/scan' && req.method === 'POST') {
			const peer = req.socket.remoteAddress || '';
			const isLocal =
				peer === '127.0.0.1' || peer === '::1' || peer === '::ffff:127.0.0.1';
			if (!isLocal) {
				res.writeHead(403, { 'Content-Type': 'application/json' });
				res.end(JSON.stringify({ error: 'Scan can only be triggered from localhost.' }));
				return;
			}
			try {
				const ts = Math.floor(Date.now() / 1000);
				const id = `task-subscan-${ts}`;
				const body =
					[
						`id: ${id}`,
						`timestamp: ${new Date().toISOString()}`,
						'task: Run an out-of-cycle paid-subscription scan. Read skills/subscription-scanner/scan-prompt.md and follow its instructions verbatim — update skills/subscription-scanner/state/subscriptions.json, snapshot history, and notify only on changes.',
						'source: web',
						'access_tier: owner',
					].join('\n') + '\n';
				writeFileSync(statePathEnsured(`tasks/${id}.txt`), body);
				res.writeHead(200, { 'Content-Type': 'application/json' });
				res.end(JSON.stringify({ ok: true, taskId: id }));
			} catch (e) {
				res.writeHead(500, { 'Content-Type': 'application/json' });
				res.end(JSON.stringify({ error: e instanceof Error ? e.message : 'failed to queue scan' }));
			}
			return;
		}

		// Vision control proxy. The voice-agent process exposes
		// /vision/{state,start,stop,frame} on 127.0.0.1:VISION_CONTROL_PORT
		// (default 7848); the browser hits us same-origin to avoid CORS and
		// keep one public surface. /vision/frame carries a binary JPEG body —
		// preserve the content-type and pass the buffer straight through.
		// When voice-agent isn't reachable, surface a synthetic state so the
		// Watch button can't get stuck mid-toggle.
		if (
			url.pathname === '/vision/state' ||
			url.pathname === '/vision/start' ||
			url.pathname === '/vision/stop' ||
			url.pathname === '/vision/frame'
		) {
			const visionPort = Number(process.env.VISION_CONTROL_PORT) || 7848;
			const method = req.method === 'POST' ? 'POST' : 'GET';
			const isFrame = url.pathname === '/vision/frame';
			const visionPath = url.pathname;
			const chunks: Buffer[] = [];
			req.on('data', (c: Buffer) => chunks.push(c));
			req.on('end', async () => {
				try {
					const incomingType =
						(req.headers['content-type'] as string | undefined) ||
						(isFrame ? 'image/jpeg' : 'application/json');
					const upstream = await fetch(`http://127.0.0.1:${visionPort}${visionPath}`, {
						method,
						headers: method === 'POST' ? { 'Content-Type': incomingType } : undefined,
						body:
							method === 'POST'
								? chunks.length
									? Buffer.concat(chunks)
									: isFrame
										? Buffer.alloc(0)
										: '{}'
								: undefined,
					});
					const text = await upstream.text();
					res.writeHead(upstream.status, { 'Content-Type': 'application/json' });
					res.end(text);
				} catch {
					const fallback =
						visionPath === '/vision/state'
							? { streaming: false, source: null, fps: 0, frames: 0, durationMs: 0, sessionReady: false }
							: { status: 'failed', error: 'voice-agent not reachable' };
					res.writeHead(visionPath === '/vision/state' ? 200 : 503, {
						'Content-Type': 'application/json',
					});
					res.end(JSON.stringify(fallback));
				}
			});
			return;
		}

		// Static asset + SPA fallback. Vite's index.html references hashed
		// assets via relative paths (./assets/index-XXXX.js). When the
		// browser resolves those against /, the request comes in as
		// /assets/index-XXXX.js — which is neither / nor /v2 nor any of
		// the API endpoints above. We need to:
		//   1. Serve the exact file from client/dist/ when it exists
		//      (e.g. /assets/*.js, /favicon.ico).
		//   2. Fall back to index.html only for extension-less paths —
		//      that's the SPA-routing case (deep links like /settings).
		//   3. 404 asset misses (paths with an extension). Returning
		//      index.html for a missing .js file produces "Unexpected
		//      token '<'" in the console and a blank page — the exact
		//      symptom we hit after PR-C step 5.
		//   4. Return a 503 with build hint when client/dist/index.html
		//      itself is missing (fresh checkout, no `pnpm build:client`).
		const isV2Path = url.pathname === '/v2' || url.pathname.startsWith('/v2/');
		let rel: string;
		if (isV2Path) {
			rel = url.pathname === '/v2' ? 'index.html' : url.pathname.slice('/v2/'.length);
		} else if (url.pathname === '/') {
			rel = 'index.html';
		} else {
			rel = url.pathname.replace(/^\/+/, '');
		}

		if (serveDistFile(rel, res, { spaFallback: false })) return;

		const looksLikeAsset = !!extname(rel) && rel !== 'index.html';
		if (looksLikeAsset) {
			res.writeHead(404, { 'Content-Type': 'text/plain' });
			res.end('Not found');
			return;
		}

		if (serveDistFile('index.html', res, { spaFallback: false })) return;
		res.writeHead(503, {
			'Content-Type': 'text/html; charset=utf-8',
			'Cache-Control': 'no-cache',
		});
		res.end(
			`<!doctype html><meta charset="utf-8"><title>Sutando — build required</title>` +
				`<style>body{font-family:-apple-system,sans-serif;max-width:560px;margin:80px auto;padding:0 20px;color:#222}</style>` +
				`<h1>Sutando client not built</h1>` +
				`<p>Run <code>pnpm install && pnpm build:client</code> from the repo root, then refresh this page.</p>`
		);
	});

	server.listen(HTTP_PORT, HTTP_HOST, () => {
		const serverUrl = HTTP_HOST === '0.0.0.0' 
			? `http://localhost:${HTTP_PORT} (or use your server's IP/DNS)`
			: `http://${HTTP_HOST}:${HTTP_PORT}`;
		console.log(`\n  Sutando — Web Client`);
		console.log(`  ────────────────────────────────`);
		console.log(`  Open in browser:  ${serverUrl}`);
		console.log(`  WebSocket URL:    Auto-detected from browser hostname`);
		console.log(`  WebSocket port:  ${WS_PORT}`);
		console.log(`\n  Press Ctrl+C to stop.\n`);
	});

	return server;
}
