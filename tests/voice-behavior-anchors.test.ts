/**
 * Step-5 behavior anchors (interaction-planes refactor).
 *
 * Since 5a-1 the tuned factories live in the importable
 * src/voice-agent-config.ts, so the anchors assert REAL factory output
 * (upgraded from the pre-move source-hash tripwires):
 *
 * 1. Tool-table anchor — name, description, execution mode, parameter shape
 *    of every importable tool. These strings ARE the tuned prompt surface.
 * 2. Instructions anchor — buildInstructions() with a fixed context and
 *    deterministic overrides must contain the committed 87-entry tuned
 *    string sequence IN ORDER, plus the exact conditional variants
 *    (meeting on/off, googleSearch on/off, mode marker placement).
 *    Env-dependent segments (voice context files, stand identity, repo URL)
 *    are pinned via overrides; buildVoiceAgentContext output is asserted
 *    present but not snapshotted (it legitimately varies per install).
 * 3. Greeting anchor — exact meeting-mode string; structural assertions on
 *    the fresh-connect and reconnect paths (which read per-machine workspace
 *    state and can't be byte-snapshotted portably).
 * 4. Source tripwire — the tool-table composition line in voice-agent.ts.
 *
 * Regenerate (deliberately): ANCHOR_UPDATE=1 npx tsx tests/voice-behavior-anchors.test.ts
 * Run: npx tsx --test tests/voice-behavior-anchors.test.ts
 */
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import assert from 'node:assert';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const REPO = join(fileURLToPath(new URL('.', import.meta.url)), '..');
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

const inlineMod = await import('../src/inline-tools.js');
const bridgeMod = await import('../src/task-bridge.js');
const cfg = await import('../src/voice-agent-config.js');

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const anyInline = inlineMod as any;
// Env-dependent tools (manifest-loaded per install + presenter conditionals)
// are excluded: CI has no personal skill manifests, so anchoring them makes
// the fixture machine-specific (learned from the first CI run of this test).
const envDep: ReadonlySet<string> = anyInline.envDependentToolNames ?? new Set();
const importableTools = [
	bridgeMod.workTool,
	...(anyInline.inlineTools ?? []),
	...(anyInline.ownerOnlyTools ?? []),
].filter(Boolean).filter((t: { name: string }) => !envDep.has(t.name));

const MODE = { marker: '\n\n[MODE-MARKER-FIXED]', isMeeting: false, isPresenter: false };
const MEETING_MODE = { marker: '\n\n[MODE-MARKER-MEETING]', isMeeting: true, isPresenter: false };

function ctx(overrides: Partial<{ meeting: boolean; googleSearch: boolean }> = {}) {
	const meeting = overrides.meeting ?? false;
	return {
		resolveCurrentMode: () => (meeting ? MEETING_MODE : MODE),
		isMeetingActive: () => meeting,
		googleSearch: overrides.googleSearch ?? true,
		resetSessionGates: () => {},
		resetNoteViewingDebounce: () => {},
		getRecentConversation: () => '',
		getSecondsSinceLastTurn: () => null,
	};
}
const OVERRIDES = {
	standIdentityJson: '{"name":"AnchorStand","nameOrigin":"fixture"}',
	voiceContext: '[ANCHOR-VOICE-CONTEXT]',
	repoUrl: 'https://github.com/sonichi/sutando',
	voiceAgentContext: '[ANCHOR-AGENT-CONTEXT]',
};

const instructions = cfg.buildInstructions(ctx(), OVERRIDES);
const instructionLines = instructions.split('\n');

// The prompt embeds the INSTALLED tool list (deliberately — it tells Gemini
// what exists), so a raw hash can never be env-stable. Mask the two dynamic
// segments before hashing: the joined inline-tool-names line, and each
// per-tool "- name: desc. Instant." line. Everything else — the actual tuned
// prose — is pinned exactly.
function stableInstructions(text: string): string {
	return text.split('\n').filter(l =>
		!(l.startsWith('- ') && l.endsWith(' — call these directly, not through work. Instant.'))
		&& !(l.startsWith('- ') && l.endsWith('. Instant.'))
	).join('\n');
}

// The tuned static entries, in order (multi-line entries appear as their
// constituent lines; conditional/dynamic segments are asserted separately).
const va = readFileSync(join(REPO, 'src', 'voice-agent.ts'), 'utf-8');
const anchors = {
	note: 'Step-5 behavior anchors (post-5a-1: real factory output). Deliberate prompt changes must regenerate via ANCHOR_UPDATE=1 with the diff called out in the PR.',
	tools: importableTools.map(toolAnchor)
		.sort((a, b) => String(a.name).localeCompare(String(b.name))),
	instructions_hash_fixed_env: createHash('sha256').update(stableInstructions(instructions)).digest('hex'),
	meeting_greeting: cfg.buildGreeting(ctx({ meeting: true })),
	regions: {
		tool_table: createHash('sha256').update(
			(() => { const i = va.indexOf('const mainAgentTools'); return va.slice(i, va.indexOf('\n', i)); })()).digest('hex'),
	},
};

if (UPDATE || !existsSync(FIXTURE)) {
	writeFileSync(FIXTURE, JSON.stringify(anchors, null, 1) + '\n');
	console.log(`anchor fixture ${UPDATE ? 'regenerated' : 'created'}: ${FIXTURE} (${anchors.tools.length} tools)`);
}
const expected = JSON.parse(readFileSync(FIXTURE, 'utf-8'));

test('tool-table anchor matches the committed fixture', () => {
	assert.deepStrictEqual(anchors.tools, expected.tools);
});

test('instructions anchor: fixed-env output hash matches (dynamic tool lines masked)', () => {
	assert.strictEqual(anchors.instructions_hash_fixed_env, expected.instructions_hash_fixed_env);
});

test('instructions: dynamic tool lines exist and are well-formed', () => {
	assert.ok(instructionLines.some(l => l.endsWith(' — call these directly, not through work. Instant.')),
		'joined inline-tool-names line present');
	assert.ok(instructionLines.filter(l => /^- [a-z_]+: .+\. Instant\.$/.test(l)).length >= 15,
		'per-tool description lines present');
});

test('instructions: injected seams land where tuned', () => {
	assert.ok(instructions.startsWith(MODE.marker), 'mode marker must be the FIRST instruction line (authoritative placement)');
	assert.ok(instructions.includes('Your Stand name is AnchorStand.'), 'stand identity line');
	assert.ok(instructions.includes('[ANCHOR-VOICE-CONTEXT]'), 'voice context block');
	assert.ok(instructions.includes('[ANCHOR-AGENT-CONTEXT]'), 'agent context block');
	assert.ok(instructions.includes('The Sutando GitHub repo is https://github.com/sonichi/sutando.'), 'repo line');
	assert.ok(instructions.includes('- Google Search for current-info queries'), 'googleSearch=true line');
});

test('instructions: anti-fabrication + feedback-routing guardrails present', () => {
	assert.ok(instructions.includes('NEVER claim you have completed, filed, submitted, sent, opened, or reported anything unless you actually invoked a tool that did it'),
		'anti-fabrication guardrail line');
	assert.ok(instructions.includes('never narrate a fake success'),
		'anti-fabrication: no fake success');
	assert.ok(instructions.includes('FILE FEEDBACK / REPORT A BUG:') && instructions.includes('the core agent files it via the report-feedback skill'),
		'feedback requests routed through work');
});

test('instructions: googleSearch=false omits the search line (capability honesty)', () => {
	const off = cfg.buildInstructions(ctx({ googleSearch: false }), OVERRIDES);
	assert.ok(!off.includes('- Google Search for current-info queries'));
});

test('instructions: meeting-active swaps BOTH conditional rules', () => {
	const meeting = cfg.buildInstructions(ctx({ meeting: true }), OVERRIDES);
	assert.ok(meeting.includes('⚠️ MEETING MODE IS CURRENTLY ACTIVE.'));
	assert.ok(meeting.includes('- IN MEETING MODE: When addressed by name, answer DIRECTLY'));
	assert.ok(!meeting.includes('- When in doubt, call work.'));
	assert.ok(instructions.includes('- When in doubt, call work.'), 'non-meeting keeps the default rule');
});

test('greeting: meeting-mode string is exact', () => {
	assert.strictEqual(anchors.meeting_greeting, expected.meeting_greeting);
});

test('greeting: fresh-connect shape (env-dependent hints excluded)', () => {
	const g = cfg.buildGreeting(ctx());
	assert.ok(g.startsWith('[System: A user just connected. Say hi and introduce yourself as Sutando'));
	assert.ok(g.endsWith(MODE.marker), 'ends with the mode marker');
});

test('greeting: reconnect replay guard preserved', () => {
	const g = cfg.buildGreeting({ ...ctx(), getRecentConversation: () => 'User: hello\nSutando: hi' });
	assert.ok(g.startsWith('[System: The user reconnected. The block below is REPLAYED HISTORY'));
	assert.ok(g.includes('User: hello\nSutando: hi'));
	assert.ok(g.includes('[Now say "Welcome back" briefly — one sentence — and then stop and wait for input.]'));
});

test('source tripwire: tool-table composition line unchanged', () => {
	assert.deepStrictEqual(anchors.regions, expected.regions);
});

test('anchor breadth: importable tool table is non-trivial', () => {
	assert.ok(anchors.tools.length >= 10, `only ${anchors.tools.length} tools anchored`);
	assert.ok(instructionLines.length > 80, 'instructions non-trivially sized');
});
