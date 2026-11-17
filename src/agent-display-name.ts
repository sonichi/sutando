/**
 * The name the voice and phone prompts introduce the agent by.
 *
 * src/agent_display_name.py owns the precedence (live s.space display name first, never a persona
 * name); this module only runs it and caches the answer. A prompt build never waits on the
 * homeserver: the first call resolves with `--offline`, and a live resolve runs in the background
 * (again at most every LIVE_REFRESH_MS), so later builds pick up the live name and any rename.
 * When python or the resolver is unavailable, the name is the resolver's own fallback, "Sutando".
 */
import { execFile, execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolvePython } from './python-binary.js';
import { repoSrcScriptPath } from './sutando_config.js';

export const FALLBACK_AGENT_NAME = 'Sutando';
/** The resolver script for a module running from `moduleDir` (src/ in dev, dist/ when bundled). */
export function agentDisplayNameScriptPath(moduleDir: string = dirname(fileURLToPath(import.meta.url))): string {
	return repoSrcScriptPath('agent_display_name.py', moduleDir);
}
const SCRIPT = agentDisplayNameScriptPath();
const OFFLINE_TIMEOUT_MS = 5_000;
const LIVE_TIMEOUT_MS = 20_000;
export const LIVE_REFRESH_MS = 10 * 60_000;

/** How the resolver CLI is run; tests inject one. `sync` returns stdout or throws. */
export interface NameRunner {
	sync(args: string[]): string;
	async(args: string[], done: (stdout: string | null) => void): void;
}

const warned = new Set<string>();

/** Warn once per process per distinct cause; causes carry no env values or resolver output. */
function warnOnce(cause: string, sink: (msg: string) => void): void {
	if (warned.has(cause)) return;
	warned.add(cause);
	sink(`[agent-display-name] ${cause}; using the last known name or "${FALLBACK_AGENT_NAME}"`);
}

function failureCause(err: unknown): string {
	const e = err as { code?: unknown; signal?: unknown; status?: unknown } | null;
	if (e?.code === 'ETIMEDOUT' || e?.signal) return 'resolver timed out';
	if (typeof e?.status === 'number') return `resolver exited with status ${e.status}`;
	if (typeof e?.code === 'number') return `resolver exited with status ${e.code}`;
	return typeof e?.code === 'string' ? `resolver failed to start (${e.code})` : 'resolver failed';
}

/** The production runner for `script`; warnings go to `sink`. Exported for tests. */
export function resolverRunner(script: string = SCRIPT, sink: (msg: string) => void = console.warn): NameRunner {
	const python = (): string | null => {
		if (!existsSync(script)) { warnOnce(`resolver script not found at ${script}`, sink); return null; }
		const py = resolvePython();
		if (py === null) warnOnce('python unavailable', sink);
		return py;
	};
	return {
		sync(args) {
			const py = python();
			if (py === null) throw new Error('resolver unavailable');
			try {
				return execFileSync(py, [script, ...args], { timeout: OFFLINE_TIMEOUT_MS, stdio: ['ignore', 'pipe', 'ignore'] }).toString();
			} catch (err) { warnOnce(failureCause(err), sink); throw err; }
		},
		async(args, done) {
			const py = python();
			if (py === null) { done(null); return; }
			execFile(py, [script, ...args], { timeout: LIVE_TIMEOUT_MS }, (err, stdout) => {
				if (err) warnOnce(failureCause(err), sink);
				done(err ? null : String(stdout));
			});
		},
	};
}

const defaultRunner = resolverRunner();

/** The `name` of the resolver's `--json` line, or '' for anything else. */
export function nameFromJson(stdout: string | null): string {
	try {
		const name = JSON.parse((stdout ?? '').trim()).name;
		return typeof name === 'string' ? name.trim() : '';
	} catch { return ''; }
}

let cached: string | undefined;
let liveStartedAt = -Infinity;

export function agentDisplayName(runner: NameRunner = defaultRunner, now: number = Date.now()): string {
	if (now - liveStartedAt >= LIVE_REFRESH_MS) {
		liveStartedAt = now;
		try {
			runner.async(['--json'], (stdout) => { const name = nameFromJson(stdout); if (name) cached = name; });
		} catch { /* the offline answer stands */ }
	}
	if (cached === undefined) {
		let name = '';
		try { name = nameFromJson(runner.sync(['--offline', '--json'])); } catch { /* fallback below */ }
		cached = name || FALLBACK_AGENT_NAME;
	}
	return cached;
}

/** Tests only: forget the cached name and the last live resolve. */
export function resetAgentDisplayNameForTests(): void {
	cached = undefined;
	liveStartedAt = -Infinity;
	warned.clear();
}
