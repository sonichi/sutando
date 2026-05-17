#!/usr/bin/env npx tsx
/**
 * Screen Companion — config loader + activation stub (v0).
 *
 * v0 scope:
 * - Discover configs in skills/screen-companion/configs/*.yaml
 * - Load one by name, validate the required fields, print what WOULD happen
 * - No wiring to voice-agent yet — that's the next PR.
 *
 * The goal of v0 is to crystallize the config schema against a real
 * concrete config (guided-setup.yaml) so the contract is testable
 * before committing to the voice-agent integration shape.
 *
 * CLI:
 *   npx tsx scripts/activate.ts --list
 *   npx tsx scripts/activate.ts --config guided-setup --goal "find the bot token in Discord dev portal"
 *
 * YAML parsing: spawn python3 (avoids adding js-yaml as an npm dep —
 * same pattern as src/oc-profile-catalog.ts).
 */

import { discoverConfigs, loadConfig, parseYaml, validateConfig, renderGoal, type ScreenCompanionConfig } from './load-config.js';

function cliArg(name: string): string | undefined {
	const i = process.argv.indexOf(`--${name}`);
	if (i < 0) return undefined;
	const next = process.argv[i + 1];
	return next?.startsWith('--') ? '' : next;
}

function printActivation(config: ScreenCompanionConfig, goal: string | undefined): void {
	console.log('━'.repeat(72));
	console.log(`Screen Companion — activating: ${config.name}`);
	console.log('━'.repeat(72));
	console.log();
	console.log(`Activation triggers:`);
	console.log(`  voice phrases:  ${config.activation.voice_phrases.map(p => `"${p}"`).join(', ')}`);
	console.log(`  button label:   "${config.activation.button_label}"`);
	console.log(`  CLI alias:      ${config.activation.cli_alias}`);
	console.log();
	console.log(`Vision:`);
	console.log(`  mode:           ${config.vision_mode}`);
	if (config.vision_cadence_ms !== undefined) {
		console.log(`  cadence:        ${config.vision_cadence_ms}ms`);
	}
	console.log();
	console.log(`Tools allowed (${config.tools_allow.length}):`);
	for (const t of config.tools_allow) console.log(`  • ${t}`);
	console.log();
	if (config.goal_template !== undefined) {
		const filled = goal !== undefined
			? config.goal_template.replace('{goal}', goal)
			: config.goal_template;
		console.log(`Goal: ${filled}`);
		console.log();
	}
	console.log('System prompt overlay:');
	console.log('─'.repeat(72));
	console.log(config.system_prompt_overlay.trimEnd());
	console.log('─'.repeat(72));
	console.log();
	console.log('[v0 stub] Next PR wires this into voice-agent.ts:');
	console.log('  1. Push the system_prompt_overlay onto the active VoiceSession');
	console.log('  2. Restrict the tool surface to tools_allow');
	console.log('  3. Set vision push/pull cadence');
	console.log('  4. Emit a one-line confirmation back to the user via voice');
}

function main(): void {
	if (process.argv.includes('--list')) {
		const all = discoverConfigs();
		if (all.length === 0) {
			console.log('No configs found.');
			return;
		}
		console.log('Configs:');
		for (const c of all) {
			try {
				const parsed = validateConfig(parseYaml(c.path), c.path);
				console.log(`  ✓ ${c.name}  — "${parsed.activation.voice_phrases[0]}"`);
			} catch (e) {
				console.log(`  ✗ ${c.name}  — ${(e as Error).message}`);
			}
		}
		return;
	}

	const name = cliArg('config');
	if (!name) {
		console.error('Usage: activate.ts --config <name> [--goal "..."]');
		console.error('       activate.ts --list');
		process.exit(1);
	}

	let config: ScreenCompanionConfig;
	try {
		config = loadConfig(name);
	} catch (e) {
		console.error((e as Error).message);
		process.exit(1);
	}

	const goal = cliArg('goal');
	printActivation(config, goal);
}

main();
