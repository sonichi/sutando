// The talk driver: turns a room talk script into beats and paces them on the
// voice session's turn ends, so actions never depend on the model remembering them.

export type ScriptItem =
	| { say: string }
	| { cue: 'slide'; move: 'next' | 'prev' | number }
	| { cue: 'highlight'; topic: string }
	| { cue: 'pause'; seconds: number };

/** One beat: the actions to take, then the line to say (empty on a trailing-cues beat). */
export type Beat = { step: number; cues: Exclude<ScriptItem, { say: string }>[]; say: string };

/** Beats in order: each line carries the cues that stand before it in its step. */
export function toBeats(steps: ScriptItem[][]): Beat[] {
	const beats: Beat[] = [];
	steps.forEach((items, step) => {
		let cues: Beat['cues'] = [];
		items.forEach((it) => {
			if ('say' in it) {
				beats.push({ step, cues, say: it.say });
				cues = [];
			} else cues.push(it);
		});
		if (cues.length) beats.push({ step, cues, say: '' });
	});
	return beats;
}

/** The relay path for one cue; pauses are waited on by the driver, not sent. */
export function cuePath(cue: Beat['cues'][number]): string | null {
	if (cue.cue === 'slide') return `/slide/${cue.move === 'next' || cue.move === 'prev' ? cue.move : String(cue.move)}`;
	if (cue.cue === 'highlight') return `/highlight/${encodeURIComponent(cue.topic)}`;
	return null;
}

/** What the model is told for one beat: speak exactly this, nothing about the control. */
export const lineInstruction = (say: string, n: number, total: number) =>
	`[SILENT CONTROL — never spoken aloud; do not read, repeat or mention it. You are presenting (line ${n} of ${total}); ` +
	`the slide is already set. Say ONLY this line, naturally, then stop and wait: "${say.replace(/"/g, "'")}"]`;
