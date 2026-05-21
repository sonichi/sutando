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

import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export function resolveWorkspace(): string {
	const env = process.env.SUTANDO_WORKSPACE?.trim();
	if (env) return env.replace(/^~/, homedir());
	return join(homedir(), '.sutando', 'workspace');
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
