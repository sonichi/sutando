// TypeScript twin of src/util_paths.py — personal-asset path resolution.
//
// Two helpers, one for per-machine state, one for shared-across-fleet state:
//
//   personalPath(filename)          — `$SUTANDO_PRIVATE_DIR/machine-<host>/<filename>`
//                                     For files where each Mac has its own copy
//                                     (stand-identity.json, pending-questions.md).
//
//   sharedPersonalPath(filename)    — `$SUTANDO_PRIVATE_DIR/<filename>`
//                                     For files synced across the whole fleet
//                                     (notes/, build_log.md).
//
// Both fall back to `<workspace>/<filename>` so existing installs keep working
// until they migrate. The `workspace` arg is optional; when omitted, the
// helpers resolve to `$SUTANDO_WORKSPACE` (default `~/.sutando/workspace/`)
// via resolveWorkspace() — NOT process.cwd(). Pre-#839 fixes the fallback was
// cwd, which silently produced the wrong path on hosts where the caller's
// cwd drifted from the workspace dir.

import { existsSync } from 'node:fs';
import { hostname } from 'node:os';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

function expandHome(p: string): string {
	return p.replace(/^~/, process.env.HOME || '');
}

/** Per-machine resolver. */
export function personalPath(filename: string, workspace?: string): string {
	const ws = workspace ?? resolveWorkspace();
	const privateRoot = process.env.SUTANDO_PRIVATE_DIR;
	if (privateRoot) {
		const root = expandHome(privateRoot);
		const host = hostname().split('.')[0];
		const candidate = join(root, `machine-${host}`, filename);
		if (existsSync(candidate)) return candidate;
	}
	// stand-avatar.png lives under assets/ in the public workspace.
	if (filename === 'stand-avatar.png') {
		const inAssets = join(ws, 'assets', filename);
		if (existsSync(inAssets)) return inAssets;
	}
	const wsPath = join(ws, filename);
	if (existsSync(wsPath)) return wsPath;
	// Nothing exists; return preferred private path so caller's existsSync()
	// check fails gracefully.
	if (privateRoot) {
		const root = expandHome(privateRoot);
		const host = hostname().split('.')[0];
		return join(root, `machine-${host}`, filename);
	}
	if (filename === 'stand-avatar.png') return join(ws, 'assets', filename);
	return wsPath;
}

/** Shared-across-fleet resolver (top-level private dir, not per-machine). */
export function sharedPersonalPath(filename: string, workspace?: string): string {
	const ws = workspace ?? resolveWorkspace();
	const privateRoot = process.env.SUTANDO_PRIVATE_DIR;
	if (privateRoot) {
		const root = expandHome(privateRoot);
		const candidate = join(root, filename);
		if (existsSync(candidate)) return candidate;
		const wsPath = join(ws, filename);
		if (existsSync(wsPath)) return wsPath;
		return candidate;
	}
	return join(ws, filename);
}


// ---------------------------------------------------------------------------
// Claude Code home directory — the host CLI's per-user state lives at
// `~/.claude/`. Sutando consumes several subpaths (channels/, projects/,
// skills/, settings.json, etc.); centralizing the resolution here keeps the
// host-CLI dependency surface a single grep.
//
// Why this helper: per the 2026-05-18 workspace-design RFC discussion, the
// dependency on `~/.claude/` is real (memory storage, channel tokens, skill
// discovery, slash-command write convention) and we accept it operationally —
// but we want the surface countable so a future swap is a 1-day grep+replace
// rather than a re-architecture. ANY new read/write into the Claude Code home
// directory should go through this helper.
//
// Resolution: prefer $CLAUDE_HOME if set (override / testing), else
// `~/.claude/`. Does NOT create the dir.
// ---------------------------------------------------------------------------

/**
 * Resolve a path under Claude Code's per-user home (`~/.claude/`).
 *
 * Pass subpath components as separate args:
 *   claudeHomePath('channels', 'discord', 'access.json')
 *   claudeHomePath('projects', projectSlug, 'memory', 'MEMORY.md')
 *   claudeHomePath('skills', skillName)
 *
 * Override the base with `$CLAUDE_HOME` for tests + alt-host installs.
 */
export function claudeHomePath(...subpath: string[]): string {
	const baseEnv = process.env.CLAUDE_HOME;
	const base = baseEnv
		? expandHome(baseEnv)
		: join(process.env.HOME || '', '.claude');
	if (subpath.length === 0) return base;
	return join(base, ...subpath);
}
