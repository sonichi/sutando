/**
 * Canonical workspace-directory resolution for Sutando TS services.
 *
 * All runtime artifacts (tasks/, results/, state/, data/, notes/, ...) live
 * under the workspace dir. Callers MUST use resolveWorkspace() rather than
 * computing paths relative to import.meta.url or process.cwd() — the latter
 * breaks when invoked from a bundle/launchd/symlink install where those
 * anchors resolve into the app package rather than the user workspace.
 *
 * Twin of src/workspace_default.py — no migration logic here. The Python
 * services run first (via startup.sh) and handle the one-time dir-move from
 * any legacy repo-root install. TS callers rely on that having already run.
 *
 * Resolution order:
 *   1. $SUTANDO_WORKSPACE env var (~ expanded).
 *   2. ~/.sutando/workspace/
 */

import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, parse } from 'node:path';
import { fileURLToPath } from 'node:url';

let _fallbackWarnPrinted = false;

/**
 * Best-effort: read `SUTANDO_WORKSPACE=` from the repo's .env file.
 *
 * Walks up from this module's resolved path to find the nearest `.env`,
 * then scans for a `SUTANDO_WORKSPACE=` line. Returns the (tilde-expanded)
 * value or undefined on any failure — never throws. Used only to enrich
 * the fallback-warn message below; resolution itself does NOT consume
 * this value, so a user who genuinely wants the default still gets it.
 */
function grepEnvForWorkspace(): string | undefined {
	try {
		let cur = fileURLToPath(import.meta.url);
		for (let i = 0; i < 5; i++) {
			cur = dirname(cur);
			if (cur === parse(cur).root) return undefined;
			const envFile = join(cur, '.env');
			if (existsSync(envFile)) {
				for (const line of readFileSync(envFile, 'utf8').split('\n')) {
					const s = line.trim();
					if (s.startsWith('SUTANDO_WORKSPACE=')) {
						let val = s.split('=').slice(1).join('=').trim();
						if (val.length >= 2 && val[0] === val[val.length - 1] && (val[0] === '"' || val[0] === "'")) {
							val = val.slice(1, -1);
						}
						return val ? val.replace(/^~/, homedir()) : undefined;
					}
				}
				return undefined;
			}
		}
	} catch {
		// Best-effort; never block resolution on .env probe failures.
	}
	return undefined;
}

export function resolveWorkspace(): string {
	const env = process.env.SUTANDO_WORKSPACE?.trim();
	if (env) return env.replace(/^~/, homedir());
	const fallback = join(homedir(), '.sutando', 'workspace');
	// Surface the silent-fallback bug class (see PR #1367/#1368): if .env
	// defines SUTANDO_WORKSPACE but the process never got it (e.g. a service
	// started outside `bash src/startup.sh`), the caller silently lands in
	// the default while the rest of the fleet uses the override → split-brain.
	// One stderr line per process makes the miss visible. We do NOT auto-honor
	// the .env value here — that's a behavior change and lives in callers
	// that opt into it (e.g. skills/agent-registry/scripts/_workspace_resolve.py).
	if (!_fallbackWarnPrinted) {
		_fallbackWarnPrinted = true;
		const envFileVal = grepEnvForWorkspace();
		if (envFileVal && envFileVal !== fallback) {
			process.stderr.write(
				`workspace: SUTANDO_WORKSPACE is unset in process env, falling back to ${fallback}. ` +
				`NOTE: .env declares SUTANDO_WORKSPACE='${envFileVal}' which is NOT being honored here — ` +
				`source .env or export the var before this process to avoid split-brain with other services.\n`
			);
		}
	}
	return fallback;
}

/**
 * Canonical WRITE location of a status file: `<workspace>/state/<name>`.
 * Loose status .json files live under state/, not the workspace root — the
 * root is structural (directories only). Twin of workspace_default.py's
 * `status_path`. Writers always use this.
 */
export function statusPath(name: string, workspace?: string): string {
	return join(workspace ?? resolveWorkspace(), 'state', name);
}

/**
 * READ location of a status file: prefer `state/<name>`, fall back to the
 * legacy workspace-root `<name>` so an un-migrated install keeps working for
 * one release. Returns the `state/` path when neither exists. The fallback
 * branch is removed the release after this one.
 */
export function statusReadPath(name: string, workspace?: string): string {
	const ws = workspace ?? resolveWorkspace();
	const p = join(ws, 'state', name);
	if (existsSync(p)) return p;
	const legacy = join(ws, name);
	return existsSync(legacy) ? legacy : p;
}
