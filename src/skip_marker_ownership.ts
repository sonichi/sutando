/**
 * Skip markers separate TWO decisions that a single guard used to conflate.
 *
 * Suppression is universal: a [no-send]/[REPLIED] body must never be
 * narrated or delivered by anyone, whoever dispatched it. Retirement is
 * ownership-scoped: results/ is shared, so only the dispatching consumer may
 * archive. Gating both on one predicate makes a foreign marked result fall
 * through and be SPOKEN — worse than the mis-archive it replaced.
 *
 * Dependency-light and standalone so it is testable without the bridge's
 * runtime deps (task-bridge.ts pulls the whole voice stack).
 */

export const SKIP_MARKER_RE = /^\s*\[(?:no-send|REPLIED)\]/i;

/** Universal: this result must not be narrated, delivered, or forwarded. */
export function isSkipMarked(file: string, result: string): boolean {
	return file.startsWith('task-') && SKIP_MARKER_RE.test(result);
}

/** Ownership-scoped: may THIS bridge archive it and retire its task row? */
export function mayRetireSkipMarked(file: string, result: string,
	isOwn: (taskId: string) => boolean): boolean {
	return isSkipMarked(file, result) && isOwn(file.replace(/\.txt$/, ''));
}
