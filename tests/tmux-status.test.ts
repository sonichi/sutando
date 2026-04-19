import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { parseTmuxPane, readTmuxStatus, _resetTmuxCacheForTests } from '../src/tmux-status.js';

/**
 * Tests for `parseTmuxPane` — the pane-capture parser used as a fallback
 * signal for `effectiveAgentState()` when `core-status.json` is stale.
 *
 * Observer-effect aside (noted in the design collab 2026-04-18): any harness
 * that calls the parser while itself running under Claude Code's tmux pane
 * will find the harness's own Bash invocation in the capture. Canned
 * fixtures sidestep that entirely — each test passes a synthesized pane
 * string and asserts the parser's return shape, no live CLI needed.
 *
 * Fixtures are inlined as TS string constants (tests/fixtures/ is gitignored
 * per repo convention; see discussion on feat/tmux-status-fallback).
 */

// ── Fixtures (synthesized pane captures) ────────────────────────────────────

const TOOL_IN_PROGRESS = `⏺ Bash(npm install 2>&1 | tail -6)
  ⎿  Running…
✳ Nebulizing… (16s · ↓ 111 tokens · thought for 5s)
──── sutando-core ──
❯ `;

const TOOL_BG = `⏺ Bash(bash src/watch-tasks.sh)
  ⎿  Running in the background (↓ to manage)
──── sutando-core ──
❯ `;

const TOOL_JUST_FINISHED = `⏺ Read(~/Desktop/sutando/README.md)
  ⎿  Read 23 lines
⏺ All done.
  Read 1 file, listed 1 directory (ctrl+o to expand)
──── sutando-core ──
❯ `;

const IDLE_PROMPT = `⏺ All done.
  Read 1 file, listed 1 directory (ctrl+o to expand)
──── sutando-core ──
❯ `;

const THINKING_ONLY = `✳ Cogitated for 7s
──── sutando-core ──
❯ `;

const THINKING_THOUGHT_FOR = `✳ Pondering… (8s · ↓ 42 tokens · thought for 4s)
──── sutando-core ──
❯ `;

const MULTIPLE_TOOLS_LAST_WINS = `⏺ Read(foo.ts)
  ⎿  Read 12 lines
⏺ Grep(pattern=\"hello\")
  ⎿  1 match
⏺ Edit(foo.ts)
  ⎿  Running…
──── sutando-core ──
❯ `;

const ALT_BULLET_GLYPH = `● WebSearch(query=\"gemini pricing\")
  ⎿  Running in the background
──── sutando-core ──
❯ `;

const AMBIGUOUS_UNRELATED = `some random shell output
not a claude-code pane at all
$ ls foo bar
foo: no such file
`;

const EMPTY = '';
const WHITESPACE_ONLY = '   \n\t\n  \n';

// ── Tests ──────────────────────────────────────────────────────────────────

describe('parseTmuxPane', () => {
	beforeEach(() => _resetTmuxCacheForTests());

	it('tool-in-progress → working with tool name', () => {
		const r = parseTmuxPane(TOOL_IN_PROGRESS);
		assert.equal(r.state, 'working');
		assert.equal(r.label, 'Bash');
	});

	it('background tool → working with tool name (not label=thinking)', () => {
		const r = parseTmuxPane(TOOL_BG);
		assert.equal(r.state, 'working');
		assert.equal(r.label, 'Bash');
	});

	it('tool just finished (no Running/BG marker) → idle', () => {
		const r = parseTmuxPane(TOOL_JUST_FINISHED);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});

	it('idle prompt with no tool markers → idle', () => {
		const r = parseTmuxPane(IDLE_PROMPT);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});

	it('thinking (Cogitated for Ns) → working label=thinking', () => {
		const r = parseTmuxPane(THINKING_ONLY);
		assert.equal(r.state, 'working');
		assert.equal(r.label, 'thinking');
	});

	it('thinking (thought for Ns) → working label=thinking', () => {
		const r = parseTmuxPane(THINKING_THOUGHT_FOR);
		assert.equal(r.state, 'working');
		assert.equal(r.label, 'thinking');
	});

	it('multiple tools → label is the LAST tool in the pane', () => {
		const r = parseTmuxPane(MULTIPLE_TOOLS_LAST_WINS);
		assert.equal(r.state, 'working');
		assert.equal(r.label, 'Edit');
	});

	it('alt bullet glyph (●) recognized as tool marker', () => {
		const r = parseTmuxPane(ALT_BULLET_GLYPH);
		assert.equal(r.state, 'working');
		assert.equal(r.label, 'WebSearch');
	});

	it('ambiguous / non-Claude-Code output → idle (silent fallback)', () => {
		const r = parseTmuxPane(AMBIGUOUS_UNRELATED);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});

	it('empty string → idle (never throws)', () => {
		const r = parseTmuxPane(EMPTY);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});

	it('whitespace-only → idle', () => {
		const r = parseTmuxPane(WHITESPACE_ONLY);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});

	it('null input → idle (defensive)', () => {
		// @ts-expect-error — contract explicitly says never throw on malformed input
		const r = parseTmuxPane(null);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});

	it('undefined input → idle (defensive)', () => {
		// @ts-expect-error
		const r = parseTmuxPane(undefined);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});

	it('non-string input → idle (defensive)', () => {
		// @ts-expect-error
		const r = parseTmuxPane(42);
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
	});
});

describe('readTmuxStatus', () => {
	beforeEach(() => _resetTmuxCacheForTests());

	it('SUTANDO_TMUX_SCRAPE=0 kill-switch → idle, no cache touch', () => {
		const prev = process.env.SUTANDO_TMUX_SCRAPE;
		process.env.SUTANDO_TMUX_SCRAPE = '0';
		try {
			const r = readTmuxStatus();
			assert.equal(r.state, 'idle');
			assert.equal(r.label, '');
		} finally {
			if (prev === undefined) delete process.env.SUTANDO_TMUX_SCRAPE;
			else process.env.SUTANDO_TMUX_SCRAPE = prev;
		}
	});

	it('cold-start cache miss returns idle fallback without blocking for capture timeout', () => {
		// With cache cleared and no refresh yet complete, the sync call must
		// return with the idle fallback WITHOUT blocking on the full
		// execFile capture — the whole point of the async refactor. The old
		// execSync path blocked for up to CAPTURE_TIMEOUT_MS (500ms) per
		// call; the new path fires an async refresh and returns immediately.
		const start = Date.now();
		const r = readTmuxStatus();
		const elapsed = Date.now() - start;
		assert.equal(r.state, 'idle');
		assert.equal(r.label, '');
		// Threshold generous for slow CI containers. The regression-signal
		// we care about is: if this ever climbs to ~500ms, someone
		// accidentally reverted to the sync path.
		assert.ok(elapsed < 300, `readTmuxStatus blocked for ${elapsed}ms (should be <300ms)`);
	});
});
