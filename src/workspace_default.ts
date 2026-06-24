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
 * Resolution order (post-v0.8 / #1440, via src/sutando_config.ts):
 *   1. sutando.config.local.json -> workspace.path (per-clone override)
 *   2. sutando.config.json -> workspace.path (tracked defaults)
 *   3. ${REPO_DIR}/workspace baked-in default
 *
 * $SUTANDO_WORKSPACE is no longer honored for resolution (removed in v0.8);
 * if set, the loader fires a one-time deprecation warning + triggers one-time
 * auto-migration via per-source sentinels (PR #1478), but the resolver
 * ignores its value. The ad-hoc no-config-no-repo-root last-ditch fallback
 * is `~/sutando-workspace/` (was `~/.sutando/workspace/` pre-v0.8 — namespace
 * retired per Mini opinion-requested 2026-06-06).
 */

import { existsSync } from 'node:fs';
import { join } from 'node:path';

import { resolveWorkspace as _resolveWorkspaceFromConfig } from './sutando_config.js';

/**
 * Resolve the workspace directory per the canonical contract.
 *
 * **Delegates to `src/sutando_config.ts::resolveWorkspace`** as of the
 * M0 cutover. The new loader implements the resolution order:
 *
 *   1. sutando.config.local.json -> workspace.path (per-clone override)
 *   2. sutando.config.json -> workspace.path (tracked defaults)
 *   3. ${REPO_DIR}/workspace baked-in default
 *
 * This wrapper is preserved so existing callers don't need code changes
 * — the export name + signature + return type are unchanged. Post-v0.8
 * (#1440), $SUTANDO_WORKSPACE is no longer honored for workspace
 * resolution; if set, the loader fires a one-time deprecation warning
 * and triggers one-time auto-migration via per-source sentinels
 * (PR #1478), but the resolver ignores its value.
 *
 * Default location is ${REPO_DIR}/workspace (in-repo). .env declarations
 * of SUTANDO_WORKSPACE are also detected and warned about; they do not
 * affect resolution.
 */
export function resolveWorkspace(): string {
	return _resolveWorkspaceFromConfig();
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
