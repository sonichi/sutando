/**
 * Tiny CLI shim used by tests/task-archive-parity.test.py to invoke the
 * TypeScript archiveFile() helper from a Python parity test.
 *
 * Reads a JSON payload from $PARITY_ARGS env var (avoiding argv-quoting
 * issues across tsx --eval) and calls archiveFile() with the parsed args.
 * Silent on success; errors propagate via console.error (the TS impl
 * already does fallback unlink + console.error on failure).
 *
 * Usage from Python:
 *   subprocess.run(
 *       ['npx', 'tsx', 'tests/_helpers/task-archive-shim.ts'],
 *       env={**os.environ, 'PARITY_ARGS': json.dumps(...)},
 *       cwd=REPO_ROOT,
 *   )
 */
import { archiveFile } from '../../src/task-archive.js';

const raw = process.env.PARITY_ARGS;
if (!raw) {
	console.error('task-archive-shim: PARITY_ARGS env var not set');
	process.exit(2);
}

const args = JSON.parse(raw) as { src: string; kind: 'tasks' | 'results'; taskId: string; base: string };
archiveFile(args.src, args.kind, args.taskId, args.base);
