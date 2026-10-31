// Worker-pool voice hook: a worker named by voice is resolved against the roster by the
// pool's own policy (resolve_worker_name.py), so the host never reads the roster itself.

import { execFile } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import type { SkillSetupCtx } from '../../src/skill-setup-runner.js';
import type { VoiceWorkerVerdict } from '../../src/task-bridge.js';
import { requirePython } from '../../src/python-binary.js';

export const tools: ToolDefinition[] = [];

export const RESOLVE_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'scripts', 'resolve_worker_name.py');
const RESOLVE_TIMEOUT_MS = 5_000;

type RawVerdict = { worker?: unknown; error?: unknown; known?: unknown };

/** The script's JSON verdict as the work tool takes it: the id, or a line for the user. */
export function verdictFromScript(spoken: string, stdout: string): VoiceWorkerVerdict {
	let raw: RawVerdict;
	try { raw = JSON.parse(stdout) as RawVerdict; } catch { raw = {}; }
	if (typeof raw.worker === 'string' && raw.worker) return { ok: true, worker: raw.worker };
	const known = Array.isArray(raw.known) ? raw.known.filter((k): k is string => typeof k === 'string') : [];
	const list = known.length ? ` The workers are: ${known.join(', ')}.` : '';
	const said = JSON.stringify(spoken);
	const head = raw.error === 'ambiguous' ? `${said} names more than one worker.`
		: raw.error === 'no-roster' ? `No workers are set up here, so there is no worker called ${said}.`
		: raw.error === 'unknown' ? `There is no worker called ${said}.`
		: `Could not check the worker name ${said}.`;
	return { ok: false, message: `${head}${list} Nothing was sent: tell the user and ask which one they meant.` };
}

export type RunScript = (python: string, args: string[]) => Promise<string>;

const runScript: RunScript = (python, args) => new Promise((resolve, reject) => {
	execFile(python, args, { timeout: RESOLVE_TIMEOUT_MS, encoding: 'utf8' }, (err, stdout) => (err ? reject(err) : resolve(stdout)));
});

/** Resolver for the host's seam. `workspace` is for tests; production uses the pool's own resolution. */
export function createWorkerNameResolver(opts: { workspace?: string; run?: RunScript; python?: () => string } = {}) {
	const run = opts.run ?? runScript;
	const python = opts.python ?? requirePython;
	return async (spoken: string): Promise<VoiceWorkerVerdict> => {
		const args = [RESOLVE_SCRIPT, ...(opts.workspace ? ['--workspace', opts.workspace] : []), '--', spoken];
		try {
			return verdictFromScript(spoken, await run(python(), args));
		} catch (e) {
			console.error(`[worker-pool] resolving ${JSON.stringify(spoken)} failed:`, e instanceof Error ? e.message : e);
			return verdictFromScript(spoken, '');
		}
	};
}

export function setup(ctx: SkillSetupCtx): void {
	if (typeof ctx?.setVoiceWorkerResolver !== 'function') {
		console.warn('[worker-pool] host has no worker-resolver seam — voice cannot name a worker');
		return;
	}
	ctx.setVoiceWorkerResolver(createWorkerNameResolver());
}
