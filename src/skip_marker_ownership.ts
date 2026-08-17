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

/**
 * Task families with no network consumer: nothing else will ever archive
 * them, so this bridge is their archiver of last resort. Cron depends on
 * it explicitly — codex-scheduler writes [no-send] "so this scheduled task
 * is archived" — and discord-bridge's skip handling is already scoped to
 * its own pending map, so scoping this one without the allowlist would
 * leave them with no archiver at all. A NEW local family must be added
 * here or its results accumulate forever.
 */
export const LOCAL_ONLY_PREFIXES = [
	// Machine-generated families, also enumerated by web-client.ts's
	// isOwnerVisibleTask. That list answers "is this owner work to display";
	// this one answers "will anything else archive it" — task-chat- is on
	// this list and owner-VISIBLE there, so the two must not be collapsed.
	'task-cron-', 'task-health-', 'task-smoke-', 'task-discord-e2e-',
	// Locally originated, no network consumer.
	'task-chat-', 'task-workstream-grouping-', 'task-project-grouping-',
];

export function isLocalOnlyTask(taskId: string): boolean {
	return LOCAL_ONLY_PREFIXES.some(p => taskId.startsWith(p));
}

/** Ownership-scoped: may THIS bridge archive it and retire its task row? */
export function mayRetireSkipMarked(file: string, result: string,
	isOwn: (taskId: string) => boolean): boolean {
	if (!isSkipMarked(file, result)) return false;
	const taskId = file.replace(/\.txt$/, '');
	// The whole rule lives here so it is testable; the caller supplies only
	// the dynamic signals (dispatched-by-me, is-a-voice-task).
	return isLocalOnlyTask(taskId) || isOwn(taskId);
}
