/**
 * Task overlap classifier for issue #1487.
 *
 * Determines whether two queued tasks are safe to execute concurrently.
 * Used by the proactive-loop when multiple tasks arrive while one is in-flight.
 *
 * Heuristics (conservative first pass):
 *   - Tasks with explicit serial-dependency language queue serially regardless.
 *   - "code" tasks always serialize — they mutate git state / filesystem.
 *   - CANCEL_INSTRUCTION tasks never overlap — they must interrupt immediately.
 *   - Two tasks in *different* non-code categories are safe to overlap (1 max).
 *   - When uncertain, serialize (false).
 */

/** Broad category inferred from the task body text. */
export type TaskCategory =
	| 'cancel'    // CANCEL_INSTRUCTION prefix — must interrupt, never overlaps
	| 'code'      // code / git / PR / test / deploy — serialized (shared state)
	| 'research'  // search / read / summarize / explain — stateless web/file reads
	| 'email'     // gmail / mail / inbox / send / draft
	| 'calendar'  // schedule / meeting / event / reminder / appointment
	| 'file'      // open / save / move / copy — filesystem mutations
	| 'unknown';  // catch-all

/** Extract the freeform task body from a raw task-file string.
 *
 * Task files put freeform user content after the `task:` header line
 * (established by PR #982 to prevent header-field forging). This helper
 * returns everything from the first `task:` line onward so classifiers see
 * only the actual request text — not `source: voice` etc. */
export function extractTaskBody(rawContent: string): string {
	const idx = rawContent.indexOf('\ntask:');
	if (idx === -1) return rawContent;
	return rawContent.slice(idx + 1); // include the "task:" line
}

/**
 * Classify a task body into a broad category.
 *
 * Operates on the task body (output of extractTaskBody), not the full
 * raw task-file content — header fields like `source: voice` must not
 * skew the keyword match.
 */
export function classifyTaskCategory(taskBody: string): TaskCategory {
	// CANCEL_INSTRUCTION appears after the "task: " prefix, not at line-start.
	if (/\bCANCEL_INSTRUCTION:/m.test(taskBody)) return 'cancel';

	// Check from most-specific to least-specific. "code" is last among
	// specific categories because "fix a bug" could match "research" too.
	if (/\b(gmail|inbox|draft\s+email|send\s+email|reply\s+to|unread\s+email|email\s+to)\b/i.test(taskBody)) return 'email';
	if (/\b(calendar|schedule\s+(a\s+)?(meeting|event|call)|add\s+(a\s+)?reminder|appointments?)\b/i.test(taskBody)) return 'calendar';
	if (/\b(fix\s+(the\s+)?bug|pull\s+request|create\s+(a\s+)?PR|open\s+(a\s+)?PR|git\s+|commit|refactor|implement|deploy|build\s+fail|test\s+fail|run\s+tests?)\b/i.test(taskBody)) return 'code';
	if (/\b(open\s+file|save\s+(the\s+)?file|move\s+(the\s+)?file|delete\s+(the\s+)?file|create\s+(a\s+)?folder)\b/i.test(taskBody)) return 'file';
	if (/\b(search|look\s+up|find\s+(me\s+)?|what\s+is|who\s+is|explain|summarize|research|read\s+(the\s+)?article|check\s+(the\s+)?news|weather)\b/i.test(taskBody)) return 'research';

	return 'unknown';
}

/**
 * Returns true if taskBody contains explicit serial-dependency language.
 *
 * Phrases like "after you finish X", "depends on", "once X is done", "wait for"
 * mean the user intended strict ordering — never overlap these.
 */
export function hasSerialDependency(taskBody: string): boolean {
	return /\b(after\s+(you\s+)?(finish|complete|do)|depends?\s+on|once\s+\S+('s?\s+|\s+is\s+)(done|complete|finished)|wait\s+for|when\s+you('re|\s+are)\s+(done|finished))\b/i.test(taskBody);
}

/**
 * Returns true if it is safe to start `body2` while `body1` is still in-flight.
 *
 * Both bodies should be the full raw task-file content; extractTaskBody is
 * applied internally.
 *
 * Rules (conservative first pass per issue #1487):
 *   1. Serial-dependency language in either task → serialize.
 *   2. Either task is "cancel" → serialize (must interrupt).
 *   3. Either task is "code" → serialize (shared git/filesystem state).
 *   4. Same category → serialize (may share external state).
 *   5. Different non-code, non-cancel categories → safe to overlap (1 extra
 *      concurrent task max; enforced by the caller).
 */
export function areSafeToOverlap(rawContent1: string, rawContent2: string): boolean {
	const body1 = extractTaskBody(rawContent1);
	const body2 = extractTaskBody(rawContent2);

	if (hasSerialDependency(body1) || hasSerialDependency(body2)) return false;

	const cat1 = classifyTaskCategory(body1);
	const cat2 = classifyTaskCategory(body2);

	if (cat1 === 'cancel' || cat2 === 'cancel') return false;
	if (cat1 === 'code' || cat2 === 'code') return false;
	if (cat1 === cat2) return false; // same-category tasks may share external state

	return true;
}
