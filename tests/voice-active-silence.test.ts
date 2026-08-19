/**
 * ACTIVE-silence recovery reducer — the normative transition table from
 * design-voice-active-silence-recovery.md, row by row, plus its named cases
 * (the 2026-08-17 incident; ask-once-then-wait; reload-mid-hang analogues).
 * Pure-function tests; the wiring feeds events and executes effects only.
 */
import {
	DEFAULT_ACTIVE_SILENCE_TICKS, EPISODE_ATTEMPT_LIMIT, MIN_ACTIVE_SILENCE_TICKS,
	initialRecoveryState, parseActiveSilenceMode, parseActiveSilenceTicks,
	reduceRecovery, type RecoveryEvent, type RecoveryState,
} from '../src/voice-active-silence-watchdog.js';

let failed = 0;
function check(name: string, got: unknown, want: unknown): void {
	const ok = JSON.stringify(got) === JSON.stringify(want);
	console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}`);
	if (!ok) { console.log(`       got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); failed++; }
}

const FACTS_OK = {
	factsAvailable: true, speechInWindow: true, speechObservedAt: 1_000,
	ingressAdvanced: true, modelSilentFor15s: true,
};
const T = 30_000; // tick spacing

function ev(e: Partial<RecoveryEvent> & { kind: RecoveryEvent['kind'] }): RecoveryEvent {
	return e as RecoveryEvent;
}
function tick(at: number, over: Record<string, unknown> = {}): RecoveryEvent {
	return ev({ kind: 'tick', at, state: 'ACTIVE', facts: { ...FACTS_OK }, pendingToolCount: 0, ...over });
}

/** Drive a fresh state to attached + active + latched speech at t0. */
function armed(t0: number): RecoveryState {
	let s = initialRecoveryState();
	s = reduceRecovery(s, ev({ kind: 'clientAttached', clientEpoch: 1, at: t0 })).state;
	s = reduceRecovery(s, ev({ kind: 'transportActive', transportEpoch: 1, attemptEpoch: null, at: t0 })).state;
	s = reduceRecovery(s, ev({ kind: 'speechObserved', at: t0 + 1_000 })).state;
	return s;
}

// ── Latch + anchor lifecycle ─────────────────────────────────────────────
{
	let s = initialRecoveryState();
	s = reduceRecovery(s, ev({ kind: 'clientAttached', clientEpoch: 1, at: 0 })).state;
	check('fresh state fails closed (null anchor, no latch)', [s.speechLatched, s.silenceAnchorAt], [false, null]);
	s = reduceRecovery(s, ev({ kind: 'speechObserved', at: 5_000 })).state;
	check('first speech latches with exact timestamp', [s.speechLatched, s.firstSpeechAt, s.silenceAnchorAt], [true, 5_000, 5_000]);
	s = reduceRecovery(s, ev({ kind: 'speechObserved', at: 9_000 })).state;
	check('later speech advances lastAboveFloorAt, keeps firstSpeechAt', [s.firstSpeechAt, s.lastAboveFloorAt], [5_000, 9_000]);
	s = reduceRecovery(s, ev({ kind: 'transportActive', transportEpoch: 1, attemptEpoch: null, at: 10_000 })).state;
	check('activation advances the anchor', s.silenceAnchorAt, 10_000);
	s = reduceRecovery(s, ev({ kind: 'modelEvent', at: 20_000, transportEpoch: 1 })).state;
	check('current-transport model event advances anchor, keeps latch', [s.silenceAnchorAt, s.speechLatched], [20_000, true]);
	s = reduceRecovery(s, ev({ kind: 'userVisibleResponse', at: 21_000, transportEpoch: 1, clientEpoch: 1, channel: 'audio-egress' })).state;
	check('user-visible response performs the full reset', [s.speechLatched, s.streak, s.episodeAttempts, s.phase], [false, 0, 0, 'idle']);
}

// ── Meeting mode ─────────────────────────────────────────────────────────
{
	let s = armed(0);
	s = reduceRecovery(s, ev({ kind: 'meetingModeChanged', active: true, at: 2_000 })).state;
	check('entering meeting mode clears speech evidence', [s.speechLatched, s.firstSpeechAt, s.streak], [false, null, 0]);
	s = reduceRecovery(s, ev({ kind: 'speechObserved', at: 3_000 })).state;
	check('meeting-mode speech never latches', s.speechLatched, false);
	s = reduceRecovery(s, ev({ kind: 'meetingModeChanged', active: false, at: 4_000 })).state;
	check('exit leaves evidence clear (no fire without new speech)', s.speechLatched, false);
}

// ── Streak build + the incident (named case): 6 qualifying ticks ⇒ fire at 3 ──
{
	let s = armed(0);
	let fired = -1;
	for (let i = 1; i <= 6; i++) {
		const r = reduceRecovery(s, tick(60_000 + i * T));
		s = r.state;
		if (r.effect === 'restart' && fired < 0) fired = i;
	}
	check('2026-08-17 incident: fires on the 3rd qualifying tick', fired, DEFAULT_ACTIVE_SILENCE_TICKS);
	check('restart consumes exactly one attempt at authorization', s.episodeAttempts, 1);
	check('phase is restarting; single-flight', s.phase, 'restarting');
	const dup = reduceRecovery(s, tick(60_000 + 7 * T));
	check('ticks while restarting are inert', [dup.effect, dup.state.phase], ['none', 'restarting']);
}

// ── Ask-once-then-wait: latch holds without fresh speech-in-window facts ──
{
	let s = armed(0);
	const noSpeechFacts = { ...FACTS_OK, speechInWindow: false, speechObservedAt: null };
	let fired = false;
	for (let i = 1; i <= 3; i++) {
		const r = reduceRecovery(s, tick(60_000 + i * T, { facts: noSpeechFacts }));
		s = r.state;
		if (r.effect === 'restart') fired = true;
	}
	check('ask-once-then-wait still fires (latch, not window)', fired, true);
}

// ── Reset vetoes reset the streak; authorization vetoes cap it ───────────
{
	let s = armed(0);
	s = reduceRecovery(s, tick(60_000 + T)).state;
	s = reduceRecovery(s, tick(60_000 + 2 * T)).state;
	check('streak built to 2', s.streak, 2);
	s = reduceRecovery(s, tick(60_000 + 3 * T, { facts: { ...FACTS_OK, ingressAdvanced: false } })).state;
	check('ingress stall resets the streak', s.streak, 0);
	// rebuild, then hit quiescence veto at threshold
	s = reduceRecovery(s, tick(60_000 + 4 * T)).state;
	s = reduceRecovery(s, tick(60_000 + 5 * T)).state;
	s = reduceRecovery(s, ev({ kind: 'speechObserved', at: 60_000 + 6 * T - 500 })).state; // speaking 0.5s ago
	const r = reduceRecovery(s, tick(60_000 + 6 * T));
	check('quiescence caps streak at threshold without firing', [r.effect, r.state.streak], ['none', DEFAULT_ACTIVE_SILENCE_TICKS]);
	// pending tool resets
	const r2 = reduceRecovery(r.state, tick(60_000 + 7 * T, { pendingToolCount: 1 }));
	check('pending tool resets the streak', r2.state.streak, 0);
}

// ── Model event mid-window resets streak (continuous silence) ────────────
{
	let s = armed(0);
	s = reduceRecovery(s, tick(60_000 + T)).state;
	s = reduceRecovery(s, ev({ kind: 'modelEvent', at: 60_000 + T + 1_000, transportEpoch: 1 })).state;
	check('slow-but-alive model resets streak via anchor', s.streak, 0);
	const r = reduceRecovery(s, tick(60_000 + T + 11_000)); // 10s after the event
	check('tick inside the fresh 15s window does not qualify', r.state.streak, 0);
	const r2 = reduceRecovery(r.state, tick(60_000 + T + 17_000)); // 16s after
	check('silence re-accumulates only from the new anchor', r2.state.streak, 1);
}

// ── waiting-retry lifecycle (residue 5 settled) ──────────────────────────
{
	let s = armed(0);
	for (let i = 1; i <= 3; i++) s = reduceRecovery(s, tick(60_000 + i * T)).state;
	check('restarting after authorization', s.phase, 'restarting');
	const ae = s.attemptEpoch;
	const r = reduceRecovery(s, ev({ kind: 'dialFailed', attemptEpoch: ae, at: 200_000 }));
	check('dialFailed → waiting-retry with schedule-retry effect', [r.state.phase, r.effect], ['waiting-retry', 'schedule-retry']);
	check('retryNotBefore respects the 60s cooldown', r.state.retryNotBefore !== null && r.state.retryNotBefore >= (r.state.lastActionAt ?? 0) + 60_000, true);
	// early retryDue is inert
	let r2 = reduceRecovery(r.state, ev({ kind: 'retryDue', attemptEpoch: ae, at: 200_001 }));
	check('early retryDue is inert', [r2.effect, r2.state.phase], ['none', 'waiting-retry']);
	// due + attached → counted restart
	r2 = reduceRecovery(r.state, ev({ kind: 'retryDue', attemptEpoch: ae, at: (r.state.retryNotBefore ?? 0) + 1 }));
	check('due retry restarts and consumes attempt 2', [r2.effect, r2.state.episodeAttempts, r2.state.phase], ['restart', 2, 'restarting']);
	// detached at delivery: inert, re-scheduled on attach
	const sd = reduceRecovery(r.state, ev({ kind: 'clientDetached', clientEpoch: 1, at: 200_100 })).state;
	const rDet = reduceRecovery(sd, ev({ kind: 'retryDue', attemptEpoch: ae, at: (r.state.retryNotBefore ?? 0) + 1 }));
	check('retryDue while detached is inert', rDet.effect, 'none');
	const rAtt = reduceRecovery(rDet.state, ev({ kind: 'clientAttached', clientEpoch: 2, at: (r.state.retryNotBefore ?? 0) + 5_000 }));
	check('reattach while overdue reschedules', rAtt.effect, 'schedule-retry');
}

// ── fatal backoff (residue 6 settled) ────────────────────────────────────
{
	let s = armed(0);
	for (let i = 1; i <= 3; i++) s = reduceRecovery(s, tick(60_000 + i * T)).state;
	const ae = s.attemptEpoch;
	let r = reduceRecovery(s, ev({ kind: 'dialFailed', attemptEpoch: ae, at: 200_000 }));
	r = reduceRecovery(r.state, ev({ kind: 'fatalBackoff', until: 400_000 }));
	check('backoff raises retryNotBefore and reschedules', [r.effect, r.state.retryNotBefore], ['schedule-retry', 400_000]);
	const early = reduceRecovery(r.state, ev({ kind: 'retryDue', attemptEpoch: ae, at: 300_000 }));
	check('retryDue before backoffUntil is inert', early.effect, 'none');
	const cleared = reduceRecovery(r.state, ev({ kind: 'fatalBackoffCleared', at: 300_500 }));
	check('fatalBackoffCleared zeroes backoff and reschedules', [cleared.state.backoffUntil, cleared.effect], [0, 'schedule-retry']);
}

// ── Terminal: entry, durability, notify-on-attach, clear set, retry ──────
{
	let s = armed(0);
	// exhaust: authorize 3 attempts via dialFailed cycles
	for (let a = 0; a < EPISODE_ATTEMPT_LIMIT; a++) {
		if (a === 0) { for (let i = 1; i <= 3; i++) s = reduceRecovery(s, tick(60_000 + i * T)).state; }
		const r = a === 0
			? { state: s, effect: 'restart' }
			: reduceRecovery(s, ev({ kind: 'retryDue', attemptEpoch: s.attemptEpoch, at: (s.retryNotBefore ?? 0) + 1 }));
		s = r.state;
		const fail = reduceRecovery(s, ev({ kind: 'dialFailed', attemptEpoch: s.attemptEpoch, at: 300_000 + a * 100_000 }));
		s = fail.state;
		if (a === EPISODE_ATTEMPT_LIMIT - 1) {
			check('third failed dial enters terminal immediately', [s.phase, fail.effect], ['terminal', 'notify-stalled']);
		}
	}
	// durability: detach preserves terminal; attach re-notifies
	s = reduceRecovery(s, ev({ kind: 'clientDetached', clientEpoch: 1, at: 700_000 })).state;
	check('detach preserves terminal', s.phase, 'terminal');
	const att = reduceRecovery(s, ev({ kind: 'clientAttached', clientEpoch: 2, at: 710_000 }));
	check('attach in terminal re-notifies', att.effect, 'notify-stalled');
	s = att.state;
	// modelEvent does NOT clear terminal; ordinary activation does not either
	s = reduceRecovery(s, ev({ kind: 'transportActive', transportEpoch: 5, attemptEpoch: null, at: 720_000 })).state;
	s = reduceRecovery(s, ev({ kind: 'modelEvent', at: 721_000, transportEpoch: 5 })).state;
	check('modelEvent + ordinary activation preserve terminal', s.phase, 'terminal');
	// stale retry (wrong attemptEpoch) is record-only
	const stale = reduceRecovery(s, ev({ kind: 'retry', stalledAttemptEpoch: s.attemptEpoch - 1, clientEpoch: 2, requestId: 'r1', at: 730_000 }));
	check('stale retry is record-only', [stale.effect, stale.state.phase], ['record-only', 'terminal']);
	// matching human retry: fresh episode, attempts=1, restart
	const retry = reduceRecovery(s, ev({ kind: 'retry', stalledAttemptEpoch: s.attemptEpoch, clientEpoch: 2, requestId: 'r2', at: 740_000 }));
	check('matching retry starts fresh episode with attempts=1', [retry.effect, retry.state.episodeAttempts, retry.state.phase], ['restart', 1, 'restarting']);
	// uVR clears terminal fully (build another terminal quickly via retry-fail x3? covered above) —
	const cleared = reduceRecovery(s, ev({ kind: 'userVisibleResponse', at: 750_000, transportEpoch: 5, clientEpoch: 2, channel: 'audio-egress' }));
	check('user-visible response clears terminal (full reset)', [cleared.state.phase, cleared.state.episodeAttempts], ['idle', 0]);
}

// ── Epoch fencing ────────────────────────────────────────────────────────
{
	const s = armed(0);
	const r1 = reduceRecovery(s, ev({ kind: 'modelEvent', at: 70_000, transportEpoch: 99 }));
	check('stale-transport model event is record-only', [r1.effect, r1.state.silenceAnchorAt], ['record-only', s.silenceAnchorAt]);
	const r2 = reduceRecovery(s, ev({ kind: 'userVisibleResponse', at: 70_000, transportEpoch: 1, clientEpoch: 42, channel: 'audio-egress' }));
	check('wrong-client uVR is record-only', r2.effect, 'record-only');
	const sd = reduceRecovery(s, ev({ kind: 'clientDetached', clientEpoch: 1, at: 71_000 })).state;
	const r3 = reduceRecovery(sd, ev({ kind: 'userVisibleResponse', at: 72_000, transportEpoch: 1, clientEpoch: 1, channel: 'audio-egress' }));
	check('detached uVR is record-only despite matching fence', r3.effect, 'record-only');
	const r4 = reduceRecovery(s, ev({ kind: 'transportActive', transportEpoch: 1, attemptEpoch: null, at: 73_000 }));
	check('non-newer ordinary activation is record-only', r4.effect, 'record-only');
}

// ── Fresh session: no model-event baseline ⇒ modelSilentFor15s false ⇒ never fires ──
{
	let s = armed(0);
	const freshFacts = { ...FACTS_OK, modelSilentFor15s: false };
	let fired = false;
	for (let i = 1; i <= 5; i++) {
		const r = reduceRecovery(s, tick(60_000 + i * T, { facts: freshFacts }));
		s = r.state;
		if (r.effect === 'restart') fired = true;
	}
	check('no-greeting/fresh session cannot fire (fact gate)', fired, false);
}

// ── Activation fencing ───────────────────────────────────────────────────
{
	let s = armed(0);
	for (let i = 1; i <= 3; i++) s = reduceRecovery(s, tick(60_000 + i * T)).state;
	check('restarting (fencing setup)', s.phase, 'restarting');
	const rOld = reduceRecovery(s, ev({ kind: 'transportActive', transportEpoch: 0, attemptEpoch: s.attemptEpoch, at: 300_000 }));
	check('correlated activation with an OLDER transport epoch is record-only', rOld.effect, 'record-only');
	const rOrd = reduceRecovery(s, ev({ kind: 'transportActive', transportEpoch: 9, attemptEpoch: null, at: 300_000 }));
	check('ordinary activation mid-restart is record-only', [rOrd.effect, rOrd.state.phase], ['record-only', 'restarting']);
	const rGood = reduceRecovery(s, ev({ kind: 'transportActive', transportEpoch: 9, attemptEpoch: s.attemptEpoch, at: 300_000 }));
	check('correlated newer activation completes the restart', [rGood.effect, rGood.state.phase, rGood.state.currentTransportEpoch], ['none', 'idle', 9]);
}

// ── Exact fatal-backoff boundary: eligible AT until, not until+1 ─────────
{
	let s = armed(0);
	for (let i = 1; i <= 3; i++) s = reduceRecovery(s, tick(60_000 + i * T)).state;
	let r = reduceRecovery(s, ev({ kind: 'dialFailed', attemptEpoch: s.attemptEpoch, at: 200_000 }));
	r = reduceRecovery(r.state, ev({ kind: 'fatalBackoff', until: 400_000 }));
	const early = reduceRecovery(r.state, ev({ kind: 'retryDue', attemptEpoch: r.state.attemptEpoch, at: 399_999 }));
	check('retryDue one ms before backoffUntil is inert', early.effect, 'none');
	const atExact = reduceRecovery(r.state, ev({ kind: 'retryDue', attemptEpoch: r.state.attemptEpoch, at: 400_000 }));
	check('retryDue exactly AT backoffUntil restarts (no stranded timer)', atExact.effect, 'restart');
}

// ── Shadow flow ──────────────────────────────────────────────────────────
{
	let s = armed(0);
	for (let i = 1; i <= 3; i++) s = reduceRecovery(s, tick(60_000 + i * T)).state;
	const r = reduceRecovery(s, ev({ kind: 'shadowRestarted', attemptEpoch: s.attemptEpoch, at: 200_000 }));
	check('shadowRestarted returns to idle-equivalent, attempts kept', [r.state.phase, r.state.episodeAttempts, r.state.streak], ['idle', 1, 0]);
	check('shadow anchor advanced to shadow-restart time', r.state.silenceAnchorAt, 200_000);
}

// ── Env parsing ──────────────────────────────────────────────────────────
{
	const warns: string[] = [];
	const w = (m: string) => { warns.push(m); };
	check('ticks: unset → default', parseActiveSilenceTicks(undefined, w), DEFAULT_ACTIVE_SILENCE_TICKS);
	check('ticks: whitespace is invalid → default + warn', parseActiveSilenceTicks('   ', w), DEFAULT_ACTIVE_SILENCE_TICKS);
	check('ticks: 0 disables', parseActiveSilenceTicks('0', w), 0);
	check('ticks: 1 clamps up to floor', parseActiveSilenceTicks('1', w), MIN_ACTIVE_SILENCE_TICKS);
	check('ticks: 100 clamps down to cap', parseActiveSilenceTicks('100', w) <= 40, true);
	check('ticks: 2.5 invalid → default', parseActiveSilenceTicks('2.5', w), DEFAULT_ACTIVE_SILENCE_TICKS);
	check('ticks: -1 invalid → default', parseActiveSilenceTicks('-1', w), DEFAULT_ACTIVE_SILENCE_TICKS);
	check('ticks: 1e999 invalid → default', parseActiveSilenceTicks('1e999', w), DEFAULT_ACTIVE_SILENCE_TICKS);
	check('ticks: warnings name the variable', warns.every((m) => m.includes('VOICE_ACTIVE_SILENCE_TICKS')), true);
	check('mode: default shadow', parseActiveSilenceMode(undefined, w), 'shadow');
	check('mode: off/shadow/armed pass through', [parseActiveSilenceMode('off', w), parseActiveSilenceMode('shadow', w), parseActiveSilenceMode('armed', w)], ['off', 'shadow', 'armed']);
	check('mode: invalid → shadow + warn', parseActiveSilenceMode('bogus', w), 'shadow');
}

console.log(failed ? `FAILED: ${failed} check(s)` : 'voice active-silence reducer: all checks passed');
process.exit(failed ? 1 : 0);
