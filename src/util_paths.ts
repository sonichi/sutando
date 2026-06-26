// TypeScript twin of src/util_paths.py — personal-asset path resolution.
//
// Two helpers, one for per-machine state, one for shared-across-fleet state:
//
//   personalPath(filename)          — `$SUTANDO_MEMORY_DIR/machine-<host>/<filename>`
//                                     For files where each Mac has its own copy
//                                     (stand-identity.json, pending-questions.md).
//
//   sharedPersonalPath(filename)    — `$SUTANDO_MEMORY_DIR/<filename>`
//                                     For files synced across the whole fleet
//                                     (notes/, build_log.md).
//
// Both fall back to `<workspace>/<filename>` so existing installs keep working
// until they migrate. The `workspace` arg is optional; when omitted, the
// helpers resolve via `resolveWorkspace()` — post-v0.8 (#1440) the default is
// `<repo>/workspace/` and `$SUTANDO_WORKSPACE` is no longer honored — NOT
// process.cwd(). Pre-#839 fixes the fallback was cwd, which silently produced
// the wrong path on hosts where the caller's cwd drifted from the workspace dir.
//
// Env var `SUTANDO_MEMORY_DIR` is the canonical name post-#858 / #870. The
// legacy alias `SUTANDO_PRIVATE_DIR` is honored as a fallback for one release
// with a deprecation warning logged to stderr on every read (cron / launchd
// environments miss startup-only warnings, so logging at every resolution is
// intentional).

import { existsSync } from 'node:fs';
import { hostname } from 'node:os';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

function expandHome(p: string): string {
	return p.replace(/^~/, process.env.HOME || '');
}

/**
 * Return the resolved memory-dir env value, preferring the new name.
 *
 * Lookup order:
 *   1. `SUTANDO_MEMORY_DIR` (canonical post-#858 / #870)
 *   2. `SUTANDO_PRIVATE_DIR` (legacy, with deprecation warning emitted to
 *      stderr on every read — not just once at startup; cron and launchd
 *      environments miss startup-only warnings).
 *
 * Returns the raw env value (caller must expandHome if needed), or undefined
 * when neither is set.
 */
export function memoryDirEnv(): string | undefined {
	const next = process.env.SUTANDO_MEMORY_DIR;
	if (next) return next;
	const legacy = process.env.SUTANDO_PRIVATE_DIR;
	if (legacy) {
		// Every-read deprecation warning. Loud by design — the legacy alias
		// will drop in the next release and silent users would otherwise miss
		// the cutover. See #870 for the rename plan.
		console.warn(
			'[util_paths.ts] DEPRECATION: SUTANDO_PRIVATE_DIR is the old name ' +
				'for the memory dir; set SUTANDO_MEMORY_DIR instead (this alias ' +
				'will be removed in the next release). See #870.',
		);
		return legacy;
	}
	return undefined;
}

/**
 * Per-host directory label: `$SUTANDO_HOST_LABEL` or short hostname.
 *
 * Single source of truth for the per-host segment so the legacy
 * `machine-<host>/` (memory-dir) and new `hosts/<host>/` (workspace)
 * conventions stay in lockstep. Matches `_host()` in sync-workspace.sh:
 * an explicit label is an override and is used RAW; only the auto-detected
 * hostname has its mDNS/domain suffix stripped. (A dotted label like
 * "a.b" must NOT be split — splitting it would strand the reader, the very
 * class this PR fixes. Mirrors `_host_label()` in util_paths.py.)
 */
function hostLabel(): string {
	const label = process.env.SUTANDO_HOST_LABEL;
	if (label) return label;
	return hostname().split('.')[0];
}

/** Per-machine resolver. */
export function personalPath(filename: string, workspace?: string): string {
	const ws = workspace ?? resolveWorkspace();
	// New per-host canonical home (workspace-as-git-repo, #1717). Probed first
	// so relocated files are found; absent → falls through to the legacy order
	// (identical to pre-#1717 behavior). Reader half of the per-host
	// relocation — without it, moving a per-host file into `hosts/<host>/`
	// would silently strand readers on the workspace-root fallback (H4).
	const hostCandidate = join(ws, 'hosts', hostLabel(), filename);
	if (existsSync(hostCandidate)) return hostCandidate;
	const privateRoot = memoryDirEnv();
	if (privateRoot) {
		const root = expandHome(privateRoot);
		const candidate = join(root, `machine-${hostLabel()}`, filename);
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
		return join(root, `machine-${hostLabel()}`, filename);
	}
	if (filename === 'stand-avatar.png') return join(ws, 'assets', filename);
	return wsPath;
}

/** Shared-across-fleet resolver (top-level private dir, not per-machine). */
export function sharedPersonalPath(filename: string, workspace?: string): string {
	const ws = workspace ?? resolveWorkspace();
	const privateRoot = memoryDirEnv();
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
// Resolution (3-tier, prefer most specific):
//   1. $CLAUDE_CONFIG_DIR  — Claude Code's canonical env var (string present
//      in the `claude` binary). Set by `claude-sutando` shell function +
//      scripts/start-cli.sh + src/startup.sh so every workspace gets its own
//      .claude-sutando/ tree instead of sharing global ~/.claude/.
//   2. $CLAUDE_HOME        — deprecated legacy override (kept for one release
//      so pre-M0 callers / test fixtures don't break instantly). Emits a
//      one-shot warning to stderr on first read.
//   3. ~/.claude/          — final fallback.
// Does NOT create the dir.
// ---------------------------------------------------------------------------

let _claudeHomeDeprecationWarned = false;

/**
 * Resolve a path under Claude Code's per-user config dir.
 *
 * Pass subpath components as separate args:
 *   claudeHomePath('channels', 'discord', 'access.json')
 *   claudeHomePath('projects', projectSlug, 'memory', 'MEMORY.md')
 *   claudeHomePath('skills', skillName)
 *
 * Prefers `$CLAUDE_CONFIG_DIR` (Claude Code canonical). Falls back to
 * deprecated `$CLAUDE_HOME` then `~/.claude/`. See Mini PR #1415 review #5
 * for the original-env-var-mismatch that motivated this.
 */
export function claudeHomePath(...subpath: string[]): string {
	const ccd = process.env.CLAUDE_CONFIG_DIR;
	const home = process.env.CLAUDE_HOME;
	let base: string;
	if (ccd) {
		base = expandHome(ccd);
	} else if (home) {
		if (!_claudeHomeDeprecationWarned) {
			_claudeHomeDeprecationWarned = true;
			console.warn(
				'[util_paths] $CLAUDE_HOME is deprecated; set $CLAUDE_CONFIG_DIR instead (will be removed next release).',
			);
		}
		base = expandHome(home);
	} else {
		base = join(process.env.HOME || '', '.claude');
	}
	if (subpath.length === 0) return base;
	return join(base, ...subpath);
}
