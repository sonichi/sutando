/**
 * The model-fallback policy, offline: every tier boundary, per-window
 * hysteresis, the 5h projection rule, the rejected → Codex-request path, the
 * low-priority ladder (off by default), and never-upgrade. No proxy, no I/O.
 */
import { test } from 'node:test';
import assert from 'node:assert';
import {
	DEFAULT_FALLBACK_CONFIG as D, clearLine, decideModel, fiveHourTier, flushHeld, gateLine, initialState, modelLevel, nextFlushAt, nextState,
	observationFromHeaders, projectFiveHour, pushSample, requestPriority, rewriteModel, samplesFromHistoryRows,
	splitModel, stateChanged, transitionLine, validModelForLevel, windowTier,
	type DmGate, type FallbackConfig, type FallbackState, type QuotaObservation, type Sample,
} from '../skills/quota-tracker/scripts/quota-fallback-policy.ts';

const T0 = Date.UTC(2026, 9, 9, 0, 0, 0);
const H = 3600_000;
const R5 = String(Math.floor((T0 + 2 * H) / 1000)); // 5h window resets 2h from T0
const R7 = String(Math.floor((T0 + 3 * 86400_000) / 1000));

const noProjection: FallbackConfig = { ...D, projection5h: { ...D.projection5h, enabled: false } };
const obs = (u5: number | null, u7: number | null, status = 'allowed', r5 = R5, r7 = R7): QuotaObservation => ({ u5, u7, status, r5, r7 });

test('shipped defaults: 5h 0.90/0.97 with projection on, 7d 0.85/0.95, hysteresis 0.03, low-priority off', () => {
	assert.deepStrictEqual(D.thresholds['5h'], { level1: 0.90, level2: 0.97 });
	assert.deepStrictEqual(D.thresholds['7d'], { level1: 0.85, level2: 0.95 });
	assert.strictEqual(D.hysteresis, 0.03);
	assert.strictEqual(D.projection5h.enabled, true);
	assert.strictEqual(D.projection5h.limit, 0.98);
	assert.strictEqual(D.lowPriorityEnabled, false);
	assert.deepStrictEqual(D.lowThresholds, { level1: 0.60, level2: 0.85 });
	assert.strictEqual(D.level2Model, 'claude-opus-5-5');
	assert.strictEqual(D.level3Model, 'claude-sonnet-5');
	assert.strictEqual(D.dmMinIntervalSec, 1800);
});

test('a target model must be a Claude id whose family sits at exactly its level', () => {
	assert.strictEqual(validModelForLevel('claude-opus-5-5', 2, D.familyLevels), true);
	assert.strictEqual(validModelForLevel('claude-sonnet-5', 3, D.familyLevels), true);
	assert.strictEqual(validModelForLevel('claude-opus-5-5', 3, D.familyLevels), false, 'wrong level');
	assert.strictEqual(validModelForLevel('claude-opsu-5-5', 2, D.familyLevels), false, 'typo family');
	assert.strictEqual(validModelForLevel('gpt-5', 3, D.familyLevels), false);
	assert.strictEqual(validModelForLevel('claude-opus-5-5[1m]', 2, D.familyLevels), false, 'the variant comes from the request, not the config');
});

test('hysteresis is bounded: a tier\'s clear line never sinks below the next tier\'s own line', () => {
	const tight = { level1: 0.90, level2: 0.92 };
	assert.strictEqual(clearLine(3, tight, 0.03), 0.90, 'level2 − h would be 0.89, under the level1 line');
	assert.ok(Math.abs(clearLine(3, D.thresholds['7d'], 0.03) - 0.92) < 1e-9);
	assert.strictEqual(clearLine(2, { level1: 0.02, level2: 0.5 }, 0.03), 0);
	assert.strictEqual(windowTier(3, 0.905, tight, 0.03, false), 3, 'inside the bounded band: holds');
	assert.strictEqual(windowTier(3, 0.895, tight, 0.03, false), 2, 'under the level1 line: tier 3 is left even though 0.895 ≥ 0.89');
	assert.strictEqual(windowTier(2, 0.875, tight, 0.03, false), 2, 'tier 2 keeps its own band down to 0.87');
	assert.strictEqual(windowTier(2, 0.865, tight, 0.03, false), 1);
});

test('DM gate: escalation, recovery and runtime lines always go out; only a de-escalation is held, and it flushes on its own', () => {
	const I = 1800_000;
	let g: DmGate = { last_sent: {}, held: {} };
	let r = gateLine(g, '7d', 'escalation', 'up to 2', T0, I); g = r.gate;
	assert.strictEqual(r.send, 'up to 2');
	r = gateLine(g, '7d', 'escalation', 'up to 3', T0 + 60_000, I); g = r.gate;
	assert.strictEqual(r.send, 'up to 3', 'an escalation inside the interval is never held');
	r = gateLine(g, '7d', 'lateral', 'down to 2', T0 + 120_000, I); g = r.gate;
	assert.strictEqual(r.send, null, 'a de-escalation inside the interval is held');
	assert.strictEqual(nextFlushAt(g, I), T0 + 60_000 + I, 'due when the window that started at the last sent line elapses');
	r = gateLine(g, '7d', 'recovery', 'back to primary', T0 + 180_000, I); g = r.gate;
	assert.strictEqual(r.send, 'back to primary [1 earlier change(s) held since the last message: down to 2]', 'recovery is never held and carries the held line');
	assert.strictEqual(nextFlushAt(g, I), null);
	r = gateLine(g, 'runtime', 'runtime', 'codex', T0 + 181_000, I); g = r.gate;
	assert.strictEqual(r.send, 'codex');
	// Timer flush: a held line with no later event goes out when the interval elapses.
	r = gateLine(g, '7d', 'escalation', 'up to 3 again', T0 + 200_000, I); g = r.gate;
	r = gateLine(g, '7d', 'lateral', 'down A', T0 + 210_000, I); g = r.gate;
	r = gateLine(g, '7d', 'lateral', 'down B', T0 + 220_000, I); g = r.gate;
	assert.strictEqual(r.send, null);
	let f = flushHeld(g, T0 + 200_000 + I - 1, I);
	assert.deepStrictEqual(f.send, [], 'not due yet');
	f = flushHeld(g, T0 + 200_000 + I, I); g = f.gate;
	assert.deepStrictEqual(f.send, ['down B [1 earlier change(s) held since the last message: down A]'], 'the latest held line goes out, the rest summarised');
	assert.strictEqual(nextFlushAt(g, I), null, 'flushed');
	assert.strictEqual(gateLine(g, '7d', 'lateral', 'C', T0 + 200_000 + I + 1, 0).send, 'C', 'interval 0 disables the gate');
});

test('model levels by family; the [1m] variant is preserved as a suffix; unknown models have no level', () => {
	assert.deepStrictEqual(splitModel('claude-fable-5-1[1m]'), { family: 'fable', suffix: '[1m]' });
	assert.strictEqual(modelLevel('claude-fable-5-1', D), 1);
	assert.strictEqual(modelLevel('claude-mythos-5-1', D), 1);
	assert.strictEqual(modelLevel('claude-opus-5-5', D), 2);
	assert.strictEqual(modelLevel('claude-opus-4-7', D), 2);
	assert.strictEqual(modelLevel('claude-sonnet-5', D), 3);
	assert.strictEqual(modelLevel('claude-haiku-4-5', D), 3);
	assert.strictEqual(modelLevel('gpt-5', D), null);
	assert.strictEqual(modelLevel('', D), null);
});

test('windowTier: boundaries are strict (> not >=) and hysteresis is threshold − 0.03', () => {
	const t = D.thresholds['7d'];
	assert.strictEqual(windowTier(1, 0.85, t, 0.03, false), 1, 'exactly at the line is not over it');
	assert.strictEqual(windowTier(1, 0.851, t, 0.03, false), 2);
	assert.strictEqual(windowTier(1, 0.95, t, 0.03, false), 2);
	assert.strictEqual(windowTier(1, 0.951, t, 0.03, false), 3);
	assert.strictEqual(windowTier(3, 0.93, t, 0.03, false), 3, 'tier 3 holds down to 0.92');
	assert.strictEqual(windowTier(3, 0.919, t, 0.03, false), 2, 'below 0.92 drops to tier 2');
	assert.strictEqual(windowTier(2, 0.83, t, 0.03, false), 2, 'tier 2 holds down to 0.82');
	assert.strictEqual(windowTier(2, 0.819, t, 0.03, false), 1, 'below 0.82 returns to primary');
	assert.strictEqual(windowTier(3, 0.50, t, 0.03, false), 1, 'a deep drop leaves every tier at once');
	assert.strictEqual(windowTier(3, 0.93, t, 0.03, true), 2, 'a reset re-evaluates against the bare lines');
});

test('7d-only crossing: 7d 86% with a quiet 5h window → tier 2, fired by the 7d level1 line', () => {
	const s = nextState(null, obs(0.30, 0.86), noProjection, T0);
	assert.strictEqual(s.tier, 2);
	assert.deepStrictEqual(s.fired, { window: '7d', key: 'level1', threshold: 0.85 });
	assert.deepStrictEqual(s.active_model_map, { fable: 'claude-opus-5-5', mythos: 'claude-opus-5-5' });
	assert.match(s.reason, /7d window 86% > level1 threshold 85%/);
});

test('5h-only crossing (threshold rule): 5h 91% with a quiet 7d window → tier 2, fired by the 5h level1 line', () => {
	const s = nextState(null, obs(0.91, 0.30), noProjection, T0);
	assert.strictEqual(s.tier, 2);
	assert.deepStrictEqual(s.fired, { window: '5h', key: 'level1', threshold: 0.90 });
});

test('5h-only hard line under the default (projection) config: 98% → tier 3 regardless of projection', () => {
	const s = nextState(null, obs(0.98, 0.30), D, T0);
	assert.strictEqual(s.tier, 3);
	assert.strictEqual(s.fired?.window, '5h');
	assert.strictEqual(s.fired?.key, 'level2');
});

test('per-window overrides: a laxer 5h line keeps 5h 91% primary while 7d keeps its own line', () => {
	const cfg: FallbackConfig = { ...noProjection, thresholds: { '5h': { level1: 0.95, level2: 0.99 }, '7d': { level1: 0.80, level2: 0.95 } } };
	assert.strictEqual(nextState(null, obs(0.91, 0.30), cfg, T0).tier, 1);
	assert.strictEqual(nextState(null, obs(0.30, 0.81), cfg, T0).tier, 2);
	assert.strictEqual(nextState(null, obs(0.96, 0.30), cfg, T0).tier, 2);
});

test('the effective tier is the worst window, and each window reverts on its own hysteresis', () => {
	let s = nextState(null, obs(0.96, 0.86), noProjection, T0);
	assert.strictEqual(s.tier, 2);
	s = nextState(s, obs(0.98, 0.86), noProjection, T0 + 1000);
	assert.strictEqual(s.tier, 3);
	s = nextState(s, obs(0.93, 0.86), noProjection, T0 + 2000);
	assert.strictEqual(s.tier, 2, '5h left tier 3 below 0.94; 7d still holds tier 2');
	s = nextState(s, obs(0.50, 0.83), noProjection, T0 + 3000);
	assert.strictEqual(s.tier, 2, '7d holds tier 2 down to 0.82');
	s = nextState(s, obs(0.50, 0.81), noProjection, T0 + 4000);
	assert.strictEqual(s.tier, 1);
	assert.match(s.reason, /^primary/);
});

test('a window reset returns to the primary model even though usage sits inside the hysteresis band', () => {
	let s = nextState(null, obs(0.30, 0.86), noProjection, T0);
	assert.strictEqual(s.tier, 2);
	s = nextState(s, obs(0.30, 0.84), noProjection, T0 + 1000);
	assert.strictEqual(s.tier, 2, 'inside the band: held');
	const r7next = String(Number(R7) + 7 * 86400);
	s = nextState(s, obs(0.30, 0.84, 'allowed', R5, r7next), noProjection, T0 + 2000);
	assert.strictEqual(s.tier, 1, 'the reset epoch moved: fresh evaluation');
});

test('decideModel: level-1 requests go to the tier model with their variant; never upward; unknown left alone', () => {
	const tier2 = nextState(null, obs(0.30, 0.86), noProjection, T0);
	assert.deepStrictEqual(decideModel('claude-fable-5-1[1m]', tier2, D), { model: 'claude-opus-5-5[1m]', rewritten: true, fromLevel: 1, toLevel: 2 });
	assert.strictEqual(decideModel('claude-opus-5-5', tier2, D).rewritten, false, 'already at the tier');
	assert.strictEqual(decideModel('claude-sonnet-5', tier2, D).rewritten, false, 'below the tier is never raised');
	const tier3 = nextState(null, obs(0.30, 0.96), noProjection, T0);
	assert.strictEqual(decideModel('claude-fable-5-1', tier3, D).model, 'claude-sonnet-5');
	assert.strictEqual(decideModel('claude-opus-5-5[1m]', tier3, D).model, 'claude-sonnet-5[1m]');
	assert.strictEqual(decideModel('claude-haiku-4-5', tier3, D).rewritten, false);
	assert.strictEqual(decideModel('gpt-5', tier3, D).rewritten, false);
	assert.strictEqual(decideModel('claude-fable-5-1', tier3, { ...D, enabled: false }).rewritten, false, 'disabled: pass-through');
	assert.strictEqual(decideModel('claude-fable-5-1', null, D).rewritten, false, 'no observation yet: pass-through');
});

test('rejected: no model swap; a Codex runtime switch is requested once and withdrawn when allowed again', () => {
	const s = nextState(null, obs(0.99, 0.99, 'rejected'), noProjection, T0);
	assert.strictEqual(s.tier, 1, 'every Claude model shares the rejected quota — nothing to swap to');
	assert.deepStrictEqual(s.runtime_switch, { to: 'codex', reason: 'rejected', at: new Date(T0).toISOString() });
	assert.match(transitionLine(null, s, D)!, /^Claude quota rejected \(status=rejected\): a switch to the Codex runtime has been requested/);
	assert.match(transitionLine(null, s, D)!, /nothing restarts on its own; switch manually \(core\.runtime=codex, then `bash src\/agent\/start-cli\.sh --restart`\)/);
	const held = nextState(s, obs(0.99, 0.99, 'rejected'), noProjection, T0 + 60_000);
	assert.strictEqual(held.runtime_switch?.at, s.runtime_switch?.at, 'the request is not re-stamped every response');
	assert.strictEqual(transitionLine(s, held, D), null);
	const back = nextState(held, obs(0.10, 0.50, 'allowed', String(Number(R5) + 18000), R7), noProjection, T0 + 120_000);
	assert.deepStrictEqual(back.runtime_switch, { to: 'claude', reason: 'window reset', at: new Date(T0 + 120_000).toISOString() });
	assert.strictEqual(transitionLine(held, back, D), 'Claude quota allowed again — the Codex runtime-switch request has been withdrawn.');
	assert.strictEqual(observationFromHeaders({ 'anthropic-ratelimit-unified-5h-status': 'rejected' }).status, 'rejected');
	assert.strictEqual(observationFromHeaders({ 'anthropic-ratelimit-unified-status': 'allowed_warning' }).status, 'allowed_warning');
});

test('low-priority ladder: off by default; on, it demotes marked traffic from 0.60/0.85 without touching normal traffic', () => {
	const off = nextState(null, obs(0.30, 0.70), noProjection, T0);
	assert.strictEqual(off.low_priority_tier, 1);
	assert.strictEqual(decideModel('claude-fable-5-1', off, noProjection, 'low').rewritten, false);
	const on: FallbackConfig = { ...noProjection, lowPriorityEnabled: true };
	let s = nextState(null, obs(0.30, 0.61), on, T0);
	assert.strictEqual(s.tier, 1);
	assert.strictEqual(s.low_priority_tier, 2);
	assert.strictEqual(decideModel('claude-fable-5-1', s, on, 'low').model, 'claude-opus-5-5');
	assert.strictEqual(decideModel('claude-fable-5-1', s, on, 'normal').rewritten, false);
	s = nextState(s, obs(0.30, 0.86), on, T0 + 1000);
	assert.strictEqual(s.tier, 2);
	assert.strictEqual(s.low_priority_tier, 3, 'low priority is never laxer than normal');
	assert.strictEqual(decideModel('claude-opus-5-5', s, on, 'low').model, 'claude-sonnet-5');
	assert.strictEqual(requestPriority({ 'x-sutando-priority': 'low' }), 'low');
	assert.strictEqual(requestPriority({ 'x-sutando-priority': 'LOW ' }), 'low');
	assert.strictEqual(requestPriority({}), 'normal');
});

// --- 5h projection -------------------------------------------------------

function ramp(from: number, to: number, minutes: number, endMs: number, r5 = R5): Sample[] {
	let s: Sample[] = [];
	for (let i = 0; i <= minutes; i += 5) {
		const t = endMs - (minutes - i) * 60_000;
		s = pushSample(s, { t, u5: from + (to - from) * (i / minutes), r5 }, 3600);
	}
	return s;
}

test('projection: a burn rate that runs out before the reset downgrades at 70%', () => {
	const samples = ramp(0.40, 0.70, 30, T0); // 0.6/h with 2h left → ~1.9 at reset
	const p = projectFiveHour(samples, T0, 0.70, Number(R5), D.projection5h)!;
	assert.strictEqual(p.source, 'slope');
	assert.ok(p.projected > 1.5, `projected ${p.projected}`);
	assert.ok(p.etaFullMs !== null && p.etaFullMs > T0 && p.etaFullMs < T0 + 2 * H, 'runs out inside the window');
	const s = nextState(null, obs(0.70, 0.30), D, T0, samples);
	assert.strictEqual(s.tier, 2);
	assert.strictEqual(s.fired?.key, 'projection');
	assert.match(s.reason, /5h window 70% at 60%\/h projects to/);
	const line = transitionLine(null, s, D)!;
	assert.match(line, /^Quota 5h window: at the current burn rate it runs out before the reset \(100% expected at \d\d:\d\d\) — Fable requests now run on claude-opus-5-5; reverts once the rate slows/);
	assert.match(line, /fallback-config set 5h projection-limit <0\.\.1>/);
});

test('projection: a slow rate at 92% that lasts to the reset does not downgrade, even above the 90% line', () => {
	// 0.04/h with 1h left → ~0.96 at reset: lasts.
	const r5 = String(Math.floor((T0 + H) / 1000));
	const near = ramp(0.90, 0.92, 30, T0, r5);
	const p = projectFiveHour(near, T0, 0.92, Number(r5), D.projection5h)!;
	assert.ok(p.projected < 0.98, `projected ${p.projected}`);
	const s = nextState(null, obs(0.92, 0.30, 'allowed', r5), D, T0, near);
	assert.strictEqual(s.tier, 1, 'lasts to the reset: stay on the primary');
});

test('projection: the 0.97 hard line downgrades to the level-3 model whatever the projection says', () => {
	const flat = ramp(0.975, 0.975, 30, T0);
	const s = nextState(null, obs(0.975, 0.30), D, T0, flat);
	assert.strictEqual(s.tier, 3);
	assert.strictEqual(s.fired?.key, 'level2');
	assert.strictEqual(decideModel('claude-opus-5-5', s, D).model, 'claude-sonnet-5');
});

test('projection: with no projection a tier set earlier is lowered only through the dwell, never dropped at once', () => {
	const pc = D.projection5h;
	const t = D.thresholds['5h'];
	const w0 = initialState(T0).windows['5h'];
	const fail = { projected: 1.2, ratePerHour: 0.5, etaFullMs: T0 + H, source: 'slope' as const };
	let ws = { ...w0, ...fiveHourTier(w0, 0.60, t, 0.03, false, fail, pc, T0) };
	assert.strictEqual(ws.tier, 2, 'projection set tier 2');
	ws = { ...ws, ...fiveHourTier(ws, 0.60, t, 0.03, false, null, pc, T0 + 10_000) };
	assert.strictEqual(ws.tier, 2, 'the projection went null (thin history): the bare 0.90 line alone must not drop the tier');
	let now = T0 + 10_000;
	for (let i = 0; i < pc.clearSamples; i++) { now += 10_000; ws = { ...ws, ...fiveHourTier(ws, 0.60, t, 0.03, false, null, pc, now) }; }
	assert.strictEqual(ws.tier, 2, 'N null samples but the quiet period has not elapsed');
	ws = { ...ws, ...fiveHourTier(ws, 0.60, t, 0.03, false, null, pc, T0 + pc.clearAfterSec * 1000 + 1000) };
	assert.strictEqual(ws.tier, 1, 'dwell satisfied: lowered one level');
	ws = { ...ws, ...fiveHourTier(ws, 0.98, t, 0.03, false, null, pc, T0 + pc.clearAfterSec * 1000 + 2000) };
	assert.strictEqual(ws.tier, 3, 'a bare line still raises at once');
});

test('projection: thin history falls back to even pace, and too-young a window falls back to the threshold rule', () => {
	// 4h elapsed of 5h at 85% → even pace lands at 1.06: runs out.
	const r5 = String(Math.floor((T0 + H) / 1000));
	const p = projectFiveHour([], T0, 0.85, Number(r5), D.projection5h)!;
	assert.strictEqual(p.source, 'even-pace');
	assert.ok(p.projected > 1.0 && p.projected < 1.1, `projected ${p.projected}`);
	assert.strictEqual(nextState(null, obs(0.85, 0.30, 'allowed', r5), D, T0).tier, 2);
	// 4h elapsed at 50% → 0.625: lasts.
	assert.strictEqual(nextState(null, obs(0.50, 0.30, 'allowed', r5), D, T0).tier, 1);
	// 2 minutes into a window: no projection; the bare 5h thresholds decide.
	const young = String(Math.floor((T0 + 5 * H - 120_000) / 1000));
	assert.strictEqual(projectFiveHour([], T0, 0.91, Number(young), D.projection5h), null);
	assert.strictEqual(nextState(null, obs(0.91, 0.30, 'allowed', young), D, T0).tier, 2);
	assert.strictEqual(nextState(null, obs(0.89, 0.30, 'allowed', young), D, T0).tier, 1);
	// No reset header at all: no projection either.
	assert.strictEqual(projectFiveHour([], T0, 0.5, null, D.projection5h), null);
});

test('projection: samples from another window never feed the fit', () => {
	const stale = ramp(0.10, 0.90, 30, T0, String(Number(R5) - 18000));
	const p = projectFiveHour(stale, T0, 0.20, Number(R5), D.projection5h)!;
	assert.strictEqual(p.source, 'even-pace', 'only the current window counts');
});

test('projection hysteresis: tier 2 escalates to 3 after the dwell if it still runs out, and reverts only after N clearing samples and a quiet period', () => {
	const pc = D.projection5h;
	const t = D.thresholds['5h'];
	const w0 = initialState(T0).windows['5h'];
	const fail = { projected: 1.2, ratePerHour: 0.5, etaFullMs: T0 + H, source: 'slope' as const };
	const clear = { projected: 0.8, ratePerHour: 0.05, etaFullMs: null, source: 'slope' as const };
	let ws = { ...w0, ...fiveHourTier(w0, 0.60, t, 0.03, false, fail, pc, T0) };
	assert.strictEqual(ws.tier, 2);
	ws = { ...ws, ...fiveHourTier(ws, 0.65, t, 0.03, false, fail, pc, T0 + 300_000) };
	assert.strictEqual(ws.tier, 2, 'inside the dwell: no escalation yet');
	ws = { ...ws, ...fiveHourTier(ws, 0.70, t, 0.03, false, fail, pc, T0 + pc.escalateAfterSec * 1000) };
	assert.strictEqual(ws.tier, 3, 'still running out after the dwell: one more level');
	let now = T0 + pc.escalateAfterSec * 1000;
	for (let i = 0; i < pc.clearSamples - 1; i++) {
		now += 10_000;
		ws = { ...ws, ...fiveHourTier(ws, 0.70, t, 0.03, false, clear, pc, now) };
		assert.strictEqual(ws.tier, 3, `clearing sample ${i + 1}: not enough yet`);
	}
	now += 10_000;
	ws = { ...ws, ...fiveHourTier(ws, 0.70, t, 0.03, false, clear, pc, now) };
	assert.strictEqual(ws.tier, 3, 'N samples but the quiet period has not elapsed');
	now = T0 + pc.escalateAfterSec * 1000 + pc.clearAfterSec * 1000;
	ws = { ...ws, ...fiveHourTier(ws, 0.70, t, 0.03, false, clear, pc, now) };
	assert.strictEqual(ws.tier, 2, 'N samples and the quiet period: one level back');
	assert.strictEqual(ws.clear_count, 0, 'the count restarts for the next level');
});

test('history rows seed the samples: seconds → ms, foreign/old rows skipped, oldest first', () => {
	const rows = [
		{ ts: (T0 - 10 * 60_000) / 1000, u5: 0.5, r5: Number(R5), u7: 0.3, r7: Number(R7) },
		{ ts: (T0 - 20 * 60_000) / 1000, u5: 0.4, r5: Number(R5) },
		{ ts: (T0 - 2 * H) / 1000, u5: 0.1, r5: Number(R5) }, // outside the lookback
		{ ts: 'bad', u5: 0.1 }, null, 42,
	];
	const s = samplesFromHistoryRows(rows, T0, 3600);
	assert.deepStrictEqual(s, [{ t: T0 - 20 * 60_000, u5: 0.4, r5: R5 }, { t: T0 - 10 * 60_000, u5: 0.5, r5: R5 }]);
});

test('rewriteModel swaps only the top-level model; non-JSON and model-less bodies come back untouched', () => {
	const body = Buffer.from('{"model":"claude-fable-5-1","messages":[{"role":"user","content":"hi"}],"max_tokens":8}');
	const out = JSON.parse(rewriteModel(body, 'claude-opus-5-5').toString());
	assert.strictEqual(out.model, 'claude-opus-5-5');
	assert.deepStrictEqual(out.messages, [{ role: 'user', content: 'hi' }]);
	assert.strictEqual(out.max_tokens, 8);
	assert.strictEqual(rewriteModel(Buffer.from('not json'), 'x').toString(), 'not json');
	assert.strictEqual(rewriteModel(Buffer.from('{"messages":[]}'), 'x').toString(), '{"messages":[]}');
});

test('stateChanged and transitionLine fire once per tier change, not per response', () => {
	const a = nextState(null, obs(0.30, 0.86), noProjection, T0);
	const b = nextState(a, obs(0.30, 0.87), noProjection, T0 + 1000);
	assert.strictEqual(stateChanged(null, a), true);
	assert.strictEqual(stateChanged(a, b), false);
	assert.strictEqual(transitionLine(a, b, D), null);
	assert.strictEqual(transitionLine(null, a, D),
		'Quota 7d window 86% is over the 85% line — Fable requests now run on claude-opus-5-5; reverts below 82% (move the line with `fallback-config set 7d level1 0.87`).');
	const c = nextState(b, obs(0.30, 0.50), noProjection, T0 + 2000);
	assert.strictEqual(transitionLine(b, c, D), 'Quota eased (5h 30%, 7d 50%) — back on the primary models.');
	const d: FallbackState = nextState(null, obs(0.30, 0.96), noProjection, T0);
	assert.strictEqual(transitionLine(null, d, D),
		'Quota 7d window 96% is over the 95% line — Fable and Opus requests now run on claude-sonnet-5; reverts below 92% (move the line with `fallback-config set 7d level2 0.97`).');
	for (const l of [transitionLine(null, a, D)!, transitionLine(null, d, D)!]) assert.doesNotMatch(l, /[\u4e00-\u9fff]/, 'owner DM lines are English');
});
