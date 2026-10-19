/**
 * The relay agent's text-model decisions. Two of them:
 * - a check on a `work` request before it is submitted: can the core act on it as written, is something
 *   missing that only the user can supply (which PR, which file, who), or was it already sent?
 * - a look at every finished user turn, whichever tool the voice model chose: is it a request for the core?
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
	| { action: 'work'; task: string }
	| { action: 'none' }
	| { action: 'ask'; question: string }
	| { action: 'duplicate'; of: string };

export interface TurnInput {
	/** What the user just said (one turn, or several when they went on speaking). */
	utterance: string;
	recentConversation: string;
	openTasks?: OpenTask[];
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
		'- duplicate: the same request as one of the open tasks below (same thing, same ask, worded differently or misheard).',
		'- ask: it is a request for the core but a specific thing is missing and the recent words do not supply it, or you cannot tell whether they want the core to act. One short question in the user\'s language.',
		...extraRules,
		'Reply with JSON only, one of:',
		'{"action":"work","task":"..."}',
		'{"action":"none"}',
		'{"action":"duplicate","of":"<id of the open task>"}',
		'{"action":"ask","question":"..."}',
		'',
		'Recent words (oldest first):',
		i.recentConversation.trim() || '(none)',
		'',
		'Open tasks (sent, not answered yet):',
		openTasksBlock(i.openTasks),
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
		const d = JSON.parse(m[0]) as { action?: unknown; task?: unknown; question?: unknown; of?: unknown };
		if (d.action === 'work' && typeof d.task === 'string' && d.task.trim()) return { action: 'work', task: d.task.trim() };
		if (d.action === 'ask' && typeof d.question === 'string' && d.question.trim()) return { action: 'ask', question: d.question.trim() };
		if (d.action === 'duplicate' && typeof d.of === 'string' && openTasks.some((t) => t.id === d.of)) return { action: 'duplicate', of: d.of };
	} catch { /* not JSON */ }
	return { action: 'none' };
}

/** Where a check prompt's instructions end and the turn being judged begins. */
const INPUT_MARK = '\nRecent words (oldest first):';

/** The instructions go in as the system instruction and the turn as the user message: sent as one block,
 *  the rules together tripped Gemini's prompt filter (PROHIBITED_CONTENT) on harmless requests. */
export function checkRequestBody(prompt: string): Record<string, unknown> {
	const at = prompt.indexOf(INPUT_MARK);
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
		const detail = verdict.action === 'work' ? ` (${verdict.task})` : verdict.action === 'ask' ? ` (${verdict.question})` : verdict.action === 'duplicate' ? ` of ${verdict.of}` : '';
		opts.log?.(`[RelayAgent] turn check: ${verdict.action}${detail} in ${Date.now() - t0}ms`);
		return verdict;
	};
}
