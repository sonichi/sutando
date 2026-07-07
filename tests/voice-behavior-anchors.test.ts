/**
 * Step-5 behavior anchors (interaction-planes refactor).
 *
 * Pins the voice agent's externally-tuned behavior surface BEFORE the
 * LiveAgentRuntime extraction moves any code, so every later slice can be
 * verified against a committed fixture instead of trust:
 *
 * 1. Tool-table anchor — name, description, and parameter shape (keys +
 *    per-key description) of every importable tool (inline tools, browser
 *    tools, workTool). These strings ARE the tuned prompt surface Gemini
 *    sees; CLAUDE.md forbids changing them in refactors.
 * 2. Source-region tripwires — SHA-256 of the non-importable tuned regions
 *    in voice-agent.ts (the `instructions:` factory and the greeting
 *    factory; voice-agent.ts executes main() at module load, so it cannot
 *    be imported by tests). A tripwire firing means a tuned region changed:
 *    either revert, or consciously regenerate the fixture in the same PR
 *    with the diff called out.
 *
 * Regenerate (deliberately): ANCHOR_UPDATE=1 npx tsx tests/voice-behavior-anchors.test.ts
 * Run: npx tsx --test tests/voice-behavior-anchors.test.ts
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const REPO = join(new URL('.', import.meta.url).pathname, '..');
const FIXTURE = join(REPO, 'tests', 'fixtures', 'voice-behavior-anchors.json');
const UPDATE = process.env.ANCHOR_UPDATE === '1';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toolAnchor(t: any): Record<string, unknown> {
	const shape = t.parameters?.shape ?? {};
	const params: Record<string, string> = {};
	for (const key of Object.keys(shape).sort()) {
		params[key] = shape[key]?._def?.description ?? '';
	}
	return { name: t.name, description: t.description, execution: t.execution ?? null, params };
}

function sourceRegion(src: string, startMarker: string, endMarker: string): string {
	const start = src.indexOf(startMarker);
	assert.notStrictEqual(start, -1, `region start not found: ${startMarker}`);
	const end = src.indexOf(endMarker, start + startMarker.length);
	assert.notStrictEqual(end, -1, `region end not found: ${endMarker}`);
	return src.slice(start, end);
}

const inlineMod = await import('../src/inline-tools.js');
const bridgeMod = await import('../src/task-bridge.js');

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const anyInline = inlineMod as any;
const importableTools = [
	bridgeMod.workTool,
	...(anyInline.inlineTools ?? []),
	...(anyInline.ownerOnlyTools ?? []),
].filter(Boolean);

const va = readFileSync(join(REPO, 'src', 'voice-agent.ts'), 'utf-8');
const anchors = {
	note: 'Step-5 behavior anchors. A diff here means the tuned prompt surface changed — deliberate changes must regenerate via ANCHOR_UPDATE=1 and call the diff out in the PR.',
	tools: importableTools.map(toolAnchor)
		.sort((a, b) => String(a.name).localeCompare(String(b.name))),
	regions: {
		// The per-session system-instruction factory — the tuned prompt core.
		instructions_factory: createHash('sha256').update(
			sourceRegion(va, '\tinstructions: () => [', '\n\ttools:')).digest('hex'),
		// The greeting/reconnect factory (meeting mode, presenter, replay rules).
		greeting_factory: createHash('sha256').update(
			sourceRegion(va, '\tget greeting()', '\n\tinstructions:')).digest('hex'),
		// The main-agent tool-table composition line.
		tool_table: createHash('sha256').update(
			sourceRegion(va, 'const mainAgentTools', '\n')).digest('hex'),
	},
};

if (UPDATE || !existsSync(FIXTURE)) {
	writeFileSync(FIXTURE, JSON.stringify(anchors, null, 1) + '\n');
	console.log(`anchor fixture ${UPDATE ? 'regenerated' : 'created'}: ${FIXTURE} (${anchors.tools.length} tools)`);
}

const expected = JSON.parse(readFileSync(FIXTURE, 'utf-8'));

test('tool-table anchor: names/descriptions/params match the committed fixture', () => {
	assert.deepStrictEqual(anchors.tools, expected.tools);
});

test('source tripwires: tuned voice-agent regions unchanged', () => {
	assert.deepStrictEqual(anchors.regions, expected.regions);
});

test('anchor breadth: the importable tool table is non-trivial', () => {
	assert.ok(anchors.tools.length >= 10, `only ${anchors.tools.length} tools anchored`);
});
