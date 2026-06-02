/**
 * Canonical loader for `sutando.config.json` / `sutando.config.local.json`.
 *
 * Twin of `src/sutando_config.py`. Same resolution order, same deep-merge
 * semantics, same comment-stripping, same `${REPO_DIR}` expansion. Both
 * languages must agree byte-for-byte on the resolved config so that
 * Python services (bridges, health-check) and TS services (voice-agent,
 * task-bridge) land in the same workspace.
 *
 * Resolution order (highest layer wins):
 *   1. `$SUTANDO_WORKSPACE` env var (legacy escape hatch; warn on use)
 *   2. `sutando.config.local.json` (per-clone override, gitignored)
 *   3. `sutando.config.json` (tracked defaults at repo root)
 *   4. Baked-in default (`{repo_root}/workspace`)
 *
 * No external deps — stdlib only.
 */

import { existsSync, readFileSync } from 'node:fs';
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
const KNOWN_TOP_LEVEL_KEYS = new Set(['workspace', 'vault']);

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
 * Order:
 *   1. `$SUTANDO_WORKSPACE` env var (legacy escape hatch; warn once).
 *   2. `sutando.config.{json,local.json}` → `workspace.path` (deep-merged).
 *   3. `{repoRoot}/workspace` baked-in default.
 *
 * Returns an absolute path. Does NOT create the directory.
 */
export function resolveWorkspace(repoRoot?: string): string {
	const envVal = process.env.SUTANDO_WORKSPACE?.trim();
	if (envVal) {
		if (!_legacyEnvWarnPrinted) {
			_legacyEnvWarnPrinted = true;
			process.stderr.write(
				`sutando config: $SUTANDO_WORKSPACE is set ('${envVal}'); honoring as legacy escape ` +
					`hatch. To silence, move the value into \`sutando.config.local.json\` under ` +
					`\`workspace.path\` and unset the env.\n`,
			);
		}
		return resolve(envVal.replace(/^~/, homedir()));
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

	// M0 .env-drift warning: if the env var is UNSET but `.env` declares one,
	// surface the stale .env line once per process. See sutando_config.py for
	// the design rationale — this is the TS twin of the same behavior.
	if (!_dotenvDriftWarnPrinted) {
		_dotenvDriftWarnPrinted = true;
		const dotenvVal = detectEnvWorkspaceInDotenv(repoRoot);
		if (dotenvVal && resolve(dotenvVal) !== resolved) {
			process.stderr.write(
				`sutando config: .env declares SUTANDO_WORKSPACE='${dotenvVal}', but resolved workspace ` +
					`is ${resolved} (config-driven). The .env line is NOT honored by the new loader — ` +
					`move the value to \`sutando.config.local.json\` under \`workspace.path\` and delete the ` +
					`.env line, or \`source .env\` before launching processes that need the override.\n`,
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
