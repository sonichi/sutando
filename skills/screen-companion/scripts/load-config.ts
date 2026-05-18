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

// LaunchAgent's PATH (`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`) finds
// Homebrew's python3 first, which has no PyYAML — `import yaml` fails at
// runtime when activation is triggered from voice. Try a list of common
// python3 binaries and pick the first one that can import yaml. macOS's
// `/usr/bin/python3` ships PyYAML by default, so the system Python is the
// usual winner here.
const PYTHON_CANDIDATES = ['/usr/bin/python3', '/opt/homebrew/bin/python3', 'python3'];
const YAML_SCRIPT = 'import sys, json, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))';

export function parseYaml(path: string): unknown {
	const errors: string[] = [];
	for (const py of PYTHON_CANDIDATES) {
		const result = spawnSync(py, ['-c', YAML_SCRIPT, path], { encoding: 'utf-8' });
		if (result.error) {
			errors.push(`${py}: ${result.error.message}`);
			continue;
		}
		if (result.status === 0) return JSON.parse(result.stdout);
		errors.push(`${py}: ${result.stderr.trim().split('\n').pop()}`);
	}
	throw new Error(
		`YAML parse failed for ${path}: no python3 with PyYAML found. Tried: ${errors.join('; ')}`,
	);
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
	if (c.vision_mode === 'push') {
		if (typeof c.vision_cadence_ms !== 'number') {
			throw new Error(`${path}: vision_mode=push requires vision_cadence_ms (number)`);
		}
		// Guard YAML typos. 70000 (70s) instead of 700 would silently produce
		// useless cadence; 50 (50ms) would saturate the network. 100–5000 is
		// the practical range for screen-companion modes today.
		if (c.vision_cadence_ms < 100 || c.vision_cadence_ms > 5000) {
			throw new Error(
				`${path}: vision_cadence_ms must be 100–5000ms, got ${c.vision_cadence_ms}`,
			);
		}
	}
	if (!Array.isArray(c.tools_allow) || !c.tools_allow.every(t => typeof t === 'string')) {
		throw new Error(`${path}: tools_allow must be a string[] (got ${typeof c.tools_allow})`);
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
