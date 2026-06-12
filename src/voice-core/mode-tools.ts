// voice-core/mode-tools.ts — factories for the shared voice tools.
// Lifted from voice-agent.ts (NOT copied: voice-agent now builds its tools
// from these factories, and the discord-voice plugin builds its own with a
// different sentinel/note config — one definition, two parametrizations).
//
// Tool descriptions and returned instructions are imported from prompts.ts
// verbatim; do not inline-edit wording here.

import { z } from 'zod';
import { existsSync, appendFileSync, writeFileSync } from 'node:fs';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import type { ModeStateHandle } from './mode-state.js';
import {
	SWITCH_MODE_DESCRIPTION,
	MEETING_ENTER_INSTRUCTION,
	ACTIVE_ENTER_INSTRUCTION,
} from './prompts.js';

/**
 * Build the switch_mode tool bound to a surface's ModeStateHandle.
 * The sentinel mirror + subscriber notifications ride on state.setMeeting()
 * (replaces the inline writeVoiceModeSentinel() call in the pre-refactor
 * voice-agent implementation — same effect, single owner).
 */
export function makeSwitchModeTool(cfg: {
	state: ModeStateHandle;
	log?: (msg: string) => void;
}): ToolDefinition {
	return {
		name: 'switch_mode',
		description: SWITCH_MODE_DESCRIPTION,
		parameters: z.object({
			mode: z.enum(['active', 'meeting']).describe('"meeting" = silent note-taker, "active" = normal assistant'),
		}),
		execution: 'inline',
		async execute(args) {
			const { mode } = args as { mode: 'active' | 'meeting' };
			cfg.state.setMeeting(mode === 'meeting');
			cfg.log?.(`[Meeting] Mode switched to: ${mode}`);
			if (mode === 'meeting') {
				return { status: 'meeting_mode', instruction: MEETING_ENTER_INSTRUCTION };
			}
			return { status: 'active_mode', instruction: ACTIVE_ENTER_INSTRUCTION };
		},
	};
}

/**
 * Build the save_meeting_note tool. `notePathFor(dateISO)` lets each surface
 * choose its notes home (voice-agent: sharedPersonalPath under the workspace;
 * the discord plugin can scope per-guild or reuse the same notes dir).
 */
export function makeSaveMeetingNoteTool(cfg: {
	notePathFor: (dateISO: string) => string;
	log?: (msg: string) => void;
}): ToolDefinition {
	return {
		name: 'save_meeting_note',
		description:
			'Save a meeting observation, decision, or action item to notes. ' +
			'Use this ONLY in meeting mode to periodically capture key points. ' +
			'Call every 5-10 minutes during a meeting, or when a significant decision/action item is discussed. ' +
			'Also call when exiting meeting mode to save a final summary.',
		parameters: z.object({
			content: z.string().describe('The meeting note: decisions, action items, key discussion points, or a summary. Include speaker names when known.'),
			type: z.enum(['point', 'summary']).optional().describe('"point" for individual observations (default), "summary" for end-of-meeting summary'),
		}),
		execution: 'inline',
		async execute(args) {
			const { content, type } = args as { content: string; type?: 'point' | 'summary' };
			const today = new Date().toISOString().slice(0, 10);
			const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
			const notePath = cfg.notePathFor(today);
			const isSummary = type === 'summary';

			if (!existsSync(notePath)) {
				// Create new meeting note file with frontmatter
				const header = `---\ntitle: Meeting notes — ${today}\ndate: ${today}\ntags: [meeting, notes]\n---\n\n`;
				writeFileSync(notePath, header);
			}

			const entry = isSummary
				? `\n## Summary (${time})\n${content}\n`
				: `\n- **[${time}]** ${content}`;
			appendFileSync(notePath, entry);
			cfg.log?.(`[MeetingNote] ${isSummary ? 'Summary' : 'Point'} saved to ${notePath}`);
			return { status: 'saved', path: notePath, type: isSummary ? 'summary' : 'point' };
		},
	};
}
