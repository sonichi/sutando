/**
 * Who may retire a skip-marked result.
 *
 * `results/` is shared by every consumer, so "task-* plus a marker" retires
 * results a bridge never dispatched: the gateway's bookkeeping `[no-send]`
 * gets archived by the voice bridge, its tid leaves the OWNING bridge's
 * in-flight ledger, and a substantive reply written to that path afterwards
 * is read by nobody. Ownership is the discriminator.
 *
 * Dependency-light and standalone so it is testable without the bridge's
 * runtime deps (task-bridge.ts pulls the whole voice stack).
 */

export const SKIP_MARKER_RE = /^\s*\[(?:no-send|REPLIED)\]/i;

export function mayRetireSkipMarked(file: string, result: string,
	isOwn: (taskId: string) => boolean): boolean {
	if (!file.startsWith('task-') || !SKIP_MARKER_RE.test(result)) return false;
	return isOwn(file.replace(/\.txt$/, ''));
}
