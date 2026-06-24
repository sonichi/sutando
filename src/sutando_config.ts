/**
 * Canonical loader for `sutando.config.json` / `sutando.config.local.json`.
 *
 * Twin of `src/sutando_config.py`. Same resolution order, same deep-merge
 * semantics, same comment-stripping, same `${REPO_DIR}` expansion. Both
 * languages must agree byte-for-byte on the resolved config so that
 * Python services (bridges, health-check) and TS services (voice-agent,
 * task-bridge) land in the same workspace.
 *
 * Resolution order (highest layer wins, v0.8 — env override removed):
 *   1. `sutando.config.local.json` (per-clone override, gitignored)
 *   2. `sutando.config.json` (tracked defaults at repo root)
 *   3. Baked-in default (`{repo_root}/workspace`)
 *
 * `$SUTANDO_WORKSPACE` is no longer honored. If set in the environment,
 * a one-time stderr warning fires pointing at `scripts/sutando-migrate.sh`
 * for relocation of any data still living at the env-pointed path.
 *
 * No external deps — stdlib only.
 */

import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, parse, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// --------------------------------------------------------------------------- //
//  File discovery                                                             //
// --------------------------------------------------------------------------- //

const CONFIG_FILENAME = 'sutando.config.json';
const LOCAL_FILENAME = 'sutando.config.local.json';

/**
 * Known top-level keys the loader understands. The matching JSON Schema
 * declares `additionalProperties: false` for IDE strictness; the loader
 * stays lenient (warn-only) so experimental/scratch keys don't break.
 * Per Mini's review #8 on PR #1395.
 */
const KNOWN_TOP_LEVEL_KEYS = new Set(['workspace', 'claude_sutando_config_dir', 'vault']);

/**
 * Walk upward from `start` until we find a directory containing
 * `sutando.config.json`. Returns undefined if not found within 6 hops.
 * Anchors on the config file rather than `.git/` so app bundles + symlinked
 * installs still resolve correctly.
 *
 * Emits a one-line stderr diagnostic on miss (gated by `SUTANDO_DEBUG=1` to
 * keep happy-path noise out of normal runs). Helps users diagnose "why is
 * Sutando using the baked-in default" without strace, per Mini's review #3
 * on PR #1395.
 */
/** @internal Exported for tests; production callers go through loadConfig. */
export function findRepoRoot(start?: string): string | undefined {
	const initial = resolve(start ?? dirname(fileURLToPath(import.meta.url)));
	let cur = initial;
	for (let i = 0; i < 6; i++) {
		if (existsSync(join(cur, CONFIG_FILENAME))) return cur;
		const parent = dirname(cur);
		if (parent === cur) break;
		cur = parent;
	}
	// Strict equality to "1" so SUTANDO_DEBUG=0 / "false" / "" don't accidentally
	// turn on the diagnostic. Mini called this out in the #1397 review — env
	// truthiness in JS treats any non-empty string as truthy, which would
	// silently emit on common "disable" values.
	if (process.env.SUTANDO_DEBUG === '1') {
		process.stderr.write(
			`sutando config: findRepoRoot walked 6 hops from ${initial} ` +
				`and did not find ${CONFIG_FILENAME}; falling back to baked-in default.\n`,
		);
	}
	return undefined;
}

// --------------------------------------------------------------------------- //
//  JSON loading + comment stripping                                           //
// --------------------------------------------------------------------------- //

type Json = string | number | boolean | null | { [k: string]: Json } | Json[];

/**
 * Recursively drop dict keys whose name starts with `_` (comment convention).
 * Lists are walked element-wise; scalars pass through.
 */
function stripComments(obj: Json): Json {
	if (Array.isArray(obj)) return obj.map(stripComments);
	if (obj !== null && typeof obj === 'object') {
		const out: { [k: string]: Json } = {};
		for (const [k, v] of Object.entries(obj)) {
			if (!k.startsWith('_')) out[k] = stripComments(v);
		}
		return out;
	}
	return obj;
}

/**
 * Read + parse a JSON file, strip comment keys, return the resulting object.
 * Empty file is treated as `{}`. Parse errors throw with a clear message.
 */
function loadJsonFile(path: string): { [k: string]: Json } {
	if (!existsSync(path)) return {};
	const text = readFileSync(path, 'utf8').trim();
	if (!text) return {};
	let data: Json;
	try {
		data = JSON.parse(text);
	} catch (e) {
		const msg = e instanceof Error ? e.message : String(e);
		throw new Error(`sutando config: failed to parse ${path}: ${msg}`);
	}
	if (data === null || typeof data !== 'object' || Array.isArray(data)) {
		throw new Error(`sutando config: ${path} top-level must be a JSON object, got ${typeof data}`);
	}
	const stripped = stripComments(data);
	return stripped as { [k: string]: Json };
}

// --------------------------------------------------------------------------- //
//  Deep merge + variable expansion                                            //
// --------------------------------------------------------------------------- //

/**
 * Recursively merge `override` into `base`. Dicts merge; everything else
 * (arrays, scalars, null) is REPLACED by the override. Returns a new object;
 * inputs are not mutated.
 */
function deepMerge(
	base: { [k: string]: Json },
	override: { [k: string]: Json },
): { [k: string]: Json } {
	const out: { [k: string]: Json } = { ...base };
	for (const [k, v] of Object.entries(override)) {
		const existing = out[k];
		if (
			v !== null &&
			typeof v === 'object' &&
			!Array.isArray(v) &&
			existing !== null &&
			typeof existing === 'object' &&
			!Array.isArray(existing)
		) {
			out[k] = deepMerge(existing as { [k: string]: Json }, v as { [k: string]: Json });
		} else {
			out[k] = v;
		}
	}
	return out;
}

/**
 * Expand `${REPO_DIR}` in every string value of the config tree. Only that
 * token is recognized — other variables pass through untouched. Walks dicts
 * + arrays; non-string scalars are returned as-is.
 */
function expandVars(obj: Json, repoDir: string): Json {
	const token = '${REPO_DIR}';
	if (typeof obj === 'string') return obj.split(token).join(repoDir);
	if (Array.isArray(obj)) return obj.map((v) => expandVars(v, repoDir));
	if (obj !== null && typeof obj === 'object') {
		const out: { [k: string]: Json } = {};
		for (const [k, v] of Object.entries(obj)) out[k] = expandVars(v, repoDir);
		return out;
	}
	return obj;
}

// --------------------------------------------------------------------------- //
//  Top-level loader                                                           //
// --------------------------------------------------------------------------- //

let _cache: { [k: string]: Json } | undefined;
let _cacheRepoRoot: string | undefined;
let _legacyEnvWarnPrinted = false;
let _dotenvDriftWarnPrinted = false;
let _unknownKeysWarnPrinted = false;

/**
 * Wrap `msg` in bold-red ANSI when stderr is a TTY; pass through otherwise.
 *
 * Keeps the v0.8 deprecation warnings ($SUTANDO_WORKSPACE no longer honored,
 * .env declares stale SUTANDO_WORKSPACE) eye-catching in interactive terminals
 * so operators actually notice and migrate, while keeping log captures (script,
 * tee, journald, GitHub Actions) free of escape sequences. `NO_COLOR=1` honored
 * as a hard opt-out (see no-color.org).
 */
function _colorWarn(msg: string): string {
	if (process.env.NO_COLOR) return msg;
	try {
		if (process.stderr.isTTY) return `\x1b[1;31m${msg}\x1b[0m`;
	} catch {
		// ignore
	}
	return msg;
}

/** Test-only: clear the per-process cache. */
export function resetCacheForTests(): void {
	_cache = undefined;
	_cacheRepoRoot = undefined;
	_legacyEnvWarnPrinted = false;
	_dotenvDriftWarnPrinted = false;
	_unknownKeysWarnPrinted = false;
}

function warnUnknownTopLevelKeys(cfg: { [k: string]: Json }, path: string): void {
	if (_unknownKeysWarnPrinted) return;
	const extras = Object.keys(cfg)
		.filter((k) => !KNOWN_TOP_LEVEL_KEYS.has(k))
		.sort();
	if (extras.length === 0) return;
	_unknownKeysWarnPrinted = true;
	process.stderr.write(
		`sutando config: ${path} has top-level keys the loader does not read: ` +
			`${extras.map((k) => `'${k}'`).join(', ')}. Known keys: ` +
			`${[...KNOWN_TOP_LEVEL_KEYS].sort().join(', ')}. Typo? Or experimental key — ` +
			`the loader will ignore it either way.\n`,
	);
}

/**
 * Load + merge sutando config from disk. Memoized per-process.
 *
 * `repoRoot` is the directory holding `sutando.config.json`; defaults to
 * the result of `findRepoRoot()`. Pass an explicit path in tests.
 *
 * Throws only for parse errors (malformed JSON) or structurally-invalid
 * top-level (non-object). Missing files are tolerated and yield `{}`.
 */
export function loadConfig(repoRoot?: string): { [k: string]: Json } {
	if (_cache !== undefined && (repoRoot === undefined || repoRoot === _cacheRepoRoot)) {
		return _cache;
	}
	const root = repoRoot ?? findRepoRoot();
	if (root === undefined) {
		_cache = {};
		_cacheRepoRoot = undefined;
		return _cache;
	}
	const defaults = loadJsonFile(join(root, CONFIG_FILENAME));
	const overrides = loadJsonFile(join(root, LOCAL_FILENAME));
	const merged = deepMerge(defaults, overrides);
	const expanded = expandVars(merged, root) as { [k: string]: Json };
	_cache = expanded;
	_cacheRepoRoot = root;
	warnUnknownTopLevelKeys(expanded, join(root, CONFIG_FILENAME));
	return expanded;
}

// --------------------------------------------------------------------------- //
//  Public path resolvers                                                      //
// --------------------------------------------------------------------------- //

const HARDCODED_WORKSPACE_DEFAULT_REL = 'workspace';

/**
 * Resolve the workspace directory per the canonical contract.
 *
 * Order (v0.8 — `$SUTANDO_WORKSPACE` no longer honored):
 *   1. `sutando.config.{json,local.json}` → `workspace.path` (deep-merged).
 *   2. `{repoRoot}/workspace` baked-in default.
 *
 * If `$SUTANDO_WORKSPACE` is set in the environment, prints a one-time
 * migration-nag warning pointing at `scripts/sutando-migrate.sh` but does
 * NOT honor the env value (the legacy escape hatch was removed in v0.8
 * per `docs/workspace-contract-v0.8.md`).
 *
 * Returns an absolute path. Does NOT create the directory.
 */
export function resolveWorkspace(repoRoot?: string): string {
	const envVal = process.env.SUTANDO_WORKSPACE?.trim();

	// Test-only escape hatch: when `SUTANDO_TEST_MODE=1` is set, honor
	// `$SUTANDO_WORKSPACE` silently. This preserves the v0.8 contract for
	// end users (no env override; warning + ignore) while letting the test
	// suite redirect workspace to per-test tmp dirs without rewriting every
	// test fixture. Production code MUST NOT set `SUTANDO_TEST_MODE`.
	if (envVal && process.env.SUTANDO_TEST_MODE === '1') {
		return resolve(envVal.replace(/^~/, homedir()));
	}

	if (envVal && !_legacyEnvWarnPrinted) {
		_legacyEnvWarnPrinted = true;
		// PR #1440 B4: drop the literal `'${envVal}'` interpolation (parity with
		// Python's c58270d safety pass). Embedding /-bearing path values in
		// stderr was the trigger for the caller-side `mkdir -p "$captured"`
		// regression that created a rogue folder tree from a tokenized warning.
		process.stderr.write(
			_colorWarn(
				`sutando config: $SUTANDO_WORKSPACE is set but NO LONGER HONORED ` +
					`(removed in v0.8). The workspace now resolves from sutando.config.{json,local.json} ` +
					`or the {repoRoot}/workspace baked-in default. If you have existing workspace data at ` +
					`the env-pointed path, run \`bash scripts/sutando-migrate.sh --dry-run\` to preview a ` +
					`relocation, then \`--commit\`. Unset $SUTANDO_WORKSPACE in your shell + .env to silence ` +
					`this warning.`,
			) + '\n',
		);
	}

	const cfg = loadConfig(repoRoot);
	const root = repoRoot ?? _cacheRepoRoot;
	const ws = (cfg.workspace as { [k: string]: Json } | undefined)?.path;
	let resolved: string;
	if (typeof ws === 'string' && ws) {
		resolved = resolve(ws.replace(/^~/, homedir()));
	} else if (root === undefined) {
		resolved = resolve(join(homedir(), '.sutando', 'workspace'));
	} else {
		resolved = resolve(join(root, HARDCODED_WORKSPACE_DEFAULT_REL));
	}

	// .env-drift warning: if `.env` still carries a stale SUTANDO_WORKSPACE line,
	// surface it once per process so the operator can clean it up.
	// (v0.8: the env var is no longer honored regardless of whether it's set in
	// the shell or in .env; the warning is purely cleanup guidance.)
	if (!_dotenvDriftWarnPrinted) {
		_dotenvDriftWarnPrinted = true;
		const dotenvVal = detectEnvWorkspaceInDotenv(repoRoot);
		if (dotenvVal) {
			// PR #1440 B4: drop literal `'${dotenvVal}'` and `${resolved}` path
			// interpolations (parity with Python's c58270d safety pass).
			process.stderr.write(
				_colorWarn(
					`sutando config: .env declares SUTANDO_WORKSPACE but the env var is no longer honored ` +
						`(removed in v0.8). The resolved workspace is config-driven ` +
						`(sutando.config.{json,local.json} or {repoRoot}/workspace default). ` +
						`Delete the .env line and, if needed, move the value to \`sutando.config.local.json\` ` +
						`under \`workspace.path\`.`,
				) + '\n',
			);
		}
	}
	return resolved;
}

/**
 * Vault config with defaults filled in. Missing config yields
 * `{ enabled: false, ... }` so callers can branch on `cfg.enabled` safely.
 */
export interface VaultConfig {
	enabled: boolean;
	remote_url: string;
	sync: { include: string[]; exclude: string[] };
	interval_seconds: number;
}

export function resolveVault(repoRoot?: string): VaultConfig {
	const cfg = loadConfig(repoRoot);
	const vault = (cfg.vault as { [k: string]: Json } | undefined) ?? {};
	const sync = (vault.sync as { [k: string]: Json } | undefined) ?? {};
	const includeRaw = sync.include;
	const excludeRaw = sync.exclude;
	return {
		enabled: typeof vault.enabled === 'boolean' ? vault.enabled : false,
		remote_url: typeof vault.remote_url === 'string' ? vault.remote_url : '',
		sync: {
			include: Array.isArray(includeRaw) ? includeRaw.filter((v): v is string => typeof v === 'string') : [],
			exclude: Array.isArray(excludeRaw) ? excludeRaw.filter((v): v is string => typeof v === 'string') : [],
		},
		interval_seconds: typeof vault.interval_seconds === 'number' ? vault.interval_seconds : 1800,
	};
}

const DEFAULT_CLAUDE_SUTANDO_SUBDIR = '.claude-sutando';

/**
 * Resolve the CLAUDE_CONFIG_DIR target for the `claude-sutando` shell alias.
 *
 * The path is always a sub-folder of `resolveWorkspace()` — the M2 vault sync
 * engine relies on this invariant to include the Claude config tree via a
 * single workspace-relative glob. Absolute paths and `..` escapes in config
 * are rejected at load (schema pattern) AND asserted again here (defense in
 * depth, catches symlink escapes the regex misses).
 *
 * Does NOT create the directory — callers (e.g. `scripts/sutando-shell-setup.sh`)
 * are responsible for mkdir as part of the alias-setup flow.
 *
 * @throws if the subdir violates the workspace-sub-folder invariant.
 */
export function resolveClaudeSutandoConfigDir(repoRoot?: string): string {
	const cfg = loadConfig(repoRoot);
	const block = (cfg.claude_sutando_config_dir as { [k: string]: Json } | undefined) ?? {};
	const subdir = (typeof block.subdir === 'string' ? block.subdir : '') || DEFAULT_CLAUDE_SUTANDO_SUBDIR;

	// Defense in depth: re-validate the invariants the schema enforces at load
	// time, in case config bypassed validation or was hand-edited.
	if (!subdir || subdir.startsWith('/') || subdir.split(/[\\/]/).includes('..')) {
		throw new Error(
			`claude_sutando_config_dir.subdir=${JSON.stringify(subdir)} violates the ` +
				`workspace-sub-folder invariant — must be a non-absolute, non-escaping relative ` +
				`path (M2 sync coherence depends on this).`,
		);
	}

	const workspace = resolveWorkspace(repoRoot);
	const final = join(workspace, subdir);

	// Final-path check — realpath follows symlinks, so the CANONICAL form of the
	// result must still be inside the CANONICAL form of the workspace tree. We
	// use realpath ONLY for this invariant check; the returned path stays in its
	// un-canonicalized form so callers get a string prefix consistent with
	// resolveWorkspace() (e.g. on macOS, `/tmp/...` doesn't become `/private/tmp/...`
	// just because we passed through realpath).
	try {
		const finalReal = realpathSync.native ? realpathSync.native(final) : final;
		const workspaceReal = realpathSync.native ? realpathSync.native(workspace) : workspace;
		const sep = workspaceReal.endsWith('/') ? '' : '/';
		if (finalReal !== workspaceReal && !finalReal.startsWith(workspaceReal + sep)) {
			throw new Error(
				`claude_sutando_config_dir.subdir=${JSON.stringify(subdir)} resolves outside ` +
					`the workspace (${finalReal} not under ${workspaceReal}). Likely a symlink escape; reject.`,
			);
		}
	} catch (err) {
		// realpath throws if the path doesn't exist yet (first-run); that's fine —
		// the un-canonicalized form is still string-prefix safe since we never
		// dereferenced symlinks in this branch. Surface non-existence as no-op.
		if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
	}

	return final;
}

/**
 * Scan the repo's `.env` for `SUTANDO_WORKSPACE=` and return the value if
 * found, else undefined. Used by the startup banner to warn users that their
 * `.env` declares a workspace path that the loader is bypassing in favor of
 * config. Best-effort: silent on file-not-found or read errors.
 */
export function detectEnvWorkspaceInDotenv(repoRoot?: string): string | undefined {
	const root = repoRoot ?? findRepoRoot();
	if (root === undefined) return undefined;
	const envFile = join(root, '.env');
	if (!existsSync(envFile)) return undefined;
	try {
		for (const line of readFileSync(envFile, 'utf8').split('\n')) {
			const s = line.trim();
			if (s.startsWith('#') || !s.includes('=')) continue;
			const [key, ...rest] = s.split('=');
			if (key.trim() !== 'SUTANDO_WORKSPACE') continue;
			let v = rest.join('=').trim();
			if (v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === '"' || v[0] === "'")) {
				v = v.slice(1, -1);
			}
			return v ? v.replace(/^~/, homedir()) : undefined;
		}
	} catch {
		// Best-effort.
	}
	return undefined;
}
