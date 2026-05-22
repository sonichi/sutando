/**
 * Voice tool: switch voice-agent's model + googleSearch preset at runtime.
 *
 * Writes `src/voice-agent.config.json` and kicks `launchctl kickstart -k
 * gui/$(id -u)/com.sutando.voice-agent` so voice-agent restarts and picks
 * up the new config. The web client auto-reconnects on restart, so the
 * user-visible flow is: spoken command → ack → ~2-3s silence → voice
 * back with new model.
 *
 * Presets (named after the only knob that matters — Web grounding):
 *   - 'search'    → 2.5-flash-native-audio + googleSearch:true  (Web grounding ON)
 *   - 'no-search' → 3.1-flash-live-preview + googleSearch:false (newer model, no Web)
 *
 * The tool returns BEFORE the kickstart fires (small setTimeout) so Gemini
 * can speak the ack before the transport closes. Kickstart kills this
 * process; launchd respawns it; web client reconnects.
 */

import { z } from 'zod';
import { writeFileSync, renameSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { VOICE_CONFIG_DEFAULTS, type VoiceConfig } from './voice-config.js';

const PRESETS: Record<'search' | 'no-search', VoiceConfig> = {
	search: { model: 'gemini-2.5-flash-native-audio-preview-12-2025', googleSearch: true },
	'no-search': { model: 'gemini-3.1-flash-live-preview', googleSearch: false },
};

const ts = () => new Date().toISOString().slice(11, 23);

export const switchVoiceConfigTool: ToolDefinition = {
	name: 'switch_voice_config',
	description:
		'Switch voice-agent to a different model + googleSearch preset and restart. ' +
		'Use when the user explicitly asks to switch — e.g. "switch to search mode", ' +
		'"switch to no-search mode", "use 2.5", "use 3.1", "turn search on", "turn search off". ' +
		'Presets: ' +
		'"search" = gemini-2.5-flash-native-audio + googleSearch:true (best for Q&A with Web grounding); ' +
		'"no-search" = gemini-3.1-flash-live-preview + googleSearch:false (newer model, no Web grounding). ' +
		'Restart takes ~2-3 seconds during which voice will be silent; the web client auto-reconnects.',
	parameters: z.object({
		preset: z.enum(['search', 'no-search']).describe('Which preset to switch to. "search" = 2.5+Web grounding. "no-search" = 3.1+no-Web.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { preset } = args as { preset: 'search' | 'no-search' };
		const cfg = PRESETS[preset];
		if (!cfg) {
			return { error: `Unknown preset "${preset}". Use "search" or "no-search".` };
		}

		// Resolve config path next to voice-agent.ts. import.meta.url resolves
		// to this file's URL — both compiled .js and source .ts share the same
		// parent dir (src/), so the config path is identical regardless of
		// runtime.
		const thisDir = dirname(fileURLToPath(import.meta.url));
		const configPath = join(thisDir, 'voice-agent.config.json');

		// Atomic write (tmp+rename) so a partial config never lands.
		const tmpPath = `${configPath}.tmp-${process.pid}`;
		try {
			// Merge with defaults so the on-disk file is complete + auditable.
			const next: VoiceConfig = { ...VOICE_CONFIG_DEFAULTS, ...cfg };
			writeFileSync(tmpPath, JSON.stringify(next, null, 2) + '\n');
			renameSync(tmpPath, configPath);
			console.log(`${ts()} [SwitchVoiceConfig] wrote ${configPath} → preset=${preset} (model=${cfg.model}, search=${cfg.googleSearch})`);
		} catch (e) {
			console.error(`${ts()} [SwitchVoiceConfig] write failed:`, e);
			return { error: `Failed to write config: ${(e as Error).message}` };
		}

		// Schedule restart AFTER returning so Gemini speaks the ack first.
		// 1.5s gives the model time to render the ack into audio + push to
		// the transport before launchd kills us.
		setTimeout(() => {
			console.log(`${ts()} [SwitchVoiceConfig] firing launchctl kickstart`);
			const uid = process.getuid?.() ?? 501;
			// detached so the child outlives this process if the kill is fast.
			const child = spawn('launchctl', ['kickstart', '-k', `gui/${uid}/com.sutando.voice-agent`], {
				detached: true,
				stdio: 'ignore',
			});
			child.unref();
		}, 1500);

		const summary = preset === 'search'
			? 'Switching to search mode: Gemini 2.5 with Web grounding. Restarting now…'
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
