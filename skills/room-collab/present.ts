// The talk driver: turns a room talk script into beats and paces them on the
// voice session's turn ends, so actions never depend on the model remembering them.

export type ScriptItem =
	| { say: string }
	| { cue: 'slide'; move: 'next' | 'prev' | number }
	| { cue: 'highlight'; topic: string }
	| { cue: 'pause'; seconds: number };

/**
 * Where the deck must be for a beat, in absolute terms, so the talk can always put
 * it back: a slide number, or the topic whose highlight the deck jumps to.
 */
export type Anchor = { slide: number } | { topic: string; slide?: number };

/** One beat: the actions to take, then the line to say (empty on a trailing-cues beat). */
export type Beat = {
	step: number;
	cues: Exclude<ScriptItem, { say: string }>[];
	say: string;
	anchor: Anchor | null;
};

/**
 * Beats in order: each line carries the cues that stand before it in its step, and
 * the position the deck is in once they have run. Relative moves are resolved here,
 * against the topic→slide map from the page outline, so a replay can never drift.
 */
export function toBeats(steps: ScriptItem[][], topicSlide: Record<string, number> = {}): Beat[] {
	const beats: Beat[] = [];
	let anchor: Anchor | null = null;
	const slideOf = (a: Anchor | null) => (a === null ? 1 : a.slide);
	const apply = (cue: Beat['cues'][number]) => {
		if (cue.cue === 'slide') {
			const at = slideOf(anchor);
			if (typeof cue.move === 'number') anchor = { slide: cue.move };
			else if (at !== undefined) anchor = { slide: Math.max(1, at + (cue.move === 'next' ? 1 : -1)) };
		} else if (cue.cue === 'highlight' && cue.topic !== 'clear') {
			anchor = { topic: cue.topic, slide: topicSlide[cue.topic] };
		}
	};
	steps.forEach((items, step) => {
		let cues: Beat['cues'] = [];
		items.forEach((it) => {
			if ('say' in it) {
				beats.push({ step, cues, say: it.say, anchor });
				cues = [];
			} else {
				cues.push(it);
				apply(it);
			}
		});
		if (cues.length) beats.push({ step, cues, say: '', anchor });
	});
	return beats;
}

/** The relay paths that put the deck where a beat belongs, whatever happened since. */
export function anchorPaths(anchor: Anchor | null): string[] {
	if (!anchor) return ['/slide/1'];
	// The slide first, for decks that do not jump to a topic themselves; the topic after.
	if ('topic' in anchor)
		return [
			...(anchor.slide ? [`/slide/${anchor.slide}`] : []),
			`/highlight/${encodeURIComponent(anchor.topic)}`,
		];
	return [`/slide/${anchor.slide}`];
}

/** The relay path for one cue; pauses are waited on by the driver, not sent. */
export function cuePath(cue: Beat['cues'][number]): string | null {
	if (cue.cue === 'slide') return `/slide/${cue.move === 'next' || cue.move === 'prev' ? cue.move : String(cue.move)}`;
	if (cue.cue === 'highlight') return `/highlight/${encodeURIComponent(cue.topic)}`;
	return null;
}

/** What the model is told for one beat: speak exactly this, nothing about the control. */
export const lineInstruction = (say: string, n: number, total: number, showing?: string) =>
	`[SILENT CONTROL — never spoken aloud; do not read, repeat or mention it. You are presenting (line ${n} of ${total}); ` +
	`the deck is already set${showing ? ` and now shows ${showing.replace(/"/g, "'")}` : ''}. ` +
	`Say ONLY this line, naturally, then stop and wait: "${say.replace(/"/g, "'")}"]`;

/** How a beat's position reads to the model: "slide 18 — What agents need…". */
export function describeAnchor(anchor: Anchor | null, titles: Record<number, string>): string | undefined {
	const n = anchor === null ? 1 : anchor.slide;
	if (n === undefined) return undefined;
	return titles[n] ? `slide ${n} — ${titles[n].replace(/[.。]+$/, "")}` : `slide ${n}`;
}
