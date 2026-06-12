// voice-core/mode-state.ts — single owner of the voice base-mode state
// machine {active | meeting | presenter} for ALL voice surfaces.
//
// Absorbs src/voice-mode-resolver.ts (which now re-exports from here) and the
// sentinel-file I/O previously inlined in voice-agent.ts. Each SURFACE creates
// its own ModeStateHandle with its own sentinel path (Mini amendment #3:
// voice-agent keeps <workspace>/state/voice-mode.txt; the discord-voice plugin
// uses a per-session dvoice-mode-<vc>.txt) — shared definition, independent
// live state, so both surfaces can run simultaneously without clobbering each
// other's mode.
//
// The resolver itself stays pure (no side effects on import) for unit tests.

import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { ACTIVE_MARKER, MEETING_MARKER, PRESENTER_MARKER } from './prompts.js';

export type BaseMode = 'active' | 'meeting' | 'presenter';

export interface ModeState {
	mode: BaseMode;
	/**
	 * The `[BASE MODE: ...]` marker for prompt injection. Includes a leading
	 * space so callers can concatenate inline without adding their own.
	 */
	marker: string;
	isPresenter: boolean;
	isMeeting: boolean;
}

/**
 * Synchronously query the iclr-highlight server for current presenter-mode
 * state. Failure-silent: server down / curl error / non-JSON response → false,
 * so the resolver falls through to meeting/active without throwing.
 *
 * Exported for the unit test to stub via dependency injection. Default
 * implementation hits the real server.
 */
export function isPresenterActiveDefault(): boolean {
	try {
		const out = execFileSync('curl', ['-s', '--max-time', '1', 'http://localhost:7877/presenter'], { timeout: 2_000 }).toString();
		const json = JSON.parse(out);
		return json && json.active === true;
	} catch {
		return false;
	}
}

/**
 * Resolve the current base mode from explicit substrate inputs.
 *
 * Priority: presenter > meeting > active. Screen-companion is a sub-mode that
 * overlays the base (handled in skills/screen-companion/tools.ts), not a base
 * mode itself.
 *
 * @param inputs Substrate state. `meetingActive` is the surface's in-memory
 *   boolean (see ModeStateHandle). `isPresenterActive` is optional — defaults
 *   to live HTTP query; tests pass an explicit boolean to avoid the network.
 */
export function resolveCurrentMode(inputs: {
	meetingActive: boolean;
	isPresenterActive?: () => boolean;
}): ModeState {
	const checkPresenter = inputs.isPresenterActive ?? isPresenterActiveDefault;
	if (checkPresenter()) {
		return {
			mode: 'presenter',
			marker: PRESENTER_MARKER,
			isPresenter: true,
			isMeeting: false,
		};
	}
	if (inputs.meetingActive) {
		return {
			mode: 'meeting',
			marker: MEETING_MARKER,
			isPresenter: false,
			isMeeting: true,
		};
	}
	return {
		mode: 'active',
		marker: ACTIVE_MARKER,
		isPresenter: false,
		isMeeting: false,
	};
}

// =============================================================================
// Per-surface mutable mode state + sentinel mirror
// =============================================================================

export interface ModeStateHandle {
	/** Current meeting/active axis (presenter is resolved separately). */
	isMeeting(): boolean;
	/** Flip the meeting axis; mirrors to the sentinel file and notifies subscribers. */
	setMeeting(v: boolean): void;
	/** Subscribe to meeting-axis changes. Returns an unsubscribe fn. */
	subscribe(cb: (meeting: boolean) => void): () => void;
	/**
	 * Re-write the sentinel from current state (used once at startup after
	 * auto-detects, so on-disk state matches the in-memory decision).
	 */
	writeSentinel(): void;
	/** Full resolver pass including the presenter axis. */
	resolve(isPresenterActive?: () => boolean): ModeState;
}

/**
 * Create a surface-local mode state whose meeting axis is mirrored to
 * `sentinelPath` ("meeting" | "active"). Sentinel writes are fail-silent —
 * mode switching must never crash a live session over disk errors.
 */
export function createModeState(cfg: { sentinelPath: string }): ModeStateHandle {
	let meetingActive = false;
	const subs = new Set<(meeting: boolean) => void>();

	const writeSentinel = () => {
		try {
			mkdirSync(dirname(cfg.sentinelPath), { recursive: true });
			writeFileSync(cfg.sentinelPath, meetingActive ? 'meeting' : 'active');
		} catch {}
	};

	return {
		isMeeting: () => meetingActive,
		setMeeting(v: boolean) {
			meetingActive = v;
			writeSentinel();
			for (const cb of subs) {
				try { cb(meetingActive); } catch {}
			}
		},
		subscribe(cb) {
			subs.add(cb);
			return () => subs.delete(cb);
		},
		writeSentinel,
		resolve(isPresenterActive?: () => boolean) {
			return resolveCurrentMode({ meetingActive, isPresenterActive });
		},
	};
}
