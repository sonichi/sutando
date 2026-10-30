// A page rewrite is work, not a write_page append. 2026-10-21 17:36:55 and 17:39:35 (voice-agent.log): asked to
// replace the v2 page with another design, and to delete it and make a v3, the voice model appended 247 and then
// 496 characters to the v2 page, and the turn check never looked (write_page counted as answering the turn).
// Run: npx tsx --test --test-force-exit tests/voice-page-rewrite-is-work.test.ts
import { describe, it, test, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-page-rewrite-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const MONTH = new Date().toISOString().slice(0, 7);
for (const d of ['tasks', 'results', join('tasks', 'archive', MONTH), join('results', 'archive', MONTH), join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const { RelayAgent, createVoiceTaskStore } = await import('../src/relay-agent.js');
const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('a turn with a write_page append is still judged by the turn check', () => {
	it('search_commons then write_page (the 17:39:35 shape): the turn check runs and its work is sent', async () => {
		const store = createVoiceTaskStore();
		const judged: string[] = [];
		const submitted: string[] = [];
		const relay = new RelayAgent({
			store,
			classify: async (input) => { judged.push(input.utterance); return { action: 'work', task: 'Replace the v2 plan page in "Susan, Chi" with a v3 built from Dot\'s design' }; },
			recentConversation: () => '',
			notice: () => {},
			submit: async (args) => { submitted.push(String(args.task)); store.add('task-r1', String(args.task)); return { status: 'pending', taskId: 'task-r1', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		// The voice agent's onToolCall wiring: a page write is noted as a write; before this fix there was no such
		// note and write_page went to noteAnsweringTool like a read.
		const r = relay as unknown as { noteWritingTool?: (n: string) => void; noteAnsweringTool: (n: string) => void };
		const noteWrite = (r.noteWritingTool ?? r.noteAnsweringTool).bind(relay);
		relay.noteAnsweringTool('search_commons');
		noteWrite('write_page');
		relay.onUserTurn('Replace the current V2 with that plan. Delete it and add a new V3.');
		await tick(10);
		assert.deepEqual(judged, ['Replace the current V2 with that plan. Delete it and add a new V3.'], 'the turn was judged');
		assert.deepEqual(submitted, ['Replace the v2 plan page in "Susan, Chi" with a v3 built from Dot\'s design']);
	});

	it('a read alone still answers the turn, and the voice model\'s own work call still takes it', async () => {
		const store = createVoiceTaskStore();
		let judged = 0;
		const submitted: string[] = [];
		const relay = new RelayAgent({
			store,
			classify: async () => { judged++; return { action: 'work', task: 'x' }; },
			notice: () => {},
			submit: async (args) => { submitted.push(String(args.task)); return { status: 'pending', taskId: `task-${submitted.length}`, queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		relay.noteAnsweringTool('read_page');
		relay.onUserTurn('what does the v2 page say');
		await tick(10);
		assert.equal(judged, 0);
		void relay.invoke('w', { task: 'make a v3 page' });
		await tick(1);
		(relay as unknown as { noteWritingTool?: (n: string) => void }).noteWritingTool?.('write_page');
		relay.onUserTurn('make a v3 page');
		await tick(10);
		assert.equal(judged, 0, 'the work call took the turn; a write in it does not send it twice');
		assert.deepEqual(submitted, ['make a v3 page']);
	});

	it('the voice agent and the skill wire write_page as a write, not an answering tool', () => {
		const agentSrc = readFileSync(join(import.meta.dirname ?? '.', '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(agentSrc, /if \(writingTools\.has\(e\.toolName\)\) relayAgent\.noteWritingTool\(e\.toolName\);\s*else if \(answeringTools\.has\(e\.toolName\)\) relayAgent\.noteAnsweringTool\(e\.toolName\)/);
		assert.match(agentSrc, /const writingTools = new Set\(personalVoiceSurface\.writingTools\)/);
		const toolsSrc = readFileSync(join(process.cwd(), 'skills', 'ag2space-voice', 'tools.ts'), 'utf-8');
		assert.match(toolsSrc, /\.\.\.roomRead\.tools\.map\(\(t\) => t\.name\)\.filter\(\(n\) => n !== 'write_page'\)/);
		assert.match(toolsSrc, /writingTools: roomRead\.tools\.some\(\(t\) => t\.name === 'write_page'\) \? \['write_page'\] : \[\]/);
	});
});

// The plugin is optional: with skills/ag2space-voice/ removed this part skips.
const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
if (!existsSync(join(PLUGIN_DIR, 'room-read.ts'))) test('ag2space-voice plugin not installed', { skip: true }, () => {});
else {
	const { writePage, createWritePageTool, ROOM_READ_PROMPT_RULE, ROOM_TURN_CHECK_RULE } = await import(pathToFileURL(join(PLUGIN_DIR, 'room-read.ts')).href);
	const { ROOM_ACTIONS_PROMPT_RULE, ROOM_ACTIONS_TURN_CHECK_RULE } = await import(pathToFileURL(join(PLUGIN_DIR, 'room-actions.ts')).href);
	const client = { async callTool() { return { structuredContent: { status: 'completed', result: {} } }; }, close() {} };
	const idx = { rooms: () => [{ room_id: '!c:x', name: 'Susan, Chi' }], refresh: async () => [{ room_id: '!c:x', name: 'Susan, Chi' }], fetchedAt: () => 1 };
	const deps = { client: () => client as never, currentRoom: () => null, index: idx };

	describe('write_page says it only appends', () => {
		it('an append tells the model plainly how much went at the end and that the page is otherwise unchanged', async () => {
			// 17:39:41: after the 496-char append the model said "I've added the new V3 plan" as if v2 were replaced.
			const r = await writePage({ room: 'Susan, Chi', kind: 'markdown-ycxdn2lw', text: 'V3 plan: Dot\'s flow.' }, deps);
			assert.equal(r.ok && r.did, 'appended');
			assert.equal((r as { note?: string }).note, 'Appended 20 characters at the end of the page; the existing text is unchanged. Nothing was replaced or deleted.');
			const created = await writePage({ room: 'Susan, Chi', title: 'V3', text: 'x' }, deps);
			assert.equal((created as { note?: string }).note, undefined, 'a new page needs no such note');
		});

		it('the description and the rules send a rewrite, a delete or a new version to work', () => {
			const tool = createWritePageTool(deps);
			assert.match(tool.description, /^It only creates a new page or appends a short note to the end of an existing one; it cannot replace, edit or delete any text already there\. A request to replace, rewrite or delete a page's text, or to make a new version of a page, is work, never this\. /);
			assert.match(ROOM_READ_PROMPT_RULE, /To replace, rewrite or delete a page's text, or to make a new version of a document from other messages, videos or pages → work, with the room, the page title and what the new version holds; write_page only appends and cannot do it\./);
			assert.match(ROOM_TURN_CHECK_RULE, /But replacing, rewriting or deleting a page's text, or making a new version of a document from other messages, videos or pages \("replace the v2 with Dot's design", "delete it and make a v3"\), is work, with the room, the page title and what the new version holds: the assistant can only append a short note\./);
			assert.match(ROOM_ACTIONS_PROMPT_RULE, /Replacing, rewriting or deleting a page's text, or making a new version of a document from other messages, videos or pages, is work, not a catalog action\./);
			assert.match(ROOM_ACTIONS_TURN_CHECK_RULE, /Replacing, rewriting or deleting a page's text, or making a new version of a document, is work\./);
		});
	});
}
