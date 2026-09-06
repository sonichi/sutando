/**
 * Voice tool: switch voice-agent's model + googleSearch preset at runtime.
 *
 * Writes the per-user voice-agent config at
 * `$SUTANDO_WORKSPACE/config/voice-agent.json` (data, not code — NOT a
 * committed repo file; the repo ships voice-agent.config.json.example as a
 * template) and fires the GUARDED restart wrapper
 * (`scripts/restart-voice-agent.sh`) so voice-agent restarts and picks
 * up the new config — never a direct `launchctl kickstart -k` (amendment
 * T4: the pre-kickstart validation runs as one guarded voice-lock.py
 * takeover transaction). The web client auto-reconnects on restart, so the
 * user-visible flow is: spoken command → ack → ~2-3s silence → voice
 * back with new model.
 *
 * Presets (named after the only knob that matters — Web grounding):
 *   - 'search'    → 2.5-flash-native-audio + googleSearch:true  (Web grounding ON)
 *   - 'no-search' → 3.1-flash-live-preview + googleSearch:false (newer model, no Web)
 *   - 'latest-search' → 3.1-flash-live-preview + googleSearch:true (needs a paid-tier VOICE key)
 *
 * The tool returns BEFORE the restart fires (small setTimeout) so Gemini
 * can speak the ack before the transport closes. The guarded takeover kills
 * this process; launchd respawns it; web client reconnects.
 */

import { z } from 'zod';
import { writeFileSync, renameSync, mkdirSync, readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { VOICE_CONFIG_DEFAULTS, type VoiceConfig } from './voice-config.js';
import { resolveWorkspace } from './workspace_default.js';

const REPO_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

/** Absolute path of the guarded restart wrapper — the ONLY sanctioned way to
 * restart voice-agent (voice-reliability plan amendment T4). */
export const GUARDED_RESTART_SCRIPT = join(REPO_ROOT, 'scripts', 'restart-voice-agent.sh');

/**
 * Fire the guarded restart (detached, fire-and-forget). Never a direct
 * `launchctl kickstart -k` of com.sutando.voice-agent: kickstart of a launchd
 * job is a restart, and amendment T4 requires the pre-kickstart validation —
 * identity of the running job pid via ONE guarded `voice-lock.py takeover`
 * transaction — to precede it. restart-voice-agent.sh wraps exactly that
 * (validate → TERM → wait → KILL → revalidate → unlink under the held fcntl
 * guard, then kickstart + etime verification); identity mismatch ⇒
 * takeover-blocked, nothing signaled; missing interpreter ⇒ the wrapper fails
 * closed before touching the lock. `spawnImpl` is injectable for tests.
 */
export function fireGuardedRestart(spawnImpl: typeof spawn = spawn): void {
	// detached so the wrapper outlives this process — the takeover it runs
	// kills the current voice-agent (we ARE the lock holder) mid-script.
	const child = spawnImpl('bash', [GUARDED_RESTART_SCRIPT], {
		detached: true,
		stdio: 'ignore',
	});
	child.unref();
}

// Presets carry only the two knobs this tool switches (model + googleSearch);
// owner_mode / channels are merged in from VOICE_CONFIG_DEFAULTS at write time.
type VoiceConfigPreset = Pick<VoiceConfig, 'model' | 'googleSearch'>;

export const PRESETS: Record<'search' | 'no-search' | 'latest-search', VoiceConfigPreset> = {
	search: { model: 'gemini-2.5-flash-native-audio-preview-12-2025', googleSearch: true },
	'no-search': { model: 'gemini-3.1-flash-live-preview', googleSearch: false },
	// 3.1 + search: a free-tier VOICE key closes with 1011; a paid-tier key holds.
	'latest-search': { model: 'gemini-3.1-flash-live-preview', googleSearch: true },
};

const ts = () => new Date().toISOString().slice(11, 23);

/** Read the live config as raw JSON. An absent, unreadable, or corrupt file
 *  is not a reason to refuse a switch — the caller falls back to defaults. */
export function readConfigRaw(path: string): unknown {
	try {
		return existsSync(path) ? JSON.parse(readFileSync(path, 'utf-8')) : null;
	} catch {
		return null;
	}
}

/**
 * What the switch writes: defaults fill gaps, **the user's own file is
 * preserved**, and only the preset's keys are overlaid.
 *
 * The previous form was `{...VOICE_CONFIG_DEFAULTS, ...preset}` — it never
 * read the file, so every switch REPLACED it. That silently deleted session
 * tuning (`compressionConfig`, `mediaResolution`), their explicit
 * `null`/`false` off-switches, and any future key, with a restart right
 * behind it so the loss left no trace. Preserving raw is also what makes a
 * fleet-wide defaults revert reach devices whose user has used the switch.
 */
export function nextSwitchConfig(
	existingRaw: unknown,
	preset: Pick<VoiceConfig, 'model' | 'googleSearch'>,
): VoiceConfig {
	const existing =
		existingRaw && typeof existingRaw === 'object' && !Array.isArray(existingRaw)
			? (existingRaw as Partial<VoiceConfig>)
			: {};
	return { ...VOICE_CONFIG_DEFAULTS, ...existing, ...preset };
}

export const switchVoiceConfigTool: ToolDefinition = {
	name: 'switch_voice_config',
	description:
		'Switch voice-agent to a different model + googleSearch preset and restart. ' +
		'Use when the user explicitly asks to switch — e.g. "switch to search mode", ' +
		'"switch to no-search mode", "use 2.5", "use 3.1", "turn search on", "turn search off". ' +
		'Presets: ' +
		'"search" = gemini-2.5-flash-native-audio + googleSearch:true (best for Q&A with Web grounding); ' +
		'"no-search" = gemini-3.1-flash-live-preview + googleSearch:false (newer model, no Web grounding); ' +
		'"latest-search" = gemini-3.1-flash-live-preview + googleSearch:true (newest model with Web grounding; needs a paid-tier VOICE key). ' +
		'Restart takes ~2-3 seconds during which voice will be silent; the web client auto-reconnects. ' +
		'HIGH-IMPACT: this restarts the whole voice session. Call it ONLY on one of those explicit switch ' +
		'requests — NEVER because the conversation merely mentions search/searching, and never on filler ' +
		'or garbled speech; when unsure, fire nothing.',
	parameters: z.object({
		preset: z.enum(['search', 'no-search', 'latest-search']).describe('Which preset to switch to. "search" = 2.5+Web grounding. "no-search" = 3.1+no-Web.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { preset } = args as { preset: 'search' | 'no-search' | 'latest-search' };
		const cfg = PRESETS[preset];
		if (!cfg) {
			return { error: `Unknown preset "${preset}". Use "search", "no-search" or "latest-search".` };
		}

		// The voice-agent config is per-user data — it lives in the workspace
		// ($SUTANDO_WORKSPACE/config/voice-agent.json), NOT in the git repo.
		// voice-agent reads from the same path; mkdir the config/ dir in case
		// this switch fires before voice-agent has seeded it.
		const configPath = join(resolveWorkspace(), 'config', 'voice-agent.json');

		// Atomic write (tmp+rename) so a partial config never lands.
		const tmpPath = `${configPath}.tmp-${process.pid}`;
		try {
			mkdirSync(join(resolveWorkspace(), 'config'), { recursive: true });
			const next = nextSwitchConfig(readConfigRaw(configPath), cfg);
			writeFileSync(tmpPath, JSON.stringify(next, null, 2) + '\n');
			renameSync(tmpPath, configPath);
			console.log(`${ts()} [SwitchVoiceConfig] wrote ${configPath} → preset=${preset} (model=${cfg.model}, search=${cfg.googleSearch})`);
		} catch (e) {
			console.error(`${ts()} [SwitchVoiceConfig] write failed:`, e);
			return { error: `Failed to write config: ${(e as Error).message}` };
		}

		// Schedule restart AFTER returning so Gemini speaks the ack first.
		// 1.5s gives the model time to render the ack into audio + push to
		// the transport before the guarded takeover kills us.
		setTimeout(() => {
			console.log(`${ts()} [SwitchVoiceConfig] firing guarded restart (${GUARDED_RESTART_SCRIPT})`);
			fireGuardedRestart();
		}, 1500);

		const summary = preset === 'search'
			? 'Switching to search mode: Gemini 2.5 with Web grounding. Restarting now…'
			: preset === 'latest-search'
				? 'Switching to latest-search mode: Gemini 3.1 with Web grounding. Restarting now…'
				: 'Switching to no-search mode: Gemini 3.1, no Web grounding. Restarting now…';
		return {
			ok: true,
			preset,
			model: cfg.model,
			googleSearch: cfg.googleSearch,
			summary,
		};
	},
};
