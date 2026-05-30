/**
 * Task-file locator + shared archive helper (#1335 sub-PR-1).
 *
 * Two primitives:
 *
 * - `findTaskFile(tasksDir, taskId)` — locator that handles the
 *   `.claimed-core-N` rename written by `claim_task.py` (#884). Bridge
 *   archive calls that hard-code `task-{id}.txt` silently no-op after
 *   claiming, leaving stranded files in `tasks/` forever.
 *
 * - `archiveFile(srcPath, kind, taskId, base)` — move `srcPath` into
 *   `<base>/<kind>/archive/<YYYY-MM>/<task_id>.txt`. Replaces the
 *   duplicated impl previously inline in `src/task-bridge.ts`. The Python
 *   counterpart is `src/task_archive.py:archive_file`.
 *
 * The TypeScript and Python implementations share the behavioral contract
 * documented in `docs/bridge-helpers-design.md` (sub-PR-1 section). A
 * parity test at `tests/task-archive-parity.test.py` exercises both
 * implementations against the same fixtures.
 *
 * Usage:
 *
 *   import { archiveFile, findTaskFile } from './task-archive.js';
 *
 *   const taskFile = findTaskFile(TASKS_DIR, taskId);
 *   if (taskFile) {
 *     archiveFile(taskFile, 'tasks', taskId, REPO_DIR);
 *   }
 */
import { existsSync, mkdirSync, readdirSync, renameSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Return the actual task file path for `taskId`, or `null` if absent.
 *
 * Checks the bare name first (unclaimed), then scans for the claimed
 * variant (`task-{id}.claimed-core-N.txt`). If multiple claimed variants
 * exist (defensively — shouldn't happen), returns the first lexicographic
 * match — the caller only needs one path to archive.
 */
export function findTaskFile(tasksDir: string, taskId: string): string | null {
	const bare = join(tasksDir, `${taskId}.txt`);
	if (existsSync(bare)) return bare;
	let entries: string[];
	try {
		entries = readdirSync(tasksDir);
	} catch {
		return null;
	}
	const claimedPrefix = `${taskId}.claimed-core-`;
	const matches = entries.filter(n => n.startsWith(claimedPrefix) && n.endsWith('.txt')).sort();
	return matches.length > 0 ? join(tasksDir, matches[0]) : null;
}

/**
 * Move `srcPath` to `<base>/<kind>/archive/<YYYY-MM>/<taskId>.txt`.
 *
 * Silent no-op if `srcPath` does not exist. On any move failure, falls
 * back to `unlinkSync` so callers never leave stale task/result files
 * behind. Logs failures to stderr.
 *
 * Contract: see `docs/bridge-helpers-design.md` § task-archive helper.
 * Cross-language parity test: `tests/task-archive-parity.test.py`.
 */
export function archiveFile(
	srcPath: string,
	kind: 'tasks' | 'results',
	taskId: string,
	base: string,
): void {
	try {
		if (!existsSync(srcPath)) return;
		const ym = new Date().toISOString().slice(0, 7); // YYYY-MM
		const destDir = join(base, kind, 'archive', ym);
		mkdirSync(destDir, { recursive: true });
		renameSync(srcPath, join(destDir, `${taskId}.txt`));
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		console.error(`archiveFile(${kind}, ${taskId}) failed: ${msg}`);
		try {
			unlinkSync(srcPath);
		} catch {
			/* ignore */
		}
	}
}
