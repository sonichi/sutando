/**
 * edit_project_file — a project file changed from a short instruction.
 *
 * Pins: the voice model passes only an instruction; the file is read here, a text model plans
 * old_text/new_text edits, and changes.apply gets them on the hash that was read.
 */
import { describe, it, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const PRESENT = existsSync(join(process.cwd(), 'skills', 'ag2space-voice', 'edit-file.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const { applyEdits, droppedFigure, editsForLog, fileRefs, newFileRefs, EDIT_REVERTED_NEXT, parseEditPlan, editPlanPrompt, editProjectFile, createEditFileTool, EDIT_FILE_PROMPT_RULE, EDIT_DONE_NEXT, EDIT_NOT_DONE_NEXT } = await import('../skills/ag2space-voice/edit-file.ts');
type PlanEdits = Parameters<typeof editProjectFile>[2];

const FILE = '#set text(fill: black)\n= My Title\nBody text.\n';
const ok = (v: unknown) => ({ structuredContent: v });
type Call = { name: string; args: Record<string, unknown> };

function fake(exec: (args: Record<string, unknown>, n: number) => unknown = () => ok({ status: 'completed', result: {} }), pages: Array<{ text: string; hash: string }> = [{ text: FILE, hash: 'h1' }]) {
	const calls: Call[] = [];
	let reads = 0, writes = 0;
	const client = {
		async callTool(name: string, args: Record<string, unknown>) {
			calls.push({ name, args });
			if (name === 'room.action.read' && args.action !== 'room.commons.pages.read') return ok({}) as never;
			if (name === 'room.action.read') {
				const p = pages[Math.min(reads++, pages.length - 1)];
				return ok({ kind: 'markdown-qop83g48', title: 'typst.typ', text: p.text, offset: 0, next_offset: null, content_hash: p.hash }) as never;
			}
			return exec(args, writes++) as never;
		},
		close() {},
	};
	const deps = { client: () => client, currentRoom: () => ({ id: '!r:ag2.space', name: 'susan’s agents' }), index: { rooms: () => [], refresh: async () => [], fetchedAt: () => 0 }, log: () => {} };
	return { calls, deps };
}
const greenTitle: PlanEdits = async () => ({ ok: true, edits: [{ old_text: '= My Title', new_text: '#text(fill: green)[= My Title]' }], summary: 'Made the title green' });
const ARGS = { kind: 'markdown-qop83g48', instruction: 'make the title green' };

describe('edit_project_file', () => {
	it('reads the file, applies the planned edits on the hash it read, and asks for an "I have …" report', async () => {
		const f = fake();
		const r = await editProjectFile(ARGS, f.deps, greenTitle);
		assert.ok(r.ok, JSON.stringify(r));
		if (r.ok) { assert.equal(r.summary, 'Made the title green'); assert.equal(r.next, EDIT_DONE_NEXT); }
		const exec = f.calls.filter(c => c.name === 'room.action.execute');
		assert.equal(exec.length, 1);
		assert.equal(exec[0].args.action, 'room.commons.changes.apply');
		assert.deepEqual(exec[0].args.arguments, {
			project: 'markdown-qop83g48', message: 'Made the title green',
			files: [{ page_id: 'markdown-qop83g48', base_hash: 'h1', edits: [{ old_text: '= My Title', new_text: '#text(fill: green)[= My Title]' }] }],
		});
	});

	it('a plan that does not fit the file is planned again with the reason, and is never sent', async () => {
		const problems: Array<string | undefined> = [];
		let n = 0;
		const plan: PlanEdits = async (i) => { problems.push(i.problem); return n++ === 0 ? { ok: true, edits: [{ old_text: '= Wrong', new_text: 'x' }], summary: 's' } : greenTitle(i); };
		const f = fake();
		assert.ok((await editProjectFile(ARGS, f.deps, plan)).ok);
		assert.deepEqual(problems, [undefined, 'edit 1: old_text does not occur in the file']);
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 1);
	});

	it('a file changed since the read is read again and planned on the new text', async () => {
		const f = fake((_a, n) => n === 0 ? { isError: true, structuredContent: { code: 'stale_base', message: 'read again' } } : ok({ status: 'completed', result: {} }), [{ text: FILE, hash: 'h1' }, { text: FILE, hash: 'h2' }]);
		assert.ok((await editProjectFile(ARGS, f.deps, greenTitle)).ok);
		const hashes = f.calls.filter(c => c.name === 'room.action.execute').map(c => ((c.args.arguments as { files: Array<{ base_hash: string }> }).files[0].base_hash));
		assert.deepEqual(hashes, ['h1', 'h2']);
	});

	it('an unknown outcome is said as such, never retried', async () => {
		const f = fake(() => ({ isError: true, structuredContent: { code: 'ACTION_OUTCOME_UNKNOWN' } }));
		const r = await editProjectFile(ARGS, f.deps, greenTitle) as { error: string; next?: string };
		assert.equal(r.error, 'outcome_unknown');
		assert.equal(r.next, undefined);
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 1);
	});

	it('a refusal, an editor that cannot do it, or a bad kind changes nothing and says so', async () => {
		const refused = fake(() => ({ isError: true, structuredContent: { code: 'INVALID_INPUT', message: 'the page is in folder ab12cd34' } }));
		const r1 = await editProjectFile(ARGS, refused.deps, greenTitle) as { error: string; detail?: string; next?: string };
		assert.equal(r1.error, 'INVALID_INPUT');
		assert.match(r1.detail ?? '', /folder ab12cd34/);
		assert.equal(r1.next, EDIT_NOT_DONE_NEXT);
		const f = fake();
		const r2 = await editProjectFile(ARGS, f.deps, async () => ({ ok: false, error: 'there is no title' })) as { error: string; detail?: string };
		assert.deepEqual([r2.error, r2.detail], ['cannot_edit', 'there is no title']);
		assert.deepEqual(await editProjectFile({ ...ARGS, kind: 'markdown' }, f.deps, greenTitle), { ok: false, error: 'no_file' });
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
	});

	it('an edit that pulls in a file nobody named is not written: the build would not find it', async () => {
		const f = fake();
		const orange: PlanEdits = async () => ({ ok: true, edits: [{ old_text: 'Body text.', new_text: 'Body text.\n#figure(image("orange.png"), caption: [An orange.])' }], summary: 's' });
		const r = await editProjectFile({ ...ARGS, instruction: 'add a figure of an orange' }, f.deps, orange) as { error: string; detail?: string };
		assert.equal(r.error, 'missing_file');
		assert.match(r.detail ?? '', /orange\.png/);
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
		const named = fake();
		assert.ok((await editProjectFile({ ...ARGS, instruction: 'add a figure using orange.png' }, named.deps, orange)).ok, 'the user named it');
	});

	it('a folder project is passed through when given', async () => {
		const f = fake();
		await editProjectFile({ ...ARGS, project: 'ab12cd34' }, f.deps, greenTitle);
		assert.equal((f.calls.find(c => c.name === 'room.action.execute')!.args.arguments as { project: string }).project, 'ab12cd34');
	});
});

describe('a change that breaks the build is undone', () => {
	// A folder project: main.typ in folder a499a739 with one image; the file's text changes with each write.
	function project(compile: (text: string) => string[]) {
		let text = FILE, hash = 0;
		const calls: Array<{ action: string; args: Record<string, unknown> }> = [];
		const client = {
			async callTool(_n: string, a: Record<string, unknown>) {
				const action = String(a.action); const args = (a.arguments ?? {}) as Record<string, unknown>;
				calls.push({ action, args });
				switch (action) {
					case 'room.commons.pages.list': return ok({ pages: [{ kind: 'markdown-qop83g48', title: 'main.typ', folder_id: 'a499a739' }] }) as never;
					case 'room.commons.folders.list': return ok({ folders: [{ id: 'a499a739', main: 'qop83g48' }], placements: [{ surface: 'media', item_id: 'img00001', folder_id: 'a499a739' }] }) as never;
					case 'room.media.list': return ok({ files: [{ id: 'img00001', name: 'apple.jpg' }] }) as never;
					case 'room.commons.pages.read': return ok({ title: 'main.typ', text, next_offset: null, content_hash: `h${hash}` }) as never;
					case 'room.commons.changes.apply': {
						const f = (args.files as Array<{ text?: string; edits?: Array<{ old_text: string; new_text: string }> }>)[0];
						if (typeof f.text === 'string') text = f.text; else for (const e of f.edits ?? []) text = text.replace(e.old_text, e.new_text);
						hash++;
						return ok({ status: 'completed', result: {} }) as never;
					}
					case 'room.commons.typst.compile': {
						const errs = compile(text);
						return ok({ status: 'completed', result: { build_id: 'b', status: errs.length ? 'errors' : 'ok', problems: errs.map(m => ({ severity: 'error', message: m })) } }) as never;
					}
				}
				return ok({}) as never;
			},
			close() {},
		};
		const deps = { client: () => client, currentRoom: () => ({ id: '!r:ag2.space', name: 'susan’s agents' }), index: { rooms: () => [], refresh: async () => [], fetchedAt: () => 0 }, log: () => {} };
		return { calls, deps, text: () => text };
	}
	const compiles = (f: { calls: Array<{ action: string }> }) => f.calls.filter(c => c.action === 'room.commons.typst.compile').length;

	it('a change that builds is kept, after one compile with the folder\'s main file and images', async () => {
		const f = project(() => []);
		const r = await editProjectFile(ARGS, f.deps, greenTitle);
		assert.ok(r.ok && r.build === 'ok', JSON.stringify(r));
		assert.equal(compiles(f), 1);
		assert.deepEqual(f.calls.find(c => c.action === 'room.commons.typst.compile')!.args, { project: 'a499a739', main: 'qop83g48', trigger: 'manual', media: [{ id: 'img00001', path: 'apple.jpg' }] });
		assert.equal((f.calls.find(c => c.action === 'room.commons.changes.apply')!.args as { project: string }).project, 'a499a739');
		assert.match(f.text(), /fill: green/);
	});

	it('a change that adds an error is undone: the file is as it was, and the error is said', async () => {
		const f = project((t) => t.includes('fill: green') ? ['unknown variable: green'] : []);
		const r = await editProjectFile(ARGS, f.deps, greenTitle) as { error: string; detail?: string; next?: string };
		assert.deepEqual([r.error, r.detail, r.next], ['breaks_build', 'unknown variable: green', EDIT_REVERTED_NEXT]);
		assert.equal(f.text(), FILE);
	});

	it('errors the file already had do not undo a change that did not cause them', async () => {
		const f = project(() => ['file not found (searched at orange.png)']);
		const r = await editProjectFile(ARGS, f.deps, greenTitle);
		assert.ok(r.ok && r.build === 'errors' && r.problem === 'file not found (searched at orange.png)', JSON.stringify(r));
		assert.match(f.text(), /fill: green/);
	});
});

describe('the edits and the editor\'s answer', () => {
	it('each old_text must occur exactly once in what the edits before it left', () => {
		assert.deepEqual(applyEdits('a b a', [{ old_text: 'b', new_text: 'c' }]), { ok: true, text: 'a c a' });
		assert.equal(applyEdits('a b a', [{ old_text: 'a', new_text: 'x' }]).ok, false);
		assert.deepEqual(applyEdits('ab', [{ old_text: 'a', new_text: 'z' }, { old_text: 'zb', new_text: 'y' }]), { ok: true, text: 'y' });
	});

	it('adding a figure may not drop or rewrite an existing one; removing or replacing may', () => {
		const fig3 = '#figure(circle(radius: 20pt, fill: red), caption: [A mushroom cap.]) <fig3>';
		const puppy = '#figure(circle(radius: 15pt, fill: gray), caption: [A puppy.]) <fig4>';
		assert.match(droppedFigure([{ old_text: fig3, new_text: puppy }], '再画一个 figure，是一只小狗') ?? '', /rewrites an existing figure/);
		assert.match(droppedFigure([{ old_text: `\n\n${fig3}`, new_text: '' }], 'add a figure of a tree') ?? '', /removes an existing figure/);
		assert.equal(droppedFigure([{ old_text: fig3, new_text: `${fig3}\n\n${puppy}` }], 'draw another figure, a puppy'), null);
		assert.equal(droppedFigure([{ old_text: `\n\n${fig3}`, new_text: '' }], 'delete figure 3'), null);
		assert.equal(droppedFigure([{ old_text: fig3, new_text: puppy }], '把 figure 3 换成一只小狗'), null);
		assert.equal(droppedFigure([{ old_text: fig3, new_text: fig3.replace('red', 'blue') }], 'make figure 3 blue'), null);
	});

	it('a plan that drops a figure is planned again with the reason', async () => {
		const problems: Array<string | undefined> = [];
		let n = 0;
		const plan: PlanEdits = async (i) => {
			problems.push(i.problem);
			return n++ === 0
				? { ok: true, edits: [{ old_text: '#figure(image("orange.png"), caption: [An orange.]) <fig3>', new_text: '#figure(circle(radius: 5pt), caption: [A dot.]) <fig4>' }], summary: 's' }
				: { ok: true, edits: [{ old_text: '#figure(image("orange.png"), caption: [An orange.]) <fig3>', new_text: '#figure(image("orange.png"), caption: [An orange.]) <fig3>\n#figure(circle(radius: 5pt), caption: [A dot.]) <fig4>' }], summary: 's' };
		};
		const f = fake(undefined, [{ text: '= Results\n#figure(image("orange.png"), caption: [An orange.]) <fig3>\n', hash: 'h1' }]);
		const r = await editProjectFile({ ...ARGS, instruction: 'draw another figure, a dot' }, f.deps, plan);
		assert.ok(r.ok, JSON.stringify(r));
		assert.match(problems[1] ?? '', /rewrites an existing figure/);
		assert.match(f.calls.filter(c => c.name === 'room.action.execute').map(c => JSON.stringify(c.args)).join(''), /orange\.png[\s\S]*A dot/);
	});

	it('the answer is parsed strictly', () => {
		assert.deepEqual(parseEditPlan('{"edits":[{"old_text":"a","new_text":"b"}],"summary":"s"}'), { ok: true, edits: [{ old_text: 'a', new_text: 'b' }], summary: 's' });
		assert.deepEqual(parseEditPlan('{"error":"no title"}'), { ok: false, error: 'no title' });
		assert.equal(parseEditPlan('not json').ok, false);
		assert.match((parseEditPlan('{"edits":[]}') as { error: string }).error, /this tool only changes the file. To answer a question about it, read it with read_page/);
		assert.equal(parseEditPlan('{"edits":[{"old_text":"a"}]}').ok, false);
	});

	it('a request to draw is told to draw, never to ask for a picture file', () => {
		for (const i of ['draw another figure, a puppy', '画一只小狗', 'sketch a tree in figure 4']) assert.match(editPlanPrompt({ title: 't.typ', text: FILE, instruction: i }), /The user asked you to DRAW it/, i);
		assert.doesNotMatch(editPlanPrompt({ title: 't.typ', text: FILE, instruction: 'add a figure of a puppy' }), /asked you to DRAW/);
	});

	it('the prompt carries the instruction, the file and the last problem', () => {
		const p = editPlanPrompt({ title: 'typst.typ', text: FILE, instruction: 'make the title green', problem: 'edit 1: old_text does not occur in the file' });
		assert.match(p, /Instruction: make the title green/);
		assert.match(p, /<<<FILE\n#set text/);
		assert.match(p, /could not be applied: edit 1/);
		assert.match(p, /Never reference a file the project may not have/);
		assert.match(p, /When the instruction asks you to draw something .* draw it in the file with the language's own shapes/);
		assert.match(p, /\{"error":"<which file is missing/);
	});

	it('file references are found in Typst and LaTeX', () => {
		assert.deepEqual([...fileRefs('#image("a.png") #include "ch1.typ" #import "t.typ": x #bibliography("refs.bib")')], ['a.png', 'refs.bib', 'ch1.typ', 't.typ']);
		assert.deepEqual([...fileRefs('\\includegraphics[width=3cm]{fig/b.pdf} \\input{intro} \\bibliography{a,b}')], ['fig/b.pdf', 'intro', 'a', 'b']);
		assert.deepEqual(newFileRefs('image("apple.png")', 'image("apple.png") image("pear.png")', 'add a pear'), ['pear.png']);
		assert.deepEqual(newFileRefs('image("apple.png")', 'image("apple.png", width: 40%)', 'shrink it'), []);
	});

	it('the log shows what each edit changed, long text cut', () => {
		assert.equal(editsForLog([{ old_text: 'a', new_text: 'b' }]), '"a" → "b"');
		assert.match(editsForLog([{ old_text: 'x'.repeat(300), new_text: 'y' }]), /^"x{120}…\(300\)" → "y"$/);
	});

	it('the logged line names the edits it applied', async () => {
		const logs: string[] = [];
		const f = fake();
		await editProjectFile(ARGS, { ...f.deps, log: (m: string) => { logs.push(m); } }, greenTitle);
		assert.ok(logs.some(l => l.includes('"= My Title" → "#text(fill: green)[= My Title]"')), logs.join('\n'));
	});
});

describe('the tool as the voice model sees it', () => {
	it('takes a kind and an instruction, never the file text, and runs inline', () => {
		const f = fake();
		const t = createEditFileTool(f.deps, greenTitle);
		assert.equal(t.name, 'edit_project_file');
		assert.equal(t.execution, 'inline');
		assert.match(t.description, /Never pass the file's text/);
		assert.match(t.description, /do not read the change back or ask for a yes/);
		assert.match(EDIT_FILE_PROMPT_RULE, /search_commons for the file, then edit_project_file/);
		assert.match(EDIT_FILE_PROMPT_RULE, /never write its text out yourself/);
		assert.match(EDIT_FILE_PROMPT_RULE, /submit work once with the whole request \(make it, put it in that room\\?'s project, change the file\), never only "generate an image"/);
		assert.match(EDIT_FILE_PROMPT_RULE, /a question about what a project file holds .* → search_commons, then read_page, and answer from it; edit_project_file only changes files/);
		assert.match(EDIT_FILE_PROMPT_RULE, /including a figure the user asks you to draw/);
		assert.match(t.description, /"missing_file" — .* submit work with the whole request in one task/);
	});
});
}
