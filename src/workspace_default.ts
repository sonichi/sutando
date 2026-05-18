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

import { homedir } from 'node:os';
import { join } from 'node:path';

export function resolveWorkspace(): string {
	const env = process.env.SUTANDO_WORKSPACE?.trim();
	if (env) return env.replace(/^~/, homedir());
	return join(homedir(), '.sutando', 'workspace');
}
