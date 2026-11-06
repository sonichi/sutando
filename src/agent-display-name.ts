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
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolvePython } from './python-binary.js';

export const FALLBACK_AGENT_NAME = 'Sutando';
const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'agent_display_name.py');
const OFFLINE_TIMEOUT_MS = 5_000;
const LIVE_TIMEOUT_MS = 20_000;
export const LIVE_REFRESH_MS = 10 * 60_000;

/** How the resolver CLI is run; tests inject one. `sync` returns stdout or throws. */
export interface NameRunner {
	sync(args: string[]): string;
	async(args: string[], done: (stdout: string | null) => void): void;
}

const defaultRunner: NameRunner = {
	sync(args) {
		const py = resolvePython();
		if (py === null) throw new Error('python unavailable');
		return execFileSync(py, [SCRIPT, ...args], { timeout: OFFLINE_TIMEOUT_MS, stdio: ['ignore', 'pipe', 'ignore'] }).toString();
	},
	async(args, done) {
		const py = resolvePython();
		if (py === null) { done(null); return; }
		execFile(py, [SCRIPT, ...args], { timeout: LIVE_TIMEOUT_MS }, (err, stdout) => done(err ? null : String(stdout)));
	},
};

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
}
