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
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { loadConfig, discoverConfigs, renderGoal } from './scripts/load-config.js';
import { registerVisionOnContributor, callUpdateTools, callRestoreTools } from '../../src/vision-tools.js';

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
const ALWAYS_RETAIN = new Set(['activate_screen_companion', 'deactivate_screen_companion']);

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
			const enforced = callUpdateTools(
				// Import is deferred to avoid a top-level circular dependency;
				// inlineTools is loaded before this module so by the time execute()
				// runs it is already settled.
				(await import('../../src/inline-tools.js')).inlineTools.filter(
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
				vision_mode: config.vision_mode,
				vision_cadence_ms: config.vision_cadence_ms ?? null,
				vision_hint: visionHint,
				activation_message: activationMessage,
				tools_enforced: enforced,
				_note:
					'Say activation_message to the user, then follow `instructions` as your system prompt for the rest of the session. Restrict yourself to the tools in tools_allow (plus mode-exit tools like deactivate_screen_companion). When the user says "exit" / "stop the mode" / "done", call deactivate_screen_companion to restore the full tool surface.',
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
	execute(_args) {
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

export const tools: ToolDefinition[] = [activateScreenCompanionTool, deactivateScreenCompanionTool];
