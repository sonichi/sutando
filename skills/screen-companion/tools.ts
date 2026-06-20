// screen-companion: voice-side mode entry.
//
// Two tools: activate_screen_companion(mode, goal) and deactivate_screen_companion().
//
// activate_screen_companion loads the named YAML config from configs/, returns
// a structured payload Gemini reads to switch behavior for the rest of the
// session, AND hard-enforces the tools_allow list by calling session.updateTools()
// via the setSessionToolUpdater hook in vision-tools.ts.
//
// deactivate_screen_companion restores the full tool surface, ending the mode.
//
// The tool does NOT toggle vision itself. Vision is owner-driven (start screen
// sharing → push frames). The tool tells Gemini how to behave AND what to say
// to the owner about screen sharing if vision isn't already streaming.

import { z } from 'zod';
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { loadConfig, discoverConfigs, renderGoal } from './scripts/load-config.js';
import { readSelection as defaultReadSelection, type SelectionResult } from './scripts/read-selection.js';
import { registerVisionOnContributor, callUpdateTools, callRestoreTools, captureSendFrame, getFullToolSurface } from '../../src/vision-tools.js';

function resolveWorkspace(): string {
	const env = process.env.SUTANDO_WORKSPACE;
	if (env) return env.replace(/^~/, process.env.HOME ?? '');
	return join(process.env.HOME ?? '', '.sutando', 'workspace');
}

// Contributor for the screen-share-started system note. Tells Gemini the
// screen-companion catalog is available AND names the configs the user can
// activate. Registered at module-load time (= when the skill-loader at
// src/inline-tools.ts:937 imports this file). If this skill is disabled or
// removed, the registration never runs and the share-start note is generic.
// This is the architecturally clean fix for sonichi's PR #794 review #3:
// no feature-specific knowledge leaks into src/vision-tools.ts.
registerVisionOnContributor(() => {
	const modes = discoverConfigs().map(c => c.name);
	if (modes.length === 0) return null;
	const modeList = modes.map(m => `\`${m}\``).join(', ');
	return (
		`Screen-companion mode is available with these pre-built configs: ${modeList}. ` +
		`Each one encodes one use case (interaction pattern + tool subset + vision cadence). ` +
		`If the user's goal matches a configured mode (e.g. an unfamiliar UI to set up → \`guided-setup\`), ` +
		`call the \`activate_screen_companion\` tool with the matching mode + their goal. ` +
		`If the goal doesn't match a configured mode, operate normally with screen awareness.`
	);
});

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

const availableModes = (): string[] => discoverConfigs().map(c => c.name);

// Track active mode so deactivate_screen_companion can log what it's exiting.
let activeMode: string | null = null;

// Tools that must always remain available in screen-companion mode:
// activate_screen_companion (mode-switch), deactivate_screen_companion (exit).
// `work` (task delegation) and `switch_mode` are NOT inlineTools — they are
// prepended into the session surface as mainAgentTools (see voice-agent.ts).
// They must be named here AND the restriction below must filter the full
// session surface (not inlineTools alone), or they get dropped and become
// uncallable inside any screen-companion mode (see #1375).
const ALWAYS_RETAIN = new Set(['activate_screen_companion', 'deactivate_screen_companion', 'switch_mode', 'work']);

const activateScreenCompanionTool: ToolDefinition = {
	name: 'activate_screen_companion',
	description:
		'Enter screen-companion mode for a specific use case. Call this when the user says one of the activation phrases for a configured mode (e.g. "help me set this up" / "guide me through this" → mode="guided-setup"). ' +
		'Loads the YAML config and returns the system-prompt overlay you MUST follow for the rest of the session, the goal text, and which tools to restrict yourself to. ' +
		'IMPORTANT: after this tool returns, the `instructions` field becomes your operating instructions until the user exits the mode. Treat it as a system prompt — follow it verbatim. ' +
		`Currently available modes: ${availableModes().join(', ') || '(none)'}. ` +
		'If the user describes a screen-watching task that doesn\'t match any mode, do NOT call this tool — instead, ask the user what you want to do and use the regular vision tools.',
	parameters: z.object({
		mode: z
			.string()
			.describe(
				'Name of the screen-companion mode (config filename minus .yaml). E.g. "guided-setup". Must match an existing config — call activate_screen_companion with an invalid mode to discover available modes (error response lists them).',
			),
		goal: z
			.string()
			.optional()
			.describe(
				'What the user is trying to do, in their words. Filled into the config\'s goal_template. E.g. "find the bot token in the Discord developer portal". Optional only if the config has no goal_template — guided-setup REQUIRES this.',
			),
	}),
	execution: 'inline',
	async execute(args) {
		const { mode, goal } = args as { mode: string; goal?: string };
		console.log(`${ts()} [ScreenCompanion] activate mode=${mode} goal=${goal ? `"${goal}"` : '(none)'}`);
		try {
			const config = loadConfig(mode);
			// Run the goal-required guard BEFORE renderGoal so we never
			// produce a string with an un-substituted `{goal}` placeholder.
			// Per sonichi review #4 on PR #794.
			if (config.goal_template && !goal) {
				return {
					error: `Mode "${mode}" requires a goal. Ask the user: "What are you trying to set up?" then call activate_screen_companion again with goal=...`,
				};
			}
			const filledGoal = renderGoal(config, goal);
			const visionHint =
				config.vision_mode === 'push'
					? `Vision mode is PUSH (frames stream at ${config.vision_cadence_ms ?? 1000}ms cadence). If the user is not already screen-sharing, ask them to start it now so you can see what they're doing.`
					: 'Vision mode is PULL (call vision_query when you need to look). The user does not need to screen-share continuously.';

			const activationMessage = filledGoal
				? `Screen Companion: ${mode} — ${filledGoal}. ${visionHint}`
				: `Screen Companion: ${mode}. ${visionHint}`;

			// Hard-enforce tools_allow: restrict the live session's tool surface
			// to only the named tools + always-retain set. If the session updater
			// isn't registered (e.g. phone-conversation context or tests), the
			// call is a no-op and advisory mode remains as the fallback.
			const toolsAllow: string[] = config.tools_allow ?? [];
			// Filter the FULL session surface (work / switch_mode / … + inlineTools),
			// NOT inlineTools alone — otherwise the non-inline mainAgentTools are
			// dropped and become uncallable in-mode (#1375). getFullToolSurface() is
			// populated by voice-agent at startup; fall back to inlineTools when the
			// updater isn't registered (phone-conversation context / tests), matching
			// the prior behavior.
			const fullSurface = getFullToolSurface();
			const restrictSource = fullSurface.length > 0
				? fullSurface
				// Import is deferred to avoid a top-level circular dependency;
				// inlineTools is loaded before this module so by the time execute()
				// runs it is already settled.
				: (await import('../../src/inline-tools.js')).inlineTools;
			const enforced = callUpdateTools(
				restrictSource.filter(
					t => toolsAllow.includes(t.name) || ALWAYS_RETAIN.has(t.name),
				),
			);
			if (enforced) {
				console.log(`${ts()} [ScreenCompanion] tool surface restricted to: ${[...toolsAllow, ...ALWAYS_RETAIN].join(', ')}`);
			}
			activeMode = mode;

			return {
				status: 'activated',
				mode: config.name,
				goal: filledGoal ?? null,
				instructions: config.system_prompt_overlay,
				tools_allow: config.tools_allow,
				// The tools actually reachable this mode = tools_allow PLUS the
				// always-retained capabilities (work / switch_mode / mode controls).
				// The restriction filter above keeps ALWAYS_RETAIN regardless of
				// tools_allow, so expose the effective set to avoid the activation
				// note contradicting a config overlay that says to call `work`.
				effective_tools: [...new Set([...(config.tools_allow ?? []), ...ALWAYS_RETAIN])],
				vision_mode: config.vision_mode,
				vision_cadence_ms: config.vision_cadence_ms ?? null,
				vision_hint: visionHint,
				activation_message: activationMessage,
				tools_enforced: enforced,
				_note:
					'Say activation_message to the user, then follow `instructions` as your system prompt for the rest of the session. Your callable tools for this mode are listed in `effective_tools` — that is `tools_allow` PLUS the always-retained capabilities: `work` (task delegation, available even when not in tools_allow), `switch_mode`, and the activate/deactivate mode controls. When the user says "exit" / "stop the mode" / "done", call deactivate_screen_companion to restore the full tool surface.',
			};
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			console.log(`${ts()} [ScreenCompanion] failed: ${msg}`);
			return {
				error: msg,
				available_modes: availableModes(),
				hint: 'If the user\'s request doesn\'t match any available mode, do NOT call this tool — operate normally with whatever tools the session already has registered.',
			};
		}
	},
};

const deactivateScreenCompanionTool: ToolDefinition = {
	name: 'deactivate_screen_companion',
	description:
		'Exit screen-companion mode and restore the full tool surface. Call this when the user says "exit", "stop the mode", "done", or otherwise indicates they want to leave the current screen-companion mode and return to normal operation.',
	parameters: z.object({}),
	execution: 'inline',
	async execute(_args) {
		const exitedMode = activeMode;
		activeMode = null;
		const restored = callRestoreTools();
		console.log(`${ts()} [ScreenCompanion] deactivated mode=${exitedMode ?? '(unknown)'} restored=${restored}`);
		return {
			status: 'deactivated',
			exited_mode: exitedMode,
			tools_restored: restored,
			_note: 'Screen-companion mode has ended. Resume normal operation with the full tool surface.',
		};
	},
};

// --- vision_query -----------------------------------------------------------
//
// Pull-mode screen lookup with three modes:
//   - 'selection': read the user's text selection (AX or Chrome JS). Exact
//     word/quote questions — no frame capture.
//   - 'frame':     capture & send the current screen to vision. Layout /
//     visual-context questions — no selection probe.
//   - 'both' (default): try selection first; if present, ALSO capture a frame
//     and return a combined result. If no selection, fall through to frame.
//
// Selection probes live in scripts/read-selection.ts (issue #1389, PR #1409).
// Captured frames flow through vision-tools.captureSendFrame to the active
// Gemini Live session. Designed for pull-mode (configs that don't stream).

// Injection seams for tests — production code uses the imported defaults.
let _readSelection: () => SelectionResult | null = defaultReadSelection;
let _captureSendFrame: typeof captureSendFrame = captureSendFrame;
export function _setVisionQueryDeps(deps: {
	readSelection?: () => SelectionResult | null;
	captureSendFrame?: typeof captureSendFrame;
}): void {
	if (deps.readSelection) _readSelection = deps.readSelection;
	if (deps.captureSendFrame) _captureSendFrame = deps.captureSendFrame;
}
export function _resetVisionQueryDeps(): void {
	_readSelection = defaultReadSelection;
	_captureSendFrame = captureSendFrame;
}

const visionQueryTool: ToolDefinition = {
	name: 'vision_query',
	description:
		'Read what\'s on screen. A mode is REQUIRED — pick based on the question: ' +
		'mode="selection" for exact word/quote questions ("what does this sentence say?") — reads the text selection only, no frame; ' +
		'mode="frame" for layout/visual-context questions ("is the dialog open?") — screen capture only, no selection probe; ' +
		'mode="selection-or-frame" when you want the exact selected text if the user has highlighted something, otherwise fall back to a frame — captures a frame ONLY when there is no selection; ' +
		'mode="both" when both might help — reads the selection AND also captures a frame (frame-only fallback if no selection). ' +
		'Pass question= to frame what you\'re looking for. ' +
		'Use in pull-mode screen-companion sessions when you need to check what\'s on screen without streaming continuously.',
	parameters: z.object({
		question: z
			.string()
			.optional()
			.describe('What to look for or answer from the current screen. E.g. "Is the OAuth2 scope list visible?" or "What does the error message say?"'),
		mode: z
			.enum(['selection', 'frame', 'selection-or-frame', 'both'])
			.describe('Required. What to read: "selection" (text selection only), "frame" (screen capture only), "selection-or-frame" (selection if present, else frame), or "both" (selection AND frame).'),
	}),
	execution: 'inline',
	async execute(args) {
		const { question, mode } = (args ?? {}) as {
			question?: string;
			mode: 'selection' | 'frame' | 'selection-or-frame' | 'both';
		};

		const wantSelection = mode === 'selection' || mode === 'both' || mode === 'selection-or-frame';
		const wantFrame = mode === 'frame' || mode === 'both' || mode === 'selection-or-frame';

		const selection = wantSelection ? _readSelection() : null;

		// mode='selection' — only selection, no frame fallback.
		if (mode === 'selection') {
			if (!selection) {
				return {
					status: 'no_selection',
					question: question ?? null,
					_note: 'No text selection found. Ask the user to select the text first, or call vision_query again with mode="frame" if you want a screen capture instead.',
				};
			}
			return {
				status: 'selection_read',
				text: selection.text,
				source: selection.source,
				question: question ?? null,
				_note: question
					? `Selected text read via ${selection.source} (exact, not frame-eyeballing). Answer: ${question}`
					: `Selected text read via ${selection.source}. Describe or quote the selection as relevant.`,
			};
		}

		// mode='selection-or-frame' with selection present — return selection only,
		// no frame (the original #1389 conditional-frame behavior: frame captured
		// ONLY when there is no selection).
		if (mode === 'selection-or-frame' && selection) {
			return {
				status: 'selection_read',
				text: selection.text,
				source: selection.source,
				question: question ?? null,
				_note: question
					? `Selected text read via ${selection.source} (exact, not frame-eyeballing). Answer: ${question}`
					: `Selected text read via ${selection.source}. Describe or quote the selection as relevant.`,
			};
		}

		// mode='both' with selection present — return BOTH selection and frame.
		if (mode === 'both' && selection) {
			const r = await _captureSendFrame('screen');
			return {
				status: 'selection_and_frame',
				selection: { text: selection.text, source: selection.source },
				frame_status: r.ok ? 'ok' : 'failed',
				frame_source: r.ok ? r.source : null,
				frame_error: r.ok ? null : (r.error ?? null),
				question: question ?? null,
				_note: question
					? `Selected text read via ${selection.source}; frame also in your vision context. Answer: ${question}`
					: `Selected text read via ${selection.source}; frame also in your vision context. Quote the selection and describe the surrounding context as relevant.`,
			};
		}

		// mode='frame', or mode='both'/'selection-or-frame' with no selection —
		// capture a frame only.
		if (wantFrame) {
			const r = await _captureSendFrame('screen');
			if (!r.ok) {
				return {
					status: 'failed',
					error: r.error,
					hint: 'Screen-capture server may not be running. Start it with `bash src/startup.sh`.',
				};
			}
			const fellThrough = mode === 'both' || mode === 'selection-or-frame';
			return {
				status: 'frame_captured',
				source: r.source,
				question: question ?? null,
				_note: question
					? (fellThrough
						? `No selection found; frame is in your vision context. Answer: ${question}`
						: `Frame is in your vision context. Answer: ${question}`)
					: 'Frame is in your vision context. Describe what you see.',
			};
		}

		// Unreachable in practice — exhaustive guard for the mode enum.
		return { status: 'no_op', question: question ?? null };
	},
};

// --- take_note --------------------------------------------------------------
//
// Save an observation to notes/ with screen-companion frontmatter tag.
// Uses the workspace notes dir (same as save_note inline tool).

const takeNoteTool: ToolDefinition = {
	name: 'take_note',
	description:
		'Save an observation or note during a screen-companion session. ' +
		'Use when the user says "remember this", "note that", or when you observe something worth keeping ' +
		'(e.g. "user got the bot token at 15:42"). ' +
		'Saves to notes/ with a screen-companion tag and ISO timestamp.',
	parameters: z.object({
		content: z.string().describe('The observation or note to save. Plain text, 1–3 sentences.'),
		title: z.string().optional().describe('Short title for the note. Auto-generated from content if omitted.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { content, title } = (args ?? {}) as { content: string; title?: string };
		const date = new Date();
		const dateStr = date.toISOString().slice(0, 10);
		const timeStr = date.toISOString().slice(0, 19).replace('T', ' ');
		const autoTitle = title ?? content.slice(0, 60).replace(/[^a-zA-Z0-9 ]/g, '').trim();
		const slug = `sc-${date.toISOString().slice(0, 16).replace(/[T:]/g, '-')}-${autoTitle.toLowerCase().slice(0, 30).replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '')}`;
		const md = `---\ntitle: "${autoTitle}"\ndate: ${dateStr}\ntags: [screen-companion, observation]\n---\n\n*Recorded at ${timeStr}*\n\n${content}\n`;
		try {
			const notesDir = join(resolveWorkspace(), 'notes');
			mkdirSync(notesDir, { recursive: true });
			writeFileSync(join(notesDir, `${slug}.md`), md);
			return { status: 'saved', title: autoTitle, slug, path: `notes/${slug}.md` };
		} catch (e) {
			return { status: 'failed', error: String(e) };
		}
	},
};

// --- look_up_reference ------------------------------------------------------
//
// Look up documentation or reference material for a setting/API/UI element.
// v1: web search via DuckDuckGo Instant Answer API (no key required), then
// fall back to fetching the top result URL. Returns a short summary.

const lookUpReferenceTool: ToolDefinition = {
	name: 'look_up_reference',
	description:
		'Look up documentation or reference material for a setting, API field, or UI element the user is confused about. ' +
		'Use when the user asks what a field means, what value to enter, or what a permission does. ' +
		'E.g. "What is the \'Bot Token Type\' field in Discord?", "What OAuth scopes do I need for reading DMs?". ' +
		'Returns a short authoritative summary.',
	parameters: z.object({
		query: z.string().describe('The question or term to look up. Be specific: include the product name and context. E.g. "Discord bot token type field meaning".'),
	}),
	execution: 'inline',
	async execute(args) {
		const { query } = (args ?? {}) as { query: string };
		try {
			// DuckDuckGo Instant Answer API — no key, returns JSON with Abstract field
			const ddgUrl = `https://api.duckduckgo.com/?q=${encodeURIComponent(query)}&format=json&no_html=1&skip_disambig=1`;
			const ddgRes = await fetch(ddgUrl, { headers: { 'User-Agent': 'Sutando/1.0' }, signal: AbortSignal.timeout(8_000) });
			const ddg = await ddgRes.json() as { Abstract?: string; AbstractURL?: string; RelatedTopics?: Array<{ Text?: string; FirstURL?: string }> };

			if (ddg.Abstract && ddg.Abstract.length > 20) {
				return {
					status: 'found',
					summary: ddg.Abstract,
					source_url: ddg.AbstractURL ?? null,
					query,
				};
			}

			// Fallback: use top RelatedTopic text
			const related = ddg.RelatedTopics?.find(t => t.Text && t.Text.length > 20);
			if (related?.Text) {
				return {
					status: 'found',
					summary: related.Text,
					source_url: related.FirstURL ?? null,
					query,
				};
			}

			return {
				status: 'not_found',
				query,
				hint: `No instant answer found. Suggest the user search "${query}" in their browser, or ask them to read the relevant field label aloud so you can explain from context.`,
			};
		} catch (err) {
			return {
				status: 'failed',
				error: (err as Error)?.message ?? String(err),
				hint: 'Reference lookup failed (network or API error). Explain from context or ask the user to read the field aloud.',
			};
		}
	},
};

export const tools: ToolDefinition[] = [activateScreenCompanionTool, deactivateScreenCompanionTool, visionQueryTool, takeNoteTool, lookUpReferenceTool];
