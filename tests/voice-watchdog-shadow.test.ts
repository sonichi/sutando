/**
 * Shadow observer wiring: derives diagnostic events from real tick inputs and
 * persists would-restart evidence without ever touching the live session.
 * Includes the 2026-08-17 incident replay as the named end-to-end case.
 */
import { mkdtempSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { VoiceWatchdogShadow, DETECTOR_VERSION } from '../src/voice-watchdog-shadow.js';
import { WatchdogLedger } from '../src/voice-watchdog-ledger.js';
import type { AudioHealthSnapshot } from '../src/voice-audio-health.js';
import type { MatrixFacts } from '../src/voice-health-matrix.js';

let failed = 0;
function check(name: string, got: unknown, want: unknown): void {
	const ok = JSON.stringify(got) === JSON.stringify(want);
	console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}`);
	if (!ok) { console.log(`       got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); failed++; }
}

function snap(o: {
	epoch?: number; lastModelEventAt?: number | null; egressFrames?: number;
	lastEgressAt?: number | null; onsetAt?: number | null; lastAboveFloorAt?: number | null;
}): AudioHealthSnapshot {
	return {
		epoch: o.epoch ?? 1,
		lastModelEventAt: o.lastModelEventAt ?? null,
		egressFrames: o.egressFrames ?? 0,
		lastEgressAt: o.lastEgressAt ?? null,
		speech: { active: false, onsetAt: o.onsetAt ?? null, lastAboveFloorAt: o.lastAboveFloorAt ?? null },
	} as unknown as AudioHealthSnapshot;
}
function facts(o: Partial<MatrixFacts> = {}): MatrixFacts {
	return {
		factsAvailable: true, speechInWindow: true, speechObservedAt: null,
		ingressAdvanced: true, modelSilentFor15s: true, ...o,
	};
}

const dir = mkdtempSync(join(tmpdir(), 'watchdog-shadow-'));

// ── Incident replay: attach → ACTIVE → one ask → silence → would-fire at 3 ──
{
	const path = join(dir, 'incident.jsonl');
	const ledger = new WatchdogLedger({ path, meta: { detectorVersion: DETECTOR_VERSION } });
	const logs: string[] = [];
	const sh = new VoiceWatchdogShadow({ ledger, env: {} as NodeJS.ProcessEnv, log: (l) => logs.push(l) });
	check('default mode is shadow', sh.mode, 'shadow');

	const T = 30_000;
	// model last spoke at t=100_000 (turn_233); user speaks at 110_000; then dead air.
	for (let i = 0; i < 8; i++) {
		const at = 130_000 + i * T;
		sh.observeTick({
			at, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
			snapshot: snap({
				lastModelEventAt: 100_000,
				onsetAt: 110_000, lastAboveFloorAt: 111_500,
			}),
			facts: facts({ speechObservedAt: 110_000 }),
		});
	}
	const fires = logs.filter((l) => l.includes('would-restart'));
	check('incident replay: two would-restarts in 8 ticks (3rd QUALIFYING tick fires; arming tick does not qualify)', fires.length, 2);
	check('first fire tagged first-fire', fires[0]?.includes('first-fire'), true);
	check('second fire tagged synthetic-follow-up', fires[1]?.includes('synthetic-follow-up'), true);
	await ledger.flush();
	const rows = readFileSync(path, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
	check('ledger rows written with detector version', rows.every((r) => r.detectorVersion === DETECTOR_VERSION), true);
	check('ledger row kinds', rows.map((r) => r.row), ['would-restart', 'would-restart']);
	check('anchor age at first fire = 90s (attach anchors at 130s; fires at 220s)',
		rows[0].anchorAgeMs, 90_000);
}

// ── Model activity between ticks suppresses the fire ─────────────────────
{
	const ledger = new WatchdogLedger({ path: join(dir, 'alive.jsonl'), meta: {} });
	const logs: string[] = [];
	const sh = new VoiceWatchdogShadow({ ledger, env: {} as NodeJS.ProcessEnv, log: (l) => logs.push(l) });
	const T = 30_000;
	for (let i = 0; i < 8; i++) {
		const at = 130_000 + i * T;
		sh.observeTick({
			at, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
			snapshot: snap({
				lastModelEventAt: at - 5_000, // model keeps producing
				onsetAt: 110_000, lastAboveFloorAt: 111_500,
			}),
			facts: facts({ speechObservedAt: 110_000, modelSilentFor15s: false }),
		});
	}
	check('live model: zero would-restarts', logs.filter((l) => l.includes('would-restart')).length, 0);
}

// ── Off mode is inert; armed falls back to shadow ────────────────────────
{
	const ledger = new WatchdogLedger({ path: join(dir, 'off.jsonl'), meta: {} });
	const logs: string[] = [];
	const off = new VoiceWatchdogShadow({
		ledger, env: { VOICE_ACTIVE_SILENCE_MODE: 'off' } as unknown as NodeJS.ProcessEnv, log: (l) => logs.push(l),
	});
	check('off mode', off.mode, 'off');
	off.observeTick({
		at: 1, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
		snapshot: snap({}), facts: facts(),
	});
	check('off mode writes nothing', existsSync(join(dir, 'off.jsonl')), false);

	const armedLogs: string[] = [];
	const armed = new VoiceWatchdogShadow({
		ledger, env: { VOICE_ACTIVE_SILENCE_MODE: 'armed' } as unknown as NodeJS.ProcessEnv, log: (l) => armedLogs.push(l),
	});
	check('armed falls back to shadow', armed.mode, 'shadow');
	check('fallback names the missing capability', armedLogs.some((l) => l.includes('capability descriptor')), true);

	const disabled = new VoiceWatchdogShadow({
		ledger, env: { VOICE_ACTIVE_SILENCE_TICKS: '0' } as unknown as NodeJS.ProcessEnv, log: () => {},
	});
	check('TICKS=0 disables regardless of mode', disabled.mode, 'off');
}

// ── Pending tool vetoes the streak ───────────────────────────────────────
{
	const ledger = new WatchdogLedger({ path: join(dir, 'tool.jsonl'), meta: {} });
	const logs: string[] = [];
	const clock = { t: 130_000 };
	const sh = new VoiceWatchdogShadow({ ledger, env: {} as NodeJS.ProcessEnv, log: (l) => logs.push(l), nowFn: () => clock.t });
	const T = 30_000;
	// Tool calls can only exist once a transport is ACTIVE: register after the
	// first tick (whose activation edge rebuilds the pending set for the epoch).
	for (let i = 0; i < 5; i++) {
		if (i === 1) sh.noteToolCall('t1');
		clock.t = 130_000 + i * T;
		sh.observeTick({
			at: 130_000 + i * T, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
			snapshot: snap({ lastModelEventAt: 100_000, onsetAt: 110_000, lastAboveFloorAt: 111_500 }),
			facts: facts({ speechObservedAt: 110_000 }),
		});
	}
	check('pending tool suppresses all fires', logs.filter((l) => l.includes('would-restart')).length, 0);
	clock.t = 130_000 + 4 * T + 1_000;
	sh.noteToolSettled('t1'); // settle advances the anchor (upstream progress)
	for (let i = 5; i < 10; i++) {
		clock.t = 130_000 + i * T;
		sh.observeTick({
			at: 130_000 + i * T, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
			snapshot: snap({ lastModelEventAt: 100_000, onsetAt: 110_000, lastAboveFloorAt: 111_500 }),
			facts: facts({ speechObservedAt: 110_000 }),
		});
	}
	check('settled tool releases the veto', logs.filter((l) => l.includes('would-restart')).length >= 1, true);
}

// ── Background tools never veto ──────────────────────────────────────────
{
	const ledger = new WatchdogLedger({ path: join(dir, 'bg.jsonl'), meta: {} });
	const logs: string[] = [];
	const sh = new VoiceWatchdogShadow({ ledger, env: {} as NodeJS.ProcessEnv, log: (l) => logs.push(l) });
	const T = 30_000;
	for (let i = 0; i < 5; i++) {
		if (i === 1) sh.noteToolCall('bg1', 'background');
		sh.observeTick({
			at: 130_000 + i * T, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
			snapshot: snap({ lastModelEventAt: 100_000, onsetAt: 110_000, lastAboveFloorAt: 111_500 }),
			facts: facts({ speechObservedAt: 110_000 }),
		});
	}
	check('background tool does not suppress the fire', logs.filter((l) => l.includes('would-restart')).length, 1);
}

// ── A user-visible response must not be undone by retained old speech ────
{
	const ledger = new WatchdogLedger({ path: join(dir, 'resp.jsonl'), meta: {} });
	const sh = new VoiceWatchdogShadow({ ledger, env: {} as NodeJS.ProcessEnv, log: () => {} });
	const T = 30_000;
	sh.observeTick({
		at: 130_000, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
		snapshot: snap({ lastModelEventAt: 100_000, onsetAt: 110_000, lastAboveFloorAt: 111_500 }),
		facts: facts({ speechObservedAt: 110_000 }),
	});
	// Response arrives (egress) — same old speech values still in the snapshot.
	sh.observeTick({
		at: 130_000 + T, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
		snapshot: snap({
			lastModelEventAt: 100_000, onsetAt: 110_000, lastAboveFloorAt: 111_500,
			egressFrames: 50, lastEgressAt: 150_000,
		}),
		facts: facts({ speechObservedAt: 110_000 }),
	});
	check('response clears the latch and old speech does not relatch it',
		sh.snapshotState.speechLatched, false);
}

// ── Client reload between ticks (epoch change, boolean never blinks) ─────
{
	const ledger = new WatchdogLedger({ path: join(dir, 'reload.jsonl'), meta: {} });
	const sh = new VoiceWatchdogShadow({ ledger, env: {} as NodeJS.ProcessEnv, log: () => {} });
	sh.observeTick({
		at: 130_000, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
		snapshot: snap({ epoch: 1 }), facts: facts({ speechObservedAt: null, speechInWindow: false }),
	});
	sh.observeTick({
		at: 160_000, sessionState: 'ACTIVE', clientConnected: true, meetingMode: false,
		snapshot: snap({ epoch: 2 }), facts: facts({ speechObservedAt: null, speechInWindow: false }),
	});
	check('epoch change installs the new client epoch', sh.snapshotState.currentClientEpoch, 2);
}

// ── Ledger bounds ────────────────────────────────────────────────────────
{
	const path = join(dir, 'cap.jsonl');
	const ledger = new WatchdogLedger({ path, meta: {} });
	for (let i = 0; i < 600; i++) ledger.append({ row: 'x', i });
	await ledger.flush();
	check('ledger never loses rows silently (written + dropped = offered)',
		ledger.written + ledger.dropped, 600);
}

console.log(failed ? `FAILED: ${failed} check(s)` : 'voice watchdog shadow: all checks passed');
process.exit(failed ? 1 : 0);
