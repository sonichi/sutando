/**
 * Model-fallback config for the credential proxy. Keys are declared in this
 * skill's manifest.json `config` block (the shipped defaults); the owner's
 * adjustments live in <workspace>/hosts/<host>/quota-fallback-config.json,
 * written by scripts/fallback-config.py so a long-running proxy picks them up
 * without a restart. Precedence: env > per-host override file > manifest > built-in.
 */
import { readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_FALLBACK_CONFIG, validModelForLevel, type FallbackConfig, type Level, type Window, type WindowThresholds } from './quota-fallback-policy.js';

/** Owner overrides are per host: <workspace>/hosts/<host>/<basename>, beside crons.json. */
export const OVERRIDE_BASENAME = 'quota-fallback-config.json';
const P = 'SUTANDO_QUOTA_FALLBACK_';

/** Every key the proxy reads; the manifest declares exactly this set. */
export const CONFIG_KEYS = [
	`${P}ENABLED`,
	`${P}5H_LEVEL1`, `${P}5H_LEVEL2`,
	`${P}7D_LEVEL1`, `${P}7D_LEVEL2`,
	`${P}HYSTERESIS`,
	`${P}5H_PROJECTION`, `${P}5H_PROJECTION_LIMIT`, `${P}5H_PROJECTION_LOOKBACK_SEC`,
	`${P}5H_PROJECTION_MIN_SPAN_SEC`, `${P}5H_PROJECTION_CLEAR_SAMPLES`,
	`${P}5H_PROJECTION_CLEAR_AFTER_SEC`, `${P}5H_PROJECTION_ESCALATE_AFTER_SEC`,
	`${P}LOW_PRIORITY`, `${P}LOW_LEVEL1`, `${P}LOW_LEVEL2`,
	`${P}LEVEL2_MODEL`, `${P}LEVEL3_MODEL`, `${P}FAMILY_LEVELS`,
	`${P}DM_MIN_INTERVAL_SEC`,
] as const;

export type RawConfig = Record<string, string>;

function flag(v: string | undefined, dflt: boolean): boolean {
	if (v === undefined) return dflt;
	const s = v.trim().toLowerCase();
	if (['1', 'true', 'on', 'yes'].includes(s)) return true;
	if (['0', 'false', 'off', 'no', ''].includes(s)) return false;
	return dflt;
}

function frac(v: string | undefined, dflt: number): number {
	if (v === undefined) return dflt;
	const n = parseFloat(v);
	return Number.isFinite(n) && n >= 0 && n <= 2 ? n : dflt;
}

function count(v: string | undefined, dflt: number): number {
	if (v === undefined) return dflt;
	const n = parseInt(v, 10);
	return Number.isFinite(n) && n >= 0 ? n : dflt;
}

function model(v: string | undefined, level: Level, families: Record<string, Level>, dflt: string): string {
	const s = (v ?? '').trim();
	return s && validModelForLevel(s, level, families) ? s : dflt;
}

/** "fable:1,opus:2" → {fable: 1, opus: 2}; a malformed list keeps the default map. */
export function parseFamilyLevels(v: string | undefined, dflt: Record<string, Level>): Record<string, Level> {
	if (v === undefined) return dflt;
	const out: Record<string, Level> = {};
	for (const part of v.split(',')) {
		const [fam, lvl] = part.split(':').map((s) => s.trim());
		if (!/^[a-z]+$/.test(fam ?? '') || !['1', '2', '3'].includes(lvl ?? '')) return dflt;
		out[fam] = Number(lvl) as Level;
	}
	return Object.keys(out).length ? out : dflt;
}

/** Hysteresis must leave every ladder a band: below each level1 line and each level2 − level1 gap. */
export function hysteresisBound(thresholds: Record<Window, WindowThresholds>, low: WindowThresholds): number {
	return Math.min(...[thresholds['5h'], thresholds['7d'], low].flatMap((t) => [t.level1, t.level2 - t.level1]));
}

/** Pure: merged raw strings → a validated config; anything unparsable falls back per field. */
export function parseFallbackConfig(raw: RawConfig, dflt: FallbackConfig = DEFAULT_FALLBACK_CONFIG): FallbackConfig {
	const g = (k: string): string | undefined => raw[`${P}${k}`];
	const win = (w: '5H' | '7D', d: { level1: number; level2: number }) => {
		const t = { level1: frac(g(`${w}_LEVEL1`), d.level1), level2: frac(g(`${w}_LEVEL2`), d.level2) };
		return t.level1 < t.level2 ? t : d; // an inverted ladder is a typo, not a policy
	};
	const lowRaw = { level1: frac(g('LOW_LEVEL1'), dflt.lowThresholds.level1), level2: frac(g('LOW_LEVEL2'), dflt.lowThresholds.level2) };
	const low = lowRaw.level1 < lowRaw.level2 ? lowRaw : dflt.lowThresholds;
	const thresholds = { '5h': win('5H', dflt.thresholds['5h']), '7d': win('7D', dflt.thresholds['7d']) };
	const bound = hysteresisBound(thresholds, low);
	const h = frac(g('HYSTERESIS'), dflt.hysteresis);
	const families = parseFamilyLevels(g('FAMILY_LEVELS'), dflt.familyLevels);
	return {
		enabled: flag(g('ENABLED'), dflt.enabled),
		thresholds,
		hysteresis: h < bound ? h : bound / 2, // a band that swallows a line would pin the tier until reset
		projection5h: {
			enabled: flag(g('5H_PROJECTION'), dflt.projection5h.enabled),
			limit: frac(g('5H_PROJECTION_LIMIT'), dflt.projection5h.limit),
			lookbackSec: count(g('5H_PROJECTION_LOOKBACK_SEC'), dflt.projection5h.lookbackSec),
			minSpanSec: count(g('5H_PROJECTION_MIN_SPAN_SEC'), dflt.projection5h.minSpanSec),
			clearSamples: count(g('5H_PROJECTION_CLEAR_SAMPLES'), dflt.projection5h.clearSamples),
			clearAfterSec: count(g('5H_PROJECTION_CLEAR_AFTER_SEC'), dflt.projection5h.clearAfterSec),
			escalateAfterSec: count(g('5H_PROJECTION_ESCALATE_AFTER_SEC'), dflt.projection5h.escalateAfterSec),
		},
		lowPriorityEnabled: flag(g('LOW_PRIORITY'), dflt.lowPriorityEnabled),
		lowThresholds: low,
		level2Model: model(g('LEVEL2_MODEL'), 2, families, dflt.level2Model),
		level3Model: model(g('LEVEL3_MODEL'), 3, families, dflt.level3Model),
		familyLevels: families,
		dmMinIntervalSec: count(g('DM_MIN_INTERVAL_SEC'), dflt.dmMinIntervalSec),
	};
}

function stringMap(obj: unknown): RawConfig {
	const out: RawConfig = {};
	if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return out;
	for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
		if (!k.startsWith(P)) continue;
		if (typeof v === 'string') out[k] = v;
		else if (typeof v === 'number' || typeof v === 'boolean') out[k] = String(v);
	}
	return out;
}

function readJson(path: string): unknown {
	try { return JSON.parse(readFileSync(path, 'utf8')); } catch { return null; }
}

/** The manifest's `config` block, as raw strings. */
export function manifestConfig(manifestPath: string): RawConfig {
	const m = readJson(manifestPath) as { config?: unknown } | null;
	return stringMap(m?.config);
}

/** Pure merge of the three layers, lowest precedence first. */
export function mergeLayers(manifest: RawConfig, override: RawConfig, env: NodeJS.ProcessEnv): RawConfig {
	const out: RawConfig = { ...manifest, ...override };
	for (const k of CONFIG_KEYS) {
		const v = env[k];
		if (typeof v === 'string' && v !== '') out[k] = v;
	}
	return out;
}

export const SKILL_MANIFEST_PATH = join(dirname(fileURLToPath(import.meta.url)), '..', 'manifest.json');

export interface ConfigSource {
	manifestPath: string;
	overridePath: string;
	env: NodeJS.ProcessEnv;
	now?: () => number;
	minReadIntervalMs?: number;
}

/**
 * A reader that re-reads the override file when its mtime moves (checked at
 * most every `minReadIntervalMs`), so an owner adjustment lands without a
 * restart. Manifest and env are read once: they change only with a restart.
 */
export function createConfigReader(src: ConfigSource): () => FallbackConfig {
	const manifest = manifestConfig(src.manifestPath);
	const now = src.now ?? Date.now;
	const interval = src.minReadIntervalMs ?? 2000;
	let lastCheck = -Infinity;
	let lastMtime = -1;
	let cached = parseFallbackConfig(mergeLayers(manifest, stringMap(readJson(src.overridePath)), src.env));
	try { lastMtime = statSync(src.overridePath).mtimeMs; } catch { lastMtime = -1; }
	return () => {
		const t = now();
		if (t - lastCheck < interval) return cached;
		lastCheck = t;
		let mtime: number;
		try { mtime = statSync(src.overridePath).mtimeMs; } catch { mtime = -1; }
		if (mtime !== lastMtime) {
			lastMtime = mtime;
			cached = parseFallbackConfig(mergeLayers(manifest, stringMap(readJson(src.overridePath)), src.env));
		}
		return cached;
	};
}
