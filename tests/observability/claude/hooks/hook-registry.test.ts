import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { hookMap } from '../../../../src/observability/claude/hook-map.js';
import type { MapContext } from '../../../../src/observability/claude/cc-records.js';
import type { ClaudeCodeHook } from '../../../../src/observability/claude/cc-hooks.js';

// Conformance: the machine-readable hook registry (hook-registry.json), the
// actual settings registration (build-hook-settings.mjs) and the actual mapper
// (hook-map.ts) must agree. The registry is the contract's authoritative table
// (docs/runtime/claude-hook-contract-v1.md) — this test is what keeps it from
// silently drifting into fiction when either code side moves.

interface RegistryEntry {
	registered: boolean;
	modeled: boolean;
	normalizedKinds: string[];
	pairedKinds?: string[];
	visibility: string;
	redactionReviewed?: boolean;
}
interface Registry {
	events: Record<string, RegistryEntry>;
	unknownEvent: { normalizedKindPattern: string };
}

const HOOKS_DIR = '../../../../src/observability/claude/hooks/';
const REGISTRY: Registry = JSON.parse(
	readFileSync(fileURLToPath(new URL(`${HOOKS_DIR}hook-registry.json`, import.meta.url)), 'utf8'),
);
const BUILDER = fileURLToPath(new URL(`${HOOKS_DIR}build-hook-settings.mjs`, import.meta.url));

const ctx: MapContext = { node: 'test-node', receivedAt: 1_753_000_000 };
const kindsOf = (hook: ClaudeCodeHook): string[] => hookMap(hook, ctx).events.map((e) => e.kind);

// Minimal fixture per modeled event, using a NON-file tool for the Post* cases
// so the primary mapping is isolated from the paired file.* emission.
const FIXTURES: Record<string, ClaudeCodeHook> = {
	UserPromptSubmit: { hook_event_name: 'UserPromptSubmit', session_id: 's', prompt: 'hi' },
	UserPromptExpansion: { hook_event_name: 'UserPromptExpansion', session_id: 's' },
	MessageDisplay: { hook_event_name: 'MessageDisplay', session_id: 's', delta: 'chunk' },
	PreToolUse: { hook_event_name: 'PreToolUse', session_id: 's', tool_name: 'Bash', tool_input: { command: 'ls' } },
	PostToolUse: { hook_event_name: 'PostToolUse', session_id: 's', tool_name: 'Bash', tool_input: { command: 'ls' }, tool_response: 'ok' },
	PostToolUseFailure: { hook_event_name: 'PostToolUseFailure', session_id: 's', tool_name: 'Bash', tool_input: { command: 'ls' }, error: 'boom' },
	Stop: { hook_event_name: 'Stop', session_id: 's' },
	SessionStart: { hook_event_name: 'SessionStart', session_id: 's', source: 'startup', model: 'm' },
	SessionEnd: { hook_event_name: 'SessionEnd', session_id: 's', end_reason: 'clear' },
	PreCompact: { hook_event_name: 'PreCompact', session_id: 's', trigger: 'auto' },
	Notification: { hook_event_name: 'Notification', session_id: 's', notification_type: 'permission' },
	SubagentStart: { hook_event_name: 'SubagentStart', session_id: 's', agent_type: 'Explore' },
	SubagentStop: { hook_event_name: 'SubagentStop', session_id: 's', agent_type: 'Explore' },
	TaskCreated: { hook_event_name: 'TaskCreated', session_id: 's', task_id: 't1', task_title: 'x' },
	TaskCompleted: { hook_event_name: 'TaskCompleted', session_id: 's', task_id: 't1' },
};

describe('hook-registry conformance', () => {
	it('registered:true set === the keys build-hook-settings.mjs actually registers', () => {
		const settings = JSON.parse(execFileSync('node', [BUILDER, '/tmp/obs-hook.sh'], { encoding: 'utf8' }));
		const registeredInSettings = Object.keys(settings.hooks).sort();
		const registeredInRegistry = Object.entries(REGISTRY.events)
			.filter(([, v]) => v.registered)
			.map(([k]) => k)
			.sort();
		assert.deepEqual(registeredInRegistry, registeredInSettings);
	});

	it('every modeled event has a registry entry and a fixture', () => {
		const modeled = Object.entries(REGISTRY.events)
			.filter(([, v]) => v.modeled)
			.map(([k]) => k);
		for (const name of modeled) {
			assert.ok(FIXTURES[name], `modeled event ${name} is missing a conformance fixture`);
		}
		// and no fixture exists for an event the registry doesn't know
		for (const name of Object.keys(FIXTURES)) {
			assert.ok(REGISTRY.events[name], `fixture ${name} has no registry entry`);
		}
	});

	it('mapper emits exactly the registry normalizedKinds for each modeled event', () => {
		for (const [name, entry] of Object.entries(REGISTRY.events)) {
			if (!entry.modeled) continue;
			assert.deepEqual(
				kindsOf(FIXTURES[name]),
				entry.normalizedKinds,
				`kinds mismatch for ${name}`,
			);
		}
	});

	it('paired file.* kinds stay within the registry pairedKinds', () => {
		const fileCase = kindsOf({
			hook_event_name: 'PostToolUse',
			session_id: 's',
			tool_name: 'Read',
			tool_input: { file_path: '/tmp/x' },
			tool_response: 'c',
		});
		const entry = REGISTRY.events.PostToolUse;
		assert.deepEqual(fileCase[0], entry.normalizedKinds[0]);
		for (const extra of fileCase.slice(1)) {
			assert.ok(entry.pairedKinds.includes(extra), `unregistered paired kind ${extra}`);
		}
	});

	it('unknown events map per the registry unknownEvent pattern', () => {
		const kinds = kindsOf({ hook_event_name: 'BrandNewVendorThing', session_id: 's' } as ClaudeCodeHook);
		assert.deepEqual(kinds, ['cc.hook.brand_new_vendor_thing']);
		assert.equal(REGISTRY.unknownEvent.normalizedKindPattern, 'cc.hook.<snake(hook_event_name)>');
	});

	it('registry hygiene: visibility enum; no product-tier entry without redaction review', () => {
		for (const [name, entry] of Object.entries(REGISTRY.events)) {
			assert.ok(['product', 'diagnostic', 'raw'].includes(entry.visibility), `bad visibility on ${name}`);
			if (entry.visibility === 'product') {
				// Promoting a kind to product tier requires an explicit redaction
				// review (contract §5) — recorded in the registry, enforced here.
				assert.equal(entry.redactionReviewed, true, `${name} promoted to product without redactionReviewed`);
			}
		}
	});
});
