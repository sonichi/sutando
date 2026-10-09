/**
 * Quota-aware model fallback — the pure policy behind the credential proxy's
 * request rewrite. No I/O: the proxy feeds it the rate-limit headers it sees
 * and the request body's model, and it answers "which model goes upstream".
 *
 * Levels (1 is the most expensive):
 *   1  Fable 5.1 — only ever used when the user switched to it; never a target.
 *   2  the default model (Opus 5.5) and the rest of the Opus family.
 *   3  the cheap backstop (Sonnet 5); Haiku already sits here.
 *
 * Each window (5h, 7d) holds its own tier; the effective tier is the WORST one.
 *   7d: usage > level1 threshold → tier 2; usage > level2 threshold → tier 3.
 *   5h: projection — if the current burn rate runs the window out before its
 *       reset, tier 2 (then 3 after a dwell if it still runs out); the level2
 *       threshold is a hard line to tier 3 regardless. Without a usable
 *       projection the 5h window falls back to the 7d-style threshold rule.
 * Hysteresis is per window: a threshold tier is left only below
 * (threshold − hysteresis); a projection tier only after the projection has
 * cleared for N samples and a few minutes. A window reset re-evaluates from
 * scratch. A request is never rewritten upward.
 *
 * status == rejected swaps no model (every Claude model shares the unified
 * quota); it records a Codex runtime-switch request instead.
 */

export type Level = 1 | 2 | 3;
export type Priority = 'normal' | 'low';
export type Window = '5h' | '7d';
export const WINDOWS: readonly Window[] = ['5h', '7d'];
export const WINDOW_SPAN_MS: Record<Window, number> = { '5h': 5 * 3600_000, '7d': 7 * 86400_000 };

export interface WindowThresholds {
	level1: number; // usage above this demotes level-1 requests to level 2
	level2: number; // usage above this demotes level-1/2 requests to level 3
}

export interface ProjectionConfig {
	enabled: boolean;
	limit: number;            // projected utilization at reset that counts as "runs out"
	lookbackSec: number;      // burn rate is fitted over samples this recent
	minSpanSec: number;       // fewer than this many seconds of samples → even-pace fallback
	clearSamples: number;     // consecutive clearing samples before a tier is left
	clearAfterSec: number;    // ...and this long since the last failing sample
	escalateAfterSec: number; // dwell at tier 2 before a still-failing projection goes to 3
}

export interface FallbackConfig {
	enabled: boolean;
	thresholds: Record<Window, WindowThresholds>;
	hysteresis: number;
	projection5h: ProjectionConfig;
	lowPriorityEnabled: boolean;
	lowThresholds: WindowThresholds; // same pair for both windows
	level2Model: string;
	level3Model: string;
	familyLevels: Record<string, Level>;
	dmMinIntervalSec: number; // at most one de-escalation DM per this many seconds (one gate for both windows)
}

export const DEFAULT_FALLBACK_CONFIG: FallbackConfig = {
	enabled: true,
	thresholds: {
		'5h': { level1: 0.90, level2: 0.97 },
		'7d': { level1: 0.85, level2: 0.95 },
	},
	hysteresis: 0.03,
	projection5h: {
		enabled: true, limit: 0.98, lookbackSec: 3600, minSpanSec: 300,
		clearSamples: 3, clearAfterSec: 300, escalateAfterSec: 600,
	},
	lowPriorityEnabled: false,
	lowThresholds: { level1: 0.60, level2: 0.85 },
	level2Model: 'claude-opus-5-5',
	level3Model: 'claude-sonnet-5',
	familyLevels: { fable: 1, mythos: 1, opus: 2, sonnet: 3, haiku: 3 },
	dmMinIntervalSec: 1800,
};

export interface Projection {
	projected: number;        // utilization expected at the window's reset
	ratePerHour: number;
	etaFullMs: number | null; // when usage reaches 1.0 at this rate, if before reset
	source: 'slope' | 'even-pace';
}

export interface WindowState {
	tier: Level;
	low_tier: Level;
	utilization: number | null;
	reset: string | null;     // raw reset epoch (seconds) last seen for this window
	tier_since: number;       // epoch ms the current tier was entered
	clear_count: number;      // consecutive samples whose projection cleared
	last_fail_at: number;     // epoch ms of the last failing projection (0 = never)
	projection?: Projection | null;
}

export interface RuntimeSwitch {
	to: 'codex' | 'claude';
	reason: string;
	at: string;
}

export interface Fired {
	window: Window;
	key: 'level1' | 'level2' | 'projection';
	threshold: number; // the line that fired (the projection limit for 'projection')
	eta_full?: number | null;
	projected?: number;
}

export interface FallbackState {
	tier: Level; // 1 = primary (no rewrite), 2, 3
	low_priority_tier: Level;
	windows: Record<Window, WindowState>;
	active_model_map: Record<string, string>; // family → the model it runs on
	since: string;
	reason: string;
	fired?: Fired | null;
	runtime_switch?: RuntimeSwitch | null;
}

export interface QuotaObservation {
	u5: number | null;
	u7: number | null;
	r5: string | null;
	r7: string | null;
	status: string | null;
}

/** One 5h usage sample; `r5` ties it to a window so a reset never bridges two. */
export interface Sample { t: number; u5: number; r5: string | null }

const RL = 'anthropic-ratelimit-unified-';

function num(v: string | undefined): number | null {
	if (v === undefined) return null;
	const n = parseFloat(v);
	return Number.isFinite(n) ? n : null;
}

/** The proxy's quota headers as one observation; absent headers are null. */
export function observationFromHeaders(h: Record<string, string>): QuotaObservation {
	const status = h[`${RL}status`] ?? null;
	const s5 = h[`${RL}5h-status`];
	return {
		u5: num(h[`${RL}5h-utilization`]),
		u7: num(h[`${RL}7d-utilization`]),
		r5: h[`${RL}5h-reset`] ?? null,
		r7: h[`${RL}7d-reset`] ?? null,
		status: status === 'rejected' || s5 === 'rejected' ? 'rejected' : status,
	};
}

export function initialState(nowMs: number): FallbackState {
	const w = (): WindowState => ({
		tier: 1, low_tier: 1, utilization: null, reset: null,
		tier_since: nowMs, clear_count: 0, last_fail_at: 0, projection: null,
	});
	return {
		tier: 1, low_priority_tier: 1,
		windows: { '5h': w(), '7d': w() },
		active_model_map: {}, since: new Date(nowMs).toISOString(), reason: 'primary', fired: null, runtime_switch: null,
	};
}

/** Family + context-length variant of a Claude model id: "claude-fable-5-1[1m]" → fable, "[1m]". */
export function splitModel(model: string): { family: string | null; suffix: string } {
	const m = /^claude-([a-z]+)-[0-9][0-9a-z.-]*(\[[^\]]*\])?$/.exec(model.trim());
	if (!m) return { family: null, suffix: '' };
	return { family: m[1], suffix: m[2] ?? '' };
}

export function modelLevel(model: string, cfg: FallbackConfig): Level | null {
	const { family } = splitModel(model);
	if (!family) return null;
	return cfg.familyLevels[family] ?? null;
}

export function modelForLevel(level: Level, cfg: FallbackConfig): string | null {
	return level === 2 ? cfg.level2Model : level === 3 ? cfg.level3Model : null;
}

/** A target model must be a Claude id whose family sits at exactly that level — a typo is refused, never routed. */
export function validModelForLevel(model: string, level: Level, familyLevels: Record<string, Level>): boolean {
	const { family, suffix } = splitModel(model);
	return family !== null && suffix === '' && familyLevels[family] === level;
}

/** Where a tier is left. Bounded so the band never reaches below the next tier's own line. */
export function clearLine(tier: Level, t: WindowThresholds, hysteresis: number): number {
	if (tier === 3) return Math.max(t.level2 - hysteresis, t.level1);
	return Math.max(t.level1 - hysteresis, 0);
}

/**
 * One window's next tier by thresholds. With hysteresis a tier is left only
 * below its clear line; a reset re-evaluates against the bare lines.
 */
export function windowTier(prev: Level, usage: number, t: WindowThresholds, hysteresis: number, resetObserved: boolean): Level {
	let tier: Level = resetObserved ? 1 : prev;
	while (tier > 1 && usage < clearLine(tier, t, hysteresis)) tier = (tier - 1) as Level;
	if (usage > t.level2) return 3;
	if (usage > t.level1) return tier > 2 ? tier : 2;
	return tier;
}

/** Samples kept for the burn-rate fit: newest `lookbackSec`, oldest first. */
export function pushSample(samples: readonly Sample[], s: Sample, lookbackSec: number): Sample[] {
	const floor = s.t - lookbackSec * 1000;
	return [...samples.filter((x) => x.t >= floor && x.t <= s.t), s];
}

/** quota-history.jsonl rows (`{ts, u5, r5}` in seconds) as samples; foreign rows are skipped. */
export function samplesFromHistoryRows(rows: readonly unknown[], nowMs: number, lookbackSec: number): Sample[] {
	const out: Sample[] = [];
	for (const r of rows) {
		if (!r || typeof r !== 'object') continue;
		const { ts, u5, r5 } = r as { ts?: unknown; u5?: unknown; r5?: unknown };
		if (typeof ts !== 'number' || typeof u5 !== 'number' || !Number.isFinite(ts) || !Number.isFinite(u5)) continue;
		const t = ts * 1000;
		if (t > nowMs || t < nowMs - lookbackSec * 1000) continue;
		out.push({ t, u5, r5: typeof r5 === 'number' ? String(r5) : typeof r5 === 'string' ? r5 : null });
	}
	return out.sort((a, b) => a.t - b.t);
}

/**
 * Where the 5h window lands at its reset if usage keeps its recent pace.
 * Least-squares slope over this window's samples when they span enough time;
 * otherwise the even-pace estimate (usage so far / fraction of window elapsed).
 * Null when neither can be computed.
 */
export function projectFiveHour(samples: readonly Sample[], nowMs: number, u5: number, resetEpochSec: number | null, cfg: ProjectionConfig): Projection | null {
	if (resetEpochSec === null || !Number.isFinite(resetEpochSec)) return null;
	const resetMs = resetEpochSec * 1000;
	const remainingMs = resetMs - nowMs;
	if (remainingMs <= 0) return null;
	const resetKey = String(resetEpochSec);
	const pts = samples
		.filter((s) => s.r5 === resetKey && s.t >= nowMs - cfg.lookbackSec * 1000 && s.t <= nowMs)
		.map((s) => [s.t, s.u5] as const);
	pts.push([nowMs, u5]);
	const span = pts[pts.length - 1][0] - pts[0][0];
	let slope: number | null = null; // utilization per ms
	let source: Projection['source'] = 'slope';
	if (pts.length >= 2 && span >= cfg.minSpanSec * 1000) {
		const n = pts.length;
		const mt = pts.reduce((a, p) => a + p[0], 0) / n;
		const mu = pts.reduce((a, p) => a + p[1], 0) / n;
		const sxx = pts.reduce((a, p) => a + (p[0] - mt) ** 2, 0);
		if (sxx > 0) slope = pts.reduce((a, p) => a + (p[0] - mt) * (p[1] - mu), 0) / sxx;
	}
	if (slope === null) {
		const elapsedMs = WINDOW_SPAN_MS['5h'] - remainingMs;
		if (elapsedMs < cfg.minSpanSec * 1000) return null;
		slope = u5 / elapsedMs;
		source = 'even-pace';
	}
	const rate = Math.max(slope, 0);
	const projected = u5 + rate * remainingMs;
	const etaFullMs = rate > 0 && u5 < 1 ? nowMs + (1 - u5) / rate : (u5 >= 1 ? nowMs : null);
	return {
		projected,
		ratePerHour: rate * 3600_000,
		etaFullMs: etaFullMs !== null && etaFullMs <= resetMs ? etaFullMs : null,
		source,
	};
}

interface TierStep { tier: Level; clear_count: number; last_fail_at: number; tier_since: number }

/**
 * The 5h window under the projection rule. The level2 threshold stays a hard
 * line; otherwise a failing projection raises one tier (with a dwell before 2→3),
 * and a cleared one lowers a tier only after `clearSamples` and `clearAfterSec`.
 */
export function fiveHourTier(ws: WindowState, usage: number, t: WindowThresholds, hysteresis: number, resetObserved: boolean,
	proj: Projection | null, pcfg: ProjectionConfig, nowMs: number): TierStep {
	let tier: Level = resetObserved ? 1 : ws.tier;
	let clear = resetObserved ? 0 : ws.clear_count;
	let lastFail = resetObserved ? 0 : ws.last_fail_at;
	const fails = proj !== null && proj.projected >= pcfg.limit;
	const dwellCleared = (): boolean => clear >= pcfg.clearSamples && nowMs - lastFail >= pcfg.clearAfterSec * 1000;
	if (usage > t.level2) {
		tier = 3; clear = 0;
	} else if (tier === 3 && usage >= clearLine(3, t, hysteresis)) {
		// hard-line hysteresis: hold
	} else if (fails) {
		clear = 0; lastFail = nowMs;
		if (tier < 2) tier = 2;
		else if (tier === 2 && nowMs - ws.tier_since >= pcfg.escalateAfterSec * 1000) tier = 3;
	} else if (proj === null) {
		// No projection: the bare lines may raise at once, but lower only through the same dwell.
		const byThreshold = windowTier(tier, usage, t, hysteresis, false);
		if (byThreshold > tier) { tier = byThreshold; clear = 0; }
		else { clear += 1; if (byThreshold < tier && dwellCleared()) { tier = (tier - 1) as Level; clear = 0; } }
	} else {
		clear += 1;
		if (tier > 1 && dwellCleared()) { tier = (tier - 1) as Level; clear = 0; }
	}
	return { tier, clear_count: clear, last_fail_at: lastFail, tier_since: tier !== ws.tier || resetObserved ? nowMs : ws.tier_since };
}

function activeModelMap(tier: Level, cfg: FallbackConfig): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [family, level] of Object.entries(cfg.familyLevels)) {
		if (level < tier) out[family] = modelForLevel(tier, cfg) ?? '';
	}
	return out;
}

export function nextState(prev: FallbackState | null, obs: QuotaObservation, cfg: FallbackConfig, nowMs: number, samples: readonly Sample[] = []): FallbackState {
	const now = new Date(nowMs).toISOString();
	const base = prev ?? initialState(nowMs);
	const windows: Record<Window, WindowState> = { '5h': { ...base.windows['5h'] }, '7d': { ...base.windows['7d'] } };
	const rejected = obs.status === 'rejected';
	let anyReset = false;
	for (const w of WINDOWS) {
		const usage = w === '5h' ? obs.u5 : obs.u7;
		const reset = w === '5h' ? obs.r5 : obs.r7;
		const ws = windows[w];
		const resetObserved = reset !== null && ws.reset !== null && reset !== ws.reset;
		anyReset = anyReset || resetObserved;
		if (reset !== null) ws.reset = reset;
		if (usage === null) continue;
		ws.utilization = usage;
		// A rejected window swaps no model: hold the tier, record usage only.
		if (rejected) continue;
		if (w === '5h' && cfg.projection5h.enabled) {
			const resetSec = reset !== null ? Number(reset) : null;
			const proj = projectFiveHour(samples, nowMs, usage, Number.isFinite(resetSec as number) ? resetSec : null, cfg.projection5h);
			ws.projection = proj;
			Object.assign(ws, fiveHourTier(ws, usage, cfg.thresholds[w], cfg.hysteresis, resetObserved, proj, cfg.projection5h, nowMs));
		} else {
			const tier = windowTier(ws.tier, usage, cfg.thresholds[w], cfg.hysteresis, resetObserved);
			if (tier !== ws.tier || resetObserved) ws.tier_since = nowMs;
			ws.tier = tier;
		}
		ws.low_tier = windowTier(ws.low_tier, usage, cfg.lowThresholds, cfg.hysteresis, resetObserved);
	}
	const tier = Math.max(windows['5h'].tier, windows['7d'].tier) as Level;
	const lowTier = cfg.lowPriorityEnabled
		? Math.max(tier, windows['5h'].low_tier, windows['7d'].low_tier) as Level
		: tier;

	let runtimeSwitch: RuntimeSwitch | null = base.runtime_switch ?? null;
	if (rejected) {
		if (runtimeSwitch?.to !== 'codex') runtimeSwitch = { to: 'codex', reason: 'rejected', at: now };
	} else if (runtimeSwitch?.to === 'codex') {
		runtimeSwitch = { to: 'claude', reason: anyReset ? 'window reset' : 'allowed again', at: now };
	}

	const changed = tier !== base.tier || lowTier !== base.low_priority_tier;
	const fired = changed ? firingWindow(windows, tier, cfg) : (base.fired ?? null);
	return {
		tier,
		low_priority_tier: lowTier,
		windows,
		active_model_map: activeModelMap(tier, cfg),
		since: changed ? now : base.since,
		reason: changed ? describeTier(windows, tier, fired) : base.reason,
		fired,
		runtime_switch: runtimeSwitch,
	};
}

/** Of the windows at the effective tier, the one that most clearly put it there. */
function firingWindow(windows: Record<Window, WindowState>, tier: Level, cfg: FallbackConfig): Fired | null {
	if (tier === 1) return null;
	let best: Fired | null = null;
	let bestOver = -Infinity;
	for (const w of WINDOWS) {
		const ws = windows[w];
		if (ws.tier !== tier || ws.utilization === null) continue;
		const key = tier === 3 ? 'level2' : 'level1';
		const threshold = cfg.thresholds[w][key];
		const byProjection = w === '5h' && cfg.projection5h.enabled && ws.utilization <= threshold && !!ws.projection
			&& ws.projection.projected >= cfg.projection5h.limit;
		const over = byProjection ? ws.projection!.projected - cfg.projection5h.limit : ws.utilization - threshold;
		if (over > bestOver) {
			bestOver = over;
			best = byProjection
				? { window: w, key: 'projection', threshold: cfg.projection5h.limit, eta_full: ws.projection!.etaFullMs, projected: ws.projection!.projected }
				: { window: w, key, threshold };
		}
	}
	return best;
}

function pct(u: number | null | undefined): string { return u === null || u === undefined ? '?' : `${Math.round(u * 100)}%`; }

function describeTier(windows: Record<Window, WindowState>, tier: Level, fired: Fired | null): string {
	if (tier === 1) return `primary (5h ${pct(windows['5h'].utilization)}, 7d ${pct(windows['7d'].utilization)})`;
	if (!fired) return `tier ${tier}`;
	const ws = windows[fired.window];
	if (fired.key === 'projection') {
		const rate = ws.projection ? `${pct(ws.projection.ratePerHour)}/h` : '?';
		return `5h window ${pct(ws.utilization)} at ${rate} projects to ${pct(fired.projected)} at reset (limit ${pct(fired.threshold)})`;
	}
	return `${fired.window} window ${pct(ws.utilization)} > ${fired.key} threshold ${pct(fired.threshold)}`;
}

/** True when anything a reader or the owner cares about moved. */
export function stateChanged(a: FallbackState | null, b: FallbackState): boolean {
	if (!a) return true;
	return a.tier !== b.tier || a.low_priority_tier !== b.low_priority_tier
		|| (a.runtime_switch?.to ?? null) !== (b.runtime_switch?.to ?? null);
}

export interface ModelDecision {
	model: string;
	rewritten: boolean;
	fromLevel: Level | null;
	toLevel: Level | null;
}

/** The model to send upstream for `requested`. Never rewrites upward or across families it does not know. */
export function decideModel(requested: string, state: FallbackState | null, cfg: FallbackConfig, priority: Priority = 'normal'): ModelDecision {
	const keep: ModelDecision = { model: requested, rewritten: false, fromLevel: null, toLevel: null };
	if (!cfg.enabled || !state) return keep;
	const level = modelLevel(requested, cfg);
	if (level === null) return keep;
	const tier = priority === 'low' && cfg.lowPriorityEnabled ? state.low_priority_tier : state.tier;
	if (level >= tier) return { ...keep, fromLevel: level, toLevel: level };
	const target = modelForLevel(tier, cfg);
	if (!target) return keep;
	const { suffix } = splitModel(requested);
	return { model: `${target}${suffix}`, rewritten: true, fromLevel: level, toLevel: tier };
}

/** The request body with its top-level `model` replaced; unparsable bodies come back untouched. */
export function rewriteModel(body: Buffer, model: string): Buffer {
	try {
		const parsed = JSON.parse(body.toString('utf8'));
		if (!parsed || typeof parsed !== 'object' || typeof parsed.model !== 'string') return body;
		parsed.model = model;
		return Buffer.from(JSON.stringify(parsed), 'utf8');
	} catch {
		return body;
	}
}

/** `x-sutando-priority: low` marks traffic the low-priority ladder may demote early. */
export function requestPriority(headers: Record<string, string | string[] | undefined>): Priority {
	const v = headers['x-sutando-priority'];
	return String(Array.isArray(v) ? v[0] : v ?? '').trim().toLowerCase() === 'low' ? 'low' : 'normal';
}

function hhmm(ms: number): string {
	const d = new Date(ms);
	return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** The one owner-DM line for a transition; null when nothing the owner would act on changed. */
export function transitionLine(prev: FallbackState | null, next: FallbackState, cfg: FallbackConfig): string | null {
	const prevSwitch = prev?.runtime_switch?.to ?? null;
	const nextSwitch = next.runtime_switch?.to ?? null;
	if (prevSwitch !== nextSwitch && nextSwitch === 'codex') {
		return 'Claude quota rejected (status=rejected): a switch to the Codex runtime has been requested and recorded. '
			+ 'This version only signals it — nothing restarts on its own; switch manually (core.runtime=codex, then `bash src/agent/start-cli.sh --restart`).';
	}
	if (prevSwitch !== nextSwitch && nextSwitch === 'claude') {
		return 'Claude quota allowed again — the Codex runtime-switch request has been withdrawn.';
	}
	const prevTier = prev?.tier ?? 1;
	if (next.tier === prevTier) return null;
	if (next.tier === 1) {
		return `Quota eased (5h ${pct(next.windows['5h'].utilization)}, 7d ${pct(next.windows['7d'].utilization)}) — back on the primary models.`;
	}
	const f = next.fired;
	if (!f) return `Quota fallback moved to tier ${next.tier}.`;
	const what = next.tier === 3 ? `Fable and Opus requests now run on ${cfg.level3Model}` : `Fable requests now run on ${cfg.level2Model}`;
	if (f.key === 'projection') {
		const eta = f.eta_full !== null && f.eta_full !== undefined ? `100% expected at ${hhmm(f.eta_full)}` : `${pct(f.projected)} expected at the reset`;
		return `Quota 5h window: at the current burn rate it runs out before the reset (${eta}) — ${what}; `
			+ 'reverts once the rate slows (adjust with `fallback-config set 5h projection-limit <0..1>`).';
	}
	const usage = pct(next.windows[f.window].utilization);
	const back = pct(clearLine(next.tier, cfg.thresholds[f.window], cfg.hysteresis));
	const adjust = `fallback-config set ${f.window} ${f.key} ${(f.threshold + 0.02).toFixed(2)}`;
	return `Quota ${f.window} window ${usage} is over the ${pct(f.threshold)} line — ${what}; reverts below ${back} (move the line with \`${adjust}\`).`;
}

/**
 * Owner-DM rate gate. The effective tier is one number, so the gate is one
 * shared gate: escalations, recovery to the primary tier and runtime switches
 * always go out and clear whatever was held; only a de-escalation (tier 3 → 2)
 * is held inside the interval, and a held line is flushed when it elapses.
 */
export type DmKind = 'escalation' | 'recovery' | 'runtime' | 'lateral';

export interface DmGate {
	last_sent: number | null;
	held: string[];
}

export const EMPTY_DM_GATE: DmGate = { last_sent: null, held: [] };

function withSummary(line: string, held: string[]): string {
	return held.length ? `${line} [${held.length} earlier change(s) held since the last message: ${held.join(' | ')}]` : line;
}

export function gateLine(gate: DmGate, kind: DmKind, line: string, nowMs: number, minIntervalMs: number): { gate: DmGate; send: string | null } {
	const inWindow = gate.last_sent !== null && nowMs - gate.last_sent < minIntervalMs;
	if (kind === 'lateral' && minIntervalMs > 0 && inWindow) {
		return { gate: { ...gate, held: [...gate.held, line] }, send: null };
	}
	return { gate: { last_sent: nowMs, held: [] }, send: withSummary(line, gate.held) };
}

/** The held lines once the interval has elapsed: the latest goes out, the rest are summarised into it. */
export function flushHeld(gate: DmGate, nowMs: number, minIntervalMs: number): { gate: DmGate; send: string | null } {
	if (!gate.held.length || nowMs - (gate.last_sent ?? 0) < minIntervalMs) return { gate, send: null };
	return { gate: { last_sent: nowMs, held: [] }, send: withSummary(gate.held[gate.held.length - 1], gate.held.slice(0, -1)) };
}

/** When the held line becomes due, or null when nothing is held. */
export function nextFlushAt(gate: DmGate, minIntervalMs: number): number | null {
	return gate.held.length ? (gate.last_sent ?? 0) + minIntervalMs : null;
}
