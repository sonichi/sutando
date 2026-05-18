// Multi-bot voice name-gate — pure decision logic, no I/O.
// See notes/multi-bot-voice-gate-redesign.md for the full design + test matrix.

export interface GateConfig {
	/** This instance's stand name, e.g. "Lucy". Empty disables the gate. */
	instanceName: string;
	/** Spoken-form aliases for instanceName (ASR variants). */
	nameAliases?: string[];
	/** Other instances' canonical names. */
	otherInstances?: string[];
	/** Spoken-form aliases for OTHER instances. */
	otherAliases?: string[];
	/** When true (and no name in transcript yet), respond to cold openers. */
	primary?: boolean;
}

export type Decision = 'allow' | 'drop';

export interface GateState {
	readonly cfg: GateConfig;
	readonly nameVariants: string[];
	readonly otherVariants: string[];
	/** Sticky last-addressed-to-me bit. */
	lastAddressedToMe: boolean;
}

export const ADDRESS_VERBS =
	'(can|could|will|would|please|tell|answer|design|write|read|check|look|help|stop|start|leave|join|log|hang|end)';

/**
 * Detect whether `text` ADDRESSES (not just mentions) any of `names`.
 * Returns true on:
 *   - "hi/hey/hello/yo/okay/ok NAME" or "hi, NAME" (greet + name)
 *   - "NAME," or "NAME?" (comma/question-tag — definite address marker)
 *   - "NAME VERB" at sentence start (imperative)
 * Returns false on plain mentions like "thanks NAME" or "NAME's answer".
 */
export function isAddressedBy(text: string, names: string[]): boolean {
	if (!text || names.length === 0) return false;
	const lc = text.toLowerCase();
	for (const raw of names) {
		const n = raw.toLowerCase().trim();
		if (!n) continue;
		// "hi/hey/... NAME" or "hi, NAME" — greeting allows optional punctuation
		const greet = new RegExp(`\\b(hi|hey|hello|yo|okay|ok)[,!:]?\\s+${escape(n)}\\b`, 'i');
		// "NAME," or "NAME?" — comma/question-tag (definite address marker)
		const commaTag = new RegExp(`\\b${escape(n)}\\s*[,?]`, 'i');
		// "NAME VERB" at sentence start (optional preceding . ! ?)
		const verbed = new RegExp(`(^|[.!?]\\s*)${escape(n)}\\s+${ADDRESS_VERBS}\\b`, 'i');
		if (greet.test(lc) || commaTag.test(lc) || verbed.test(lc)) return true;
	}
	return false;
}

/**
 * Open-world "addressed to someone other than me" detector. Catches greet/
 * comma/verb patterns that name ANY token not in `myNames` — so the operator
 * can say "Hi Bob" or "Hi Daddy" without us having to enumerate every alias
 * in the OTHER list. Stopwords filter pronouns/question-words/short verbs to
 * avoid false-positive on "you can hear me", "what time is it", etc.
 *
 * Anchored patterns (commaTag + verbed require sentence-start `(^|[.!?]\s*)`)
 * are intentional — without that, "is this math?" matches as "math?" address.
 */
export function isAddressedToOther(text: string, myNames: string[]): boolean {
	if (!text) return false;
	const myLc = myNames.map(n => n.toLowerCase().trim()).filter(Boolean);
	const lc = text.toLowerCase();
	const patterns = [
		// greet+name anywhere (also allow 1-2 word names like "Maddy Lou")
		/\b(hi|hey|hello|yo|okay|ok)[,!:]?\s+([a-z][a-z'-]*(?:\s+[a-z][a-z'-]*)?)\b/gi,
		// commaTag — require start-of-clause before name
		/(^|[.!?]\s*)([a-z][a-z'-]*)\s*[,?]/gi,
		// imperative — start-of-clause + name + verb
		new RegExp(`(^|[.!?]\\s*)([a-z][a-z'-]*)\\s+${ADDRESS_VERBS}\\b`, 'gi'),
	];
	for (const re of patterns) {
		let m;
		while ((m = re.exec(lc)) !== null) {
			// Group containing the captured name varies by pattern; find non-empty
			const name = (m[2] || m[1] || '').trim();
			if (!name || _STOPWORDS.has(name)) continue;
			const isMe = myLc.some(v => v === name || name.startsWith(v + ' '));
			if (!isMe) return true;
		}
	}
	return false;
}

const _STOPWORDS = new Set([
	// pronouns + question words
	'i', 'me', 'my', 'mine', 'you', 'your', 'yours', 'we', 'us', 'our', 'ours',
	'they', 'them', 'their', 'theirs', 'he', 'him', 'his', 'she', 'her', 'hers',
	'it', 'its', 'this', 'that', 'these', 'those', 'there', 'here',
	'who', 'whom', 'whose', 'what', 'which', 'when', 'where', 'why', 'how',
	// affirmations / acknowledgements
	'yes', 'no', 'yep', 'nope', 'yeah', 'nah', 'okay', 'ok', 'sure', 'right',
	'thanks', 'thank', 'please', 'sorry', 'maybe',
	// fillers / interjections
	'oh', 'um', 'uh', 'well', 'so', 'just', 'now', 'still', 'also',
	'and', 'or', 'but', 'if', 'as',
	// common short verbs that often start clauses
	'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
	'do', 'does', 'did', 'go', 'goes', 'went', 'come', 'came',
	'one', 'two', 'three', 'a', 'an', 'the', 'all', 'some', 'any',
]);

/** Construct initial gate state. */
export function createGate(cfg: GateConfig): GateState {
	const nameVariants = [cfg.instanceName, ...(cfg.nameAliases ?? [])]
		.map(s => s.trim()).filter(Boolean);
	const otherVariants = [...(cfg.otherInstances ?? []), ...(cfg.otherAliases ?? [])]
		.map(s => s.trim()).filter(Boolean);
	return {
		cfg,
		nameVariants,
		otherVariants,
		lastAddressedToMe: !!cfg.primary,
	};
}

/**
 * Process one user-turn's transcript text and return the new decision.
 * Updates state in-place. Sticky semantics:
 *   - my-name addressed → allow (sticky=true)
 *   - other-name addressed → drop (sticky=false)
 *   - neither → unchanged (sticky carries)
 * If OTHER_INSTANCES is empty (no peer present), gate is disabled and
 * always allows.
 */
export function decideForTurn(state: GateState, userText: string): Decision {
	if (state.otherVariants.length === 0) return 'allow'; // gate disabled
	const haveMyName = isAddressedBy(userText, state.nameVariants);
	const haveOtherName = isAddressedBy(userText, state.otherVariants);
	if (haveMyName) state.lastAddressedToMe = true;
	else if (haveOtherName) state.lastAddressedToMe = false;
	return state.lastAddressedToMe ? 'allow' : 'drop';
}

// --- internals ---

function escape(s: string): string {
	return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
