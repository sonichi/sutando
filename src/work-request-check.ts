/**
 * The relay agent's text-model decisions. Two of them:
 * - a check on a `work` request before it is submitted: can the core act on it as written, is something
 *   missing that only the user can supply (which PR, which file, who), or was it already sent?
 * - a look at every finished user turn, whichever tool the voice model chose: is it a request for the core?
 * - a look at every spoken assistant turn while a result waits to be told: did the turn tell it?
 *
 * The voice model decides fluently and does not stop to ask (2026-10-12: four prompt revisions,
 * zero questions asked; a blank request was submitted or an answer was invented). A text model
 * asked one question follows instructions, so the decision lives here, outside the voice model.
 * Any failure or delay lets the request through: the check never blocks work.
 */

/** How long the core will take: a quick task is one lookup, command or message; a long one builds, writes, researches or generates a picture. */
export type WorkLane = 'quick' | 'long';
export type WorkCheck = { action: 'submit'; lane?: WorkLane } | { action: 'ask'; question: string } | { action: 'duplicate'; of: string };

/** A task the user asked for that has not been answered yet: the check compares a new request with these. */
export interface OpenTask {
	id: string;
	text: string;
}

export interface WorkCheckInput {
	/** The request the voice model wrote for the core. */
	task: string;
	/** The user's and assistant's recent words, oldest first. */
	recentConversation: string;
	/** Tasks already sent and not yet answered: the same request again is not sent twice. */
	openTasks?: OpenTask[];
	/** Tasks answered in the last minute: the same request again is a duplicate of one; what to do with it is the relay's call. */
	answeredTasks?: OpenTask[];
}

export type WorkRequestCheck = (input: WorkCheckInput, signal?: AbortSignal) => Promise<WorkCheck>;

/** The default model of the relay checks; voice-agent.json `relayCheck.model` overrides it. */
export const WORK_CHECK_MODEL = 'gemini-3-flash-preview';
export const WORK_CHECK_TIMEOUT_MS = 4_000;

export function workCheckPrompt(i: WorkCheckInput): string {
	return [
		'A voice assistant wrote a request for its back-end agent (the core), which does lookups, actions and file work. Before it is sent, decide two things: does the request identify WHICH thing it is about, and is it quick or long?',
		'Rules:',
		'- The request is about a specific thing when it names a type: a PR, an issue, a file, a document, a person, a repository, a meeting, a place.',
		'- It identifies it when it gives the number, name or title, or when the user is continuing the same request in the recent words (e.g. answering the assistant\'s "which PR?").',
		'- "the PR", "that file", "the meeting", "那个 PR" with no number, name or title = not identified. Do NOT fill it in from a PR, file or meeting that came up earlier, not even in the request just before: ask. The only exception is the user answering the assistant\'s own "which one?".',
		'- A bare number is identified when the recent words show what kind of thing the numbers are (e.g. the user has been asking about PRs).',
		'- The same request as one of the open tasks (same thing, same ask, maybe worded differently or misheard) = duplicate.',
		'- lane "quick": one lookup, one command, one count, one message or file sent, a short text; done in well under a minute with no building. lane "long": building or writing something (an app, a document, code), generating or drawing a picture (a minute or more: 2026-10-18 a lizard picture held three lookups behind it), research across sources, or many steps.',
		'Reply with JSON only:',
		'{"action":"duplicate","of":"<id of the open task>"} when it is the same request as an open task.',
		'{"action":"ask","question":"..."} when a specific thing is not identified. One short question in the user\'s language asking only for the missing piece.',
		'{"action":"submit","lane":"quick"|"long"} otherwise, including requests about no specific thing (a general summary, a search, a how-to).',
		'Examples:',
		'Request "Check the status of the PR"; recent words "帮我查一下那个 PR 的状态" -> {"action":"ask","question":"你说的是哪个 PR？"}',
		'Request "Check the status of PR 5302"; recent words "帮我查一下 PR5302 的状态" -> {"action":"submit","lane":"quick"}',
		'Request "Summarize the meeting notes"; recent words "帮我总结一下会议纪要" -> {"action":"ask","question":"哪一场会议的纪要？"}',
		'Request "List the PRs merged this week in sonichi/sutando" -> {"action":"submit","lane":"quick"}',
		'Request "Check 5332"; recent words "帮我查一下 5428 改了什么 … 帮我查一下 5332" -> {"action":"submit","lane":"quick"}',
		'Request "Draw a picture of a squirrel" -> {"action":"submit","lane":"long"}',
		'Request "Create a calorie tracking app that estimates calories from food photos" -> {"action":"submit","lane":"long"}',
		'Request "Write a report comparing the three vendors" -> {"action":"submit","lane":"long"}',
		'Request "Check the status of that PR"; recent words "帮我查一下 PR 5302 的状态 … (answered) … 帮我查一下那个 PR 的状态" -> {"action":"ask","question":"你说的是哪个 PR？"}',
		'Request "Summarize PR 5333"; open task task-9 "帮我查一下 PR 5333 改了什么" -> {"action":"duplicate","of":"task-9"}',
		'',
		'Recent words (oldest first):',
		i.recentConversation.trim() || '(none)',
		'',
		'Open tasks (sent, not answered yet):',
		openTasksBlock(i.openTasks),
		'',
		'Answered a moment ago (the same request again is a duplicate of that id too):',
		openTasksBlock(i.answeredTasks),
		'',
		'Request the assistant wrote:',
		i.task.trim(),
	].join('\n');
}

function openTasksBlock(tasks: OpenTask[] | undefined): string {
	return tasks?.length ? tasks.map((t) => `${t.id}: ${t.text.replace(/\s+/g, ' ').slice(0, 160)}`).join('\n') : '(none)';
}

/** The model's reply as a decision. Anything that is not a clear ask or a known duplicate means submit. */
export function parseWorkCheck(text: string, openTasks: OpenTask[] = []): WorkCheck {
	const m = /\{[\s\S]*\}/.exec(text);
	if (!m) return { action: 'submit' };
	try {
		const d = JSON.parse(m[0]) as { action?: unknown; question?: unknown; of?: unknown };
		if (d.action === 'ask' && typeof d.question === 'string' && d.question.trim()) return { action: 'ask', question: d.question.trim() };
		// Only an id that is really open: a made-up one must not swallow a request.
		if (d.action === 'duplicate' && typeof d.of === 'string' && openTasks.some((t) => t.id === d.of)) return { action: 'duplicate', of: d.of };
		const lane = (d as { lane?: unknown }).lane;
		if (lane === 'quick' || lane === 'long') return { action: 'submit', lane };
	} catch { /* not JSON: submit */ }
	return { action: 'submit' };
}

/**
 * The relay agent's look at every finished user turn, whatever tool the voice model chose (2026-10-12:
 * "帮我查一下 5334 的状态" went to get_task_status, the relay never saw it, and the task was lost). Is it a
 * request for the core, something the voice model answers itself, a question only the user can settle,
 * or a request already sent?
 */
export type TurnVerdict =
	/** `acknowledged`: the assistant's reply to this turn already told the user it is on this request (2026-10-21 18:38:35
	 *  "Understood, I'll make sure to check…"), so the submit is not acknowledged a second time. `corrects`: the open task
	 *  this request corrects or spells out (a misheard name); one the user heard acknowledged is not acknowledged again. */
	| { action: 'work'; task: string; acknowledged?: true; corrects?: string }
	| { action: 'none' }
	| { action: 'ask'; question: string }
	| { action: 'duplicate'; of: string }
	/** The assistant said it already did something this turn that no tool call or delivered result backs (2026-10-21
	 *  17:39:23 "I've replaced the V2 plan…" with no tool call). `task` when only the core can do it. */
	| { action: 'unbacked'; claim: string; task?: string };

export interface TurnInput {
	/** What the user just said (one turn, or several when they went on speaking). */
	utterance: string;
	recentConversation: string;
	openTasks?: OpenTask[];
	/** This turn's tool calls with their status, and the results delivered to the voice model in it (thisTurnToolLines). */
	thisTurn?: string;
}

export type TurnClassifier = (input: TurnInput, signal?: AbortSignal) => Promise<TurnVerdict>;

/** @param extraRules rule lines from the voice surface: what the voice model answers itself (a skill's tools), so the core is not asked too. */
export function turnClassifierPrompt(i: TurnInput, extraRules: readonly string[] = []): string {
	return [
		'You sit behind a voice assistant. It talks with the user; a back-end agent (the core) does the real work: anything about the user\'s own accounts, files, code repositories and PRs, email, calendar, messages, notes, and any action or lookup that needs them. The voice assistant itself can only talk, search the public web, and answer from general knowledge or from this conversation.',
		'The user just finished saying something. Decide whether it asks the core to do something.',
		'Rules:',
		'- work: the user asks for something only the core can do (look up a PR, check a status in their systems, send, write, schedule, remind, open, change something). Write the request for the core, complete and self-contained, in the user\'s language. Name what it is about only from what the user said for this request (this turn, or the same request continued, e.g. their answer to the assistant\'s "which PR?").',
		'- "那个 PR", "that file", "the meeting" with no number or name in what the user just said is never filled in, not even from the request just before it: that is a guess. If the assistant is already asking which one, decide none; otherwise decide ask. The only exception is the user answering the assistant\'s own "which one?" question.',
		'- The assistant\'s last line in the recent words is its reply to this turn. If it asks the user something about this request (which one, do you mean X, shall I), decide none: the user\'s answer decides.',
		'- A fragment that is not a sentence (one letter, a sound, a lone word like "A", "in", "嗯") is none.',
		'- "the status of 5334" / "查一下 5334 的状态" when the recent words are about PRs is a request to look up PR 5334: work. It is not a question about the assistant\'s own task list.',
		'- A yes to the assistant\'s own offer to do something ("你是想让我查一下 5332 吗？" -> "对") is that request: work.',
		'- A question whether an earlier request was done, lost or already answered ("did you lose that one?", "是不是丢了一个", "did you tell me the PR count?") is none: the assistant answers it from its task list, and the core is not asked again.',
		'- none: chit-chat, thanks, any other yes/no reply, a question about the assistant\'s own task list ("how many tasks are pending", "is it done yet") with no new thing named, a public fact or general-knowledge question, or anything already being answered.',
		// 2026-10-21 17:39:17: "Did you see it 923?" (a video at 9:23 in the room), already answered, was sent to the core.
		'- A question about a message, video or picture at some time or place ("did you see the 9:23 one?", "did you watch it?"), or about something the assistant has just answered, is none: the assistant looks or answers itself.',
		// 2026-10-21 18:38:35: "Understood, I'll make sure to check for pull requests by Sonicchi.", then the submit's notice
		// made it say "I'm looking into those pull requests now." as well.
		'- With work, add "acknowledged":true when the assistant\'s reply to this turn (its lines after the user\'s last line in the recent words) already tells the user it is on this request ("let me look into", "I\'ll check that", "understood, I\'ll look for…"), even cut off. Leave it out when that reply says nothing about doing it.',
		// 2026-10-21 18:56:16: "c h i s o n a c h i" spelled out the request sent a turn before; the reply was "Mm-hm.".
		'- With work, add "corrects":"<id>" when the user is correcting or spelling out one of the open tasks below (a misheard name, a missing piece) rather than asking for something new.',
		'- duplicate: the same request as one of the open tasks below (same thing, same ask, worded differently or misheard).',
		'- ask: it is a request for the core but a specific thing is missing and the recent words do not supply it, or you cannot tell whether they want the core to act. One short question in the user\'s language.',
		// 2026-10-21 17:39:23, 17:40:00: "I've replaced the V2 plan…", "I've created a separate page … v3", no tool call in either turn.
		'- unbacked: the assistant\'s reply to this turn (its lines after the user\'s last line in the recent words) says it already did something — did, created, wrote, updated, replaced, sent, deleted, opened — that no tool call this turn with "ok" did and no delivered result reports. claim = that sentence. Saying it is doing it or will do it, or that a task is with the core, is not a claim; nor is repeating what a delivered or earlier result said. Add task (the request for the core, as for work) only when only the core can do it and no open task is it already. When the assistant claimed it did what the user asked, decide unbacked, not work.',
		// The skills' rules name what the assistant's own tools do: they win over "messages, notes" above (17:39:17).
		...(extraRules.length ? ['The rules below come from the assistant\'s own tools and take precedence over the split above: what they say the assistant does itself is none, even when it is about messages or notes.', ...extraRules] : []),
		'Reply with JSON only, one of:',
		'{"action":"work","task":"...","acknowledged":true} (acknowledged only when the assistant already said it is on it; "corrects" as above)',
		'{"action":"none"}',
		'{"action":"duplicate","of":"<id of the open task>"}',
		'{"action":"ask","question":"..."}',
		'{"action":"unbacked","claim":"...","task":"..."} (task only when the core must do it)',
		'',
		'Recent words (oldest first):',
		i.recentConversation.trim() || '(none)',
		'',
		'Open tasks (sent, not answered yet):',
		openTasksBlock(i.openTasks),
		'',
		'This turn, after the user spoke — the assistant\'s tool calls (name, arguments, result) and the results delivered to it:',
		i.thisTurn?.trim() || '(none)',
		'',
		'What the user just said:',
		i.utterance.trim(),
	].join('\n');
}

/** Anything unclear is `none`: the voice model's own `work` call still covers the turn. */
export function parseTurnVerdict(text: string, openTasks: OpenTask[] = []): TurnVerdict {
	const m = /\{[\s\S]*\}/.exec(text);
	if (!m) return { action: 'none' };
	try {
		const d = JSON.parse(m[0]) as { action?: unknown; task?: unknown; question?: unknown; of?: unknown; claim?: unknown; acknowledged?: unknown; corrects?: unknown };
		if (d.action === 'unbacked' && typeof d.claim === 'string' && d.claim.trim()) {
			const task = typeof d.task === 'string' ? d.task.trim() : '';
			return task ? { action: 'unbacked', claim: d.claim.trim(), task } : { action: 'unbacked', claim: d.claim.trim() };
		}
		if (d.action === 'work' && typeof d.task === 'string' && d.task.trim()) {
			// Only an id that is really open, as for duplicate.
			const corrects = typeof d.corrects === 'string' && openTasks.some((t) => t.id === d.corrects) ? d.corrects : undefined;
			return { action: 'work', task: d.task.trim(), ...(d.acknowledged === true ? { acknowledged: true as const } : {}), ...(corrects ? { corrects } : {}) };
		}
		if (d.action === 'ask' && typeof d.question === 'string' && d.question.trim()) return { action: 'ask', question: d.question.trim() };
		if (d.action === 'duplicate' && typeof d.of === 'string' && openTasks.some((t) => t.id === d.of)) return { action: 'duplicate', of: d.of };
	} catch { /* not JSON */ }
	return { action: 'none' };
}

/** A result that came back through its work call and has not been heard yet. */
export interface WaitingResult {
	id: string;
	/** What the user asked. */
	request: string;
	result: string;
}

export interface HeardInput {
	results: WaitingResult[];
	/** The assistant's spoken lines since the results came back, oldest first. */
	assistantWords: string;
	/** The user cut the last turn off. */
	cutOff: boolean;
}

/** The ids of the results the assistant's words told the user; null when there is no verdict (the caller's own rule decides). */
export type HeardCheck = (input: HeardInput, signal?: AbortSignal) => Promise<string[] | null>;

/** A result is given this much: a summary's answer can sit past the first sentences. */
export const HEARD_RESULT_MAX = 600;

/**
 * Did the assistant's words tell the user a waiting result? A phrase pattern decided it and failed on each new wording
 * (2026-10-21 18:56:52: "you merged three yesterday" for "You merged 3 pull requests yesterday", judged unsaid, nudged
 * and said again); looser patterns took holding lines for the answer.
 */
export function heardCheckPrompt(i: HeardInput): string {
	const quote = (s: string) => `"${s.replace(/\s+/g, ' ').replace(/"/g, "'").trim()}"`;
	const clip = (s: string) => { const t = s.replace(/\s+/g, ' ').trim(); return t.length > HEARD_RESULT_MAX ? `${t.slice(0, HEARD_RESULT_MAX - 1)}…` : t; };
	return [
		'A voice assistant is waiting to tell the user the results below; each answers a request the user made. Decide which of them the assistant\'s words already told the user.',
		'Rules:',
		'- A result is told when the words give the user its main answer, in any wording or language: "you merged three yesterday" tells "You merged 3 pull requests yesterday: number 5641, 5648 and 5650". Not every detail needs saying.',
		'- Saying it is checking or working on it, naming the request without its answer, or a different answer does not tell it. Nor does an answer to something else that happens to share a word or number.',
		'- An answer cut off before its main point does not tell it.',
		'Reply with JSON only: {"told":["<id>", ...]}, an empty list when none was told.',
		'',
		'Results waiting to be told (id: asked -> result):',
		...i.results.map((r) => `${r.id}: ${quote(r.request)} -> ${clip(r.result)}`),
		'',
		`The assistant's words since these results came back, oldest first${i.cutOff ? ' (the last turn was cut off by the user)' : ''}:`,
		i.assistantWords.trim() || '(none)',
	].join('\n');
}

/** The ids told, only ones that were asked about; null when the reply is not a verdict. */
export function parseHeardVerdict(text: string, results: WaitingResult[]): string[] | null {
	const m = /\{[\s\S]*\}/.exec(text);
	if (!m) return null;
	try {
		const told = (JSON.parse(m[0]) as { told?: unknown }).told;
		if (!Array.isArray(told)) return null;
		return results.map((r) => r.id).filter((id) => told.includes(id));
	} catch {
		return null;
	}
}

/** Where a check prompt's instructions end and the turn being judged begins. */
const INPUT_MARKS = ['\nRecent words (oldest first):', '\nResults waiting to be told'];

/** The instructions go in as the system instruction and the turn as the user message: sent as one block,
 *  the rules together tripped Gemini's prompt filter (PROHIBITED_CONTENT) on harmless requests. */
export function checkRequestBody(prompt: string): Record<string, unknown> {
	const found = INPUT_MARKS.map((m) => prompt.indexOf(m)).filter((k) => k > 0);
	const at = found.length ? Math.min(...found) : -1;
	const contents = (text: string) => [{ role: 'user', parts: [{ text }] }];
	return at > 0
		? { systemInstruction: { parts: [{ text: prompt.slice(0, at) }] }, contents: contents(prompt.slice(at + 1)) }
		: { contents: contents(prompt) };
}

/** One generateContent call: thinking off, JSON out. Null text on any failure, logged; `blocked` when Gemini's filter refused the prompt. */
async function geminiJson(opts: { apiKey: string; model: string; timeoutMs: number; prompt: string; signal?: AbortSignal; log?: (m: string) => void; what: string }): Promise<{ text: string | null; blocked: boolean }> {
	const ctl = new AbortController();
	const timer = setTimeout(() => ctl.abort(), opts.timeoutMs);
	opts.signal?.addEventListener('abort', () => ctl.abort(), { once: true });
	try {
		// The key in a header, never the URL, so it cannot end up in a logged URL.
		const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${opts.model}:generateContent`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', 'x-goog-api-key': opts.apiKey },
			body: JSON.stringify({
				...checkRequestBody(opts.prompt),
				// A thinking model spends the output budget on thinking and returns a cut-off "{"action":" (seen 2026-10-12):
				// thinking off, JSON out.
				generationConfig: { maxOutputTokens: 300, temperature: 0, responseMimeType: 'application/json', thinkingConfig: { thinkingBudget: 0 } },
			}),
			signal: ctl.signal,
		});
		const data = await res.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> }; finishReason?: string }>; error?: { message?: string }; promptFeedback?: { blockReason?: string } };
		const text = data.candidates?.[0]?.content?.parts?.map((p) => p.text ?? '').join('') ?? '';
		if (!text) {
			const blocked = data.promptFeedback?.blockReason;
			const why = blocked ? `blocked by Gemini's prompt filter: ${blocked}` : data.error?.message ?? `empty, finish ${data.candidates?.[0]?.finishReason ?? 'none'}`;
			opts.log?.(`[RelayAgent] ${opts.what} gave no answer (${why})`);
			return { text: null, blocked: !!blocked };
		}
		return { text, blocked: false };
	} catch (err) {
		opts.log?.(`[RelayAgent] ${opts.what} failed (${err instanceof Error ? err.message : String(err)})`);
		return { text: null, blocked: false };
	} finally {
		clearTimeout(timer);
	}
}

export function createGeminiWorkCheck(opts: { apiKey: () => string | undefined; model?: string; timeoutMs?: number; log?: (m: string) => void }): WorkRequestCheck {
	const model = opts.model ?? WORK_CHECK_MODEL;
	return async (input, signal) => {
		const apiKey = opts.apiKey();
		if (!apiKey) return { action: 'submit' };
		const t0 = Date.now();
		const { text } = await geminiJson({ apiKey, model, timeoutMs: opts.timeoutMs ?? WORK_CHECK_TIMEOUT_MS, prompt: workCheckPrompt(input), signal, log: opts.log, what: 'work check' });
		if (text === null) return { action: 'submit' };
		const verdict = parseWorkCheck(text, [...(input.openTasks ?? []), ...(input.answeredTasks ?? [])]);
		opts.log?.(`[RelayAgent] work check: ${verdict.action}${verdict.action === 'ask' ? ` (${verdict.question})` : verdict.action === 'duplicate' ? ` of ${verdict.of}` : ''} in ${Date.now() - t0}ms`);
		return verdict;
	};
}

export function createGeminiTurnClassifier(opts: { apiKey: () => string | undefined; model?: string; timeoutMs?: number; log?: (m: string) => void; extraRules?: readonly string[] }): TurnClassifier {
	const model = opts.model ?? WORK_CHECK_MODEL;
	return async (input, signal) => {
		const apiKey = opts.apiKey();
		if (!apiKey) return { action: 'none' };
		const t0 = Date.now();
		const ask = (rules?: readonly string[]) => geminiJson({ apiKey, model, timeoutMs: opts.timeoutMs ?? WORK_CHECK_TIMEOUT_MS, prompt: turnClassifierPrompt(input, rules), signal, log: opts.log, what: 'turn check' });
		let r = await ask(opts.extraRules);
		// Still filtered: once more without the surface's rules, so a real request is still caught.
		if (r.text === null && r.blocked && opts.extraRules?.length) r = await ask();
		if (r.text === null) return { action: 'none' };
		const verdict = parseTurnVerdict(r.text, input.openTasks);
		const detail = verdict.action === 'work' ? ` (${verdict.task})${verdict.acknowledged ? ' [already acknowledged]' : ''}${verdict.corrects ? ` [corrects ${verdict.corrects}]` : ''}` : verdict.action === 'unbacked' ? ` ("${verdict.claim}"${verdict.task ? `; task=${verdict.task}` : ''})` : verdict.action === 'ask' ? ` (${verdict.question})` : verdict.action === 'duplicate' ? ` of ${verdict.of}` : '';
		opts.log?.(`[RelayAgent] turn check: ${verdict.action}${detail} in ${Date.now() - t0}ms`);
		return verdict;
	};
}

/** One call, no retry: a verdict later than the relay waits for is not used (it falls back to its own rule). */
export function createGeminiHeardCheck(opts: { apiKey: () => string | undefined; model?: string; timeoutMs?: number; log?: (m: string) => void }): HeardCheck {
	const model = opts.model ?? WORK_CHECK_MODEL;
	return async (input, signal) => {
		const apiKey = opts.apiKey();
		if (!apiKey) return null;
		const t0 = Date.now();
		const { text } = await geminiJson({ apiKey, model, timeoutMs: opts.timeoutMs ?? WORK_CHECK_TIMEOUT_MS, prompt: heardCheckPrompt(input), signal, log: opts.log, what: 'heard check' });
		if (text === null) return null;
		const told = parseHeardVerdict(text, input.results);
		opts.log?.(`[RelayAgent] heard check: ${told === null ? 'no verdict' : `told [${told.join(', ')}] of [${input.results.map((r) => r.id).join(', ')}]`} in ${Date.now() - t0}ms`);
		return told;
	};
}
