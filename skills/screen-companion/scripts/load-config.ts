/**
 * Screen Companion — config loader (shared between activate.ts CLI and
 * the inline tool `activate_guided_setup`).
 *
 * YAML parsing: spawn python3 (avoids adding js-yaml as an npm dep —
 * same pattern as src/oc-profile-catalog.ts).
 */

import { readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const SKILL_DIR = join(dirname(fileURLToPath(import.meta.url)), '..');
const CONFIGS_DIR = join(SKILL_DIR, 'configs');

export interface Activation {
	voice_phrases: string[];
	button_label: string;
	cli_alias: string;
}

export interface ScreenCompanionConfig {
	name: string;
	activation: Activation;
	vision_mode: 'push' | 'pull';
	vision_cadence_ms?: number;
	system_prompt_overlay: string;
	tools_allow: string[];
	goal_template?: string;
}

export function parseYaml(path: string): unknown {
	const result = spawnSync(
		'python3',
		['-c', 'import sys, json, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', path],
		{ encoding: 'utf-8' },
	);
	if (result.status !== 0) {
		throw new Error(`YAML parse failed for ${path}: ${result.stderr}`);
	}
	return JSON.parse(result.stdout);
}

export function validateConfig(raw: unknown, path: string): ScreenCompanionConfig {
	if (typeof raw !== 'object' || raw === null) {
		throw new Error(`${path}: config must be an object`);
	}
	const c = raw as Record<string, unknown>;
	const required = ['name', 'activation', 'vision_mode', 'system_prompt_overlay', 'tools_allow'];
	const missing = required.filter(k => !(k in c));
	if (missing.length > 0) {
		throw new Error(`${path}: missing required fields: ${missing.join(', ')}`);
	}
	const a = c.activation as Record<string, unknown>;
	const activationRequired = ['voice_phrases', 'button_label', 'cli_alias'];
	const activationMissing = activationRequired.filter(k => !(k in a));
	if (activationMissing.length > 0) {
		throw new Error(`${path}: activation missing: ${activationMissing.join(', ')}`);
	}
	if (c.vision_mode !== 'push' && c.vision_mode !== 'pull') {
		throw new Error(`${path}: vision_mode must be "push" or "pull", got "${c.vision_mode}"`);
	}
	if (c.vision_mode === 'push' && typeof c.vision_cadence_ms !== 'number') {
		throw new Error(`${path}: vision_mode=push requires vision_cadence_ms (number)`);
	}
	return c as unknown as ScreenCompanionConfig;
}

export function discoverConfigs(): { name: string; path: string }[] {
	if (!existsSync(CONFIGS_DIR)) return [];
	return readdirSync(CONFIGS_DIR)
		.filter(f => f.endsWith('.yaml') || f.endsWith('.yml'))
		.map(f => ({ name: f.replace(/\.ya?ml$/, ''), path: join(CONFIGS_DIR, f) }));
}

export function loadConfig(name: string): ScreenCompanionConfig {
	const all = discoverConfigs();
	const match = all.find(c => c.name === name);
	if (!match) {
		const names = all.map(c => c.name).join(', ') || '(none)';
		throw new Error(`No config named "${name}". Available: ${names}`);
	}
	const raw = parseYaml(match.path);
	return validateConfig(raw, match.path);
}

/**
 * Render the goal_template with the user's actual goal text.
 * Returns the goal string ready to inject into a system prompt overlay.
 */
export function renderGoal(config: ScreenCompanionConfig, goal: string | undefined): string | undefined {
	if (config.goal_template === undefined) return undefined;
	if (goal === undefined) return config.goal_template;
	return config.goal_template.replace('{goal}', goal);
}
