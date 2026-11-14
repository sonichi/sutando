/**
 * The voice session's compressed conversation summary. bodhi keeps a summary slot
 * (ConversationContext.setSummary) but never fills it; this fills it with Gemini Flash once the
 * conversation grows past a token bound, keeping the most recent turns verbatim. Work tasks carry
 * the summary and the recent turns, and bodhi replays them on a reconnect.
 */
import type { ConversationItem } from 'bodhi-realtime-agent';
import { resolveCredential } from './credential-resolver.js';

/** The part of bodhi's ConversationContext the summarizer uses. */
export interface SummarizedContext {
	readonly items: readonly ConversationItem[];
	readonly summary: string | null;
	readonly tokenEstimate: number;
	setSummary(summary: string): void;
}

/** Turns the earlier conversation (and the summary so far) into a new summary. */
export type Summarize = (previous: string | null, items: readonly ConversationItem[]) => Promise<string>;

export interface SummarizerOptions {
	context: () => SummarizedContext | null;
	summarize: Summarize;
	/** Summarize once the conversation's token estimate passes this. */
	thresholdTokens?: number;
	/** Items kept verbatim after a summary. */
	keepRecent?: number;
	/** bodhi dropped this many items from the front (they are in the summary now). */
	onEvicted?: (count: number) => void;
	log?: (msg: string) => void;
}

export const SUMMARY_THRESHOLD_TOKENS = 6000;
export const SUMMARY_KEEP_RECENT = 10;

/** A spoken line as the summary and the work task show it. */
export function renderItems(items: ReadonlyArray<{ role: string; content?: string | null }>): string {
	return items
		.filter((i) => (i.role === 'user' || i.role === 'assistant') && i.content)
		.map((i) => `${i.role}: ${(i.content ?? '').replace(/\s+/g, ' ').trim()}`)
		.join('\n');
}

export function createConversationSummarizer(opts: SummarizerOptions) {
	const threshold = opts.thresholdTokens ?? SUMMARY_THRESHOLD_TOKENS;
	const keep = opts.keepRecent ?? SUMMARY_KEEP_RECENT;
	let running = false;
	let generation = 0;

	return {
		/** After a turn: summarize the older part when the conversation has grown past the bound. */
		async onTurnEnd(): Promise<void> {
			const ctx = opts.context();
			if (!ctx || running || ctx.tokenEstimate < threshold) return;
			const snapshot = ctx.items.slice();
			const k = snapshot.length - keep;
			if (k <= 0) return;
			running = true;
			const gen = generation;
			try {
				const text = (await opts.summarize(ctx.summary, snapshot.slice(0, k))).trim();
				// A clear while the model was summarizing: the summary belongs to a conversation that is gone.
				if (!text || gen !== generation || ctx.items[0] !== snapshot[0]) return;
				// setSummary drops every item bodhi has already persisted, which can be more than were summarized.
				const before = ctx.items.slice();
				ctx.setSummary(text);
				const evicted = before.length - ctx.items.length;
				const missed = renderItems(before.slice(k, evicted));
				if (missed) ctx.setSummary(`${text}\n\nThen, verbatim:\n${missed}`);
				if (evicted > 0) opts.onEvicted?.(evicted);
				opts.log?.(`[VoiceSummary] summarized ${k} items; ${evicted} dropped from the live context`);
			} catch (e) {
				opts.log?.(`[VoiceSummary] summary failed: ${(e as Error).message}`);
			} finally {
				running = false;
			}
		},
		/** The conversation was reset (a goodbye): its summary goes with it. Call right after the reset. */
		clear(): void {
			generation++;
			const ctx = opts.context();
			if (ctx?.summary) ctx.setSummary('');
		},
	};
}

/** Model for the summary; override via .env. */
const SUMMARY_MODEL = process.env.VOICE_SUMMARY_MODEL || 'gemini-3-flash-preview';

/** Gemini Flash over REST: a short factual summary of the earlier conversation. */
export const geminiSummarize: Summarize = async (previous, items) => {
	const apiKey = resolveCredential('gemini-voice').key;
	if (!apiKey) throw new Error('no Gemini key');
	const prompt =
		'Summarize this earlier part of a voice conversation between a user and their assistant, for the assistant to keep as context. ' +
		'Keep every request the user made and its outcome, decisions, names, numbers, dates and open questions. ' +
		'Plain text, at most 200 words, no preamble.' +
		(previous ? `\n\nSummary of what came before:\n${previous}` : '') +
		`\n\nConversation:\n${renderItems(items)}`;
	const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${SUMMARY_MODEL}:generateContent?key=${apiKey}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			contents: [{ role: 'user', parts: [{ text: prompt }] }],
			// A thinking model spends its token budget reasoning unless thinking is off.
			generationConfig: { maxOutputTokens: 800, thinkingConfig: { thinkingBudget: 0 } },
		}),
		signal: AbortSignal.timeout(30_000),
	});
	if (!res.ok) throw new Error(`HTTP ${res.status}`);
	const data = await res.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
	return data.candidates?.[0]?.content?.parts?.map((p) => p.text ?? '').join('') ?? '';
};
