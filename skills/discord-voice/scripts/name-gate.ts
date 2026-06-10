// Multi-bot voice name-gate — pure decision logic, no I/O.
// See notes/multi-bot-voice-gate-redesign.md for the full design + test matrix.

export interface GateConfig {
	/** This instance's stand name, e.g. "BotA". Empty disables the gate. */
	instanceName: string;
	/** Spoken-form aliases for instanceName (ASR variants). */
	nameAliases?: string[];
	/** Other instances' canonical names. */
	otherInstances?: string[];
	/** Spoken-form aliases for OTHER instances. */
	otherAliases?: string[];
	/** When true (and no name in transcript yet), respond to cold openers. */
	primary?: boolean;
	/**
	 * Meeting-buddy mode (single bot, multiple humans). When true the gate:
	 *   - starts SILENT (ignores `primary`) — waits to be named before answering,
	 *   - stays active even with no peer instances (own-name is the only wake),
	 *   - honors `standbyAliases` as an explicit "go silent but stay" command.
	 * See notes/multi-bot-voice-gate-redesign.md + PR #1427.
	 */
	meetingMode?: boolean;
	/** Spoken-form "standby / go quiet" phrases that re-silence the bot. */
	standbyAliases?: string[];
}

export type Decision = 'allow' | 'drop';

export interface GateState {
	readonly cfg: GateConfig;
	readonly nameVariants: string[];
	readonly otherVariants: string[];
	readonly standbyVariants: string[];
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
		// greet+name anywhere (also allow 1-2 word names like "BotA Lou")
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
	const standbyVariants = (cfg.standbyAliases ?? [])
		.map(s => s.trim()).filter(Boolean);
	return {
		cfg,
		nameVariants,
		otherVariants,
		standbyVariants,
		// Meeting mode starts SILENT — the bot waits to be named, regardless of
		// `primary`. Legacy (non-meeting) mode keeps the primary-driven default.
		lastAddressedToMe: cfg.meetingMode ? false : !!cfg.primary,
	};
}

// CJK address/imperative verbs — a Chinese summons names the bot then issues a
// request ("露西帮我看下屏幕" = Lucy, help me look at the screen). These are the
// CJK analogue of ADDRESS_VERBS: a mention verb like 说 (said) is deliberately
// EXCLUDED so "露西说的那个" (the thing Lucy said) is NOT treated as a summons.
const CJK_ADDRESS_VERBS = [
	'帮', '给', '打开', '看', '查', '告诉', '搜', '读', '念', '设置', '播放',
	'放', '调', '找', '显示', '截', '复制', '粘贴', '开', '关', '解释', '翻译',
	'记', '存', '发', '回答', '听', '过来', '醒',
];

/**
 * Bare-name / CJK-imperative summons (#1600 M2). The greet/comma/verb matchers
 * in isAddressedBy MISS the single most common live call pattern — a bare name
 * with nothing after it ("Lucy" / "Lucy." / "Loosey") and a Chinese name +
 * imperative ("露西帮我看下屏幕"). Both are unambiguous summons yet matched
 * NEITHER isAddressedBy NOR isWakePhrase, so after a reconnect cleared the
 * sticky addressed bit the group-active gate stayed muted ("叫不醒"). This is
 * OR-ed into the addressing decision (decideForTurn), NOT into isWakePhrase —
 * a bare name ANSWERS one turn but does not flip meeting→active (that still
 * needs a deliberate wake phrase), preserving the no-false-wake property.
 *
 * Safe against passing mentions: rule (1) fires ONLY when the WHOLE normalized
 * utterance IS the name ("I told Lucy" → "i told lucy" ≠ "lucy" → no match);
 * rule (2) fires only on a CJK name at the very start immediately followed by
 * an imperative verb (mention verbs like 说 are excluded).
 */
export function isBareSummons(text: string, names: string[]): boolean {
	if (!text || names.length === 0) return false;
	const norm = normalizeSpoken(text);
	if (!norm) return false;
	for (const raw of names) {
		const n = normalizeSpoken(raw);
		if (!n) continue;
		// (1) bare standalone name — the entire utterance is just the name.
		if (norm === n) return true;
		// (2) CJK name at the start + (optional space) + an imperative/request verb.
		if (/[一-鿿]/.test(n) && norm.startsWith(n)) {
			const rest = norm.slice(n.length).trimStart();
			if (rest && CJK_ADDRESS_VERBS.some(v => rest.startsWith(v))) return true;
		}
	}
	return false;
}

/**
 * Detect an explicit "standby / go quiet" command. Standby phrases are bare
 * command words ("standby", "待命"), not name-addressed imperatives, so this is
 * a word-boundary (ASCII) / substring (CJK) match rather than isAddressedBy.
 */
export function isStandby(text: string, standbyVariants: string[]): boolean {
	if (!text || standbyVariants.length === 0) return false;
	const lc = text.toLowerCase();
	for (const raw of standbyVariants) {
		const s = raw.toLowerCase().trim();
		if (!s) continue;
		const ascii = /^[\x00-\x7f]+$/.test(s);
		if (ascii ? new RegExp(`\\b${escape(s)}\\b`, 'i').test(lc) : lc.includes(s)) return true;
	}
	return false;
}

/**
 * Process one user-turn's transcript text and return the new decision.
 * Updates state in-place. Sticky semantics:
 *   - standby phrase → drop (sticky=false): go silent but stay in the room.
 *   - my-name addressed → allow (sticky=true)
 *   - other-name addressed → drop (sticky=false)
 *   - neither → unchanged (sticky carries)
 *
 * Meeting mode (single bot, multiple humans): the gate stays active even with
 * NO peer instances — own-name is the only wake, standby is the only sleep —
 * and (via createGate) starts silent. Legacy mode keeps the old "no peer →
 * always allow" shortcut so non-meeting single-bot deployments are unchanged.
 */
export function decideForTurn(state: GateState, userText: string, forceGate = false): Decision {
	// Standby: explicit "go silent but stay" — re-silences until next name cue.
	if (isStandby(userText, state.standbyVariants)) {
		state.lastAddressedToMe = false;
		return 'drop';
	}
	// Legacy single-bot (no peers, not meeting mode): gate disabled, allow all.
	// forceGate overrides this — the population-aware group regime (#1600 M3)
	// needs real addressing gating even with no peer instances configured, so it
	// can re-acquire the sticky addressed bit after a reconnect cleared it
	// (otherwise this early-return would skip the lastAddressedToMe update and
	// the group-active gate would stay muted = "叫不醒").
	if (!forceGate && !state.cfg.meetingMode && state.otherVariants.length === 0) return 'allow';
	const haveMyName = isAddressedBy(userText, state.nameVariants) || isBareSummons(userText, state.nameVariants);
	const haveOtherName = state.otherVariants.length > 0
		&& (isAddressedBy(userText, state.otherVariants) || isBareSummons(userText, state.otherVariants));
	if (haveMyName) state.lastAddressedToMe = true;
	else if (haveOtherName) state.lastAddressedToMe = false;
	return state.lastAddressedToMe ? 'allow' : 'drop';
}

/**
 * Normalize spoken text for phrase matching: lowercase, strip punctuation to
 * spaces, collapse whitespace. STT renders "Hi, Lucy! Wake up." — a raw
 * substring match misses "hi lucy" / "lucy wake up" because of the commas, so
 * every fixed-phrase matcher must compare against THIS form, never the raw
 * transcript. Keeps letters/digits in any script (CJK survives).
 */
export function normalizeSpoken(text: string): string {
	return text.toLowerCase().replace(/[^\p{L}\p{N}\s]+/gu, ' ').replace(/\s+/g, ' ').trim();
}

/**
 * Deterministic meeting→active wake matcher (#1427): does `text` carry a
 * name-qualified wake command for any of `names` (stand name + ASR aliases)?
 *
 * This is the fixed-phrase HALF of the wake decision — it is OR-ed with the
 * LLM audio classifier's wake=true judgment, so an obvious spoken wake
 * ("Hi Lucy, wake up" / "Lucy, can you hear me?") flips the bot active even
 * when the classifier misses or lags. Wake stays name-qualified: a bare
 * verb ("wake up", "active mode") without a name matches NOTHING, so channel
 * echo of a peer/own sentence can't wake this bot (the #1427 over-fire bug).
 *
 * Matching is word-boundary on the normalized form (space-padded includes),
 * so an alias can't fire as a prefix of a longer word ("may" vs "maybe").
 */
export function isWakePhrase(text: string, names: string[]): boolean {
	if (!text) return false;
	const t = ` ${normalizeSpoken(text)} `;
	if (t.includes(' stop meeting mode ')) return true;
	for (const raw of names) {
		const n = normalizeSpoken(raw);
		if (!n) continue;
		const forms = [
			`hi ${n}`, `hey ${n}`, `hello ${n}`, `okay ${n}`, `ok ${n}`,
			`${n} wake up`, `wake up ${n}`,
			`${n} active mode`, `${n} you can talk`,
			`${n} resume`, `${n} come back`,
			`${n} can you hear me`, `can you hear me ${n}`,
			`${n} are you there`, `${n} you there`,
		];
		if (forms.some(f => t.includes(` ${f} `))) return true;
	}
	return false;
}

// --- internals ---

function escape(s: string): string {
	return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
