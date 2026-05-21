import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/**
 * Per-process temp workspace for tests that touch workspace state.
 *
 * Established by #849 as the durable fix for the cross-test-file race
 * that produced flaky CI on #800 / #840 — concurrent test files writing
 * to a shared `<REPO_ROOT>/core-status.json` (and similar). Each test
 * process gets its own `$SUTANDO_WORKSPACE` so the `resolveWorkspace()`
 * helper in production code resolves to an isolated dir.
 *
 * Usage (module-level — call before importing the code under test, so the
 * code's module-init `WORKSPACE_DIR = resolveWorkspace()` captures the
 * temp path):
 *
 *   import { setupTempWorkspace } from './_helpers/temp-workspace.js';
 *   const { workspace, cleanup } = setupTempWorkspace('agent-state');
 *
 *   // ... write fixtures via `join(workspace, 'core-status.json')` etc.
 *
 *   after(cleanup);
 *
 * If you're spawning a subprocess that needs the same workspace, pass
 * `SUTANDO_WORKSPACE: workspace` in the child's env so its
 * `resolveWorkspace()` resolves to the same temp dir.
 *
 * The function also writes `process.env.SUTANDO_WORKSPACE = workspace`
 * for the test process itself — useful when the code under test reads
 * the env var directly OR when its module-init capture runs after this
 * helper.
 */
export function setupTempWorkspace(name: string): {
	workspace: string;
	cleanup: () => void;
} {
	const workspace = mkdtempSync(join(tmpdir(), `sutando-test-${name}-`));
	process.env.SUTANDO_WORKSPACE = workspace;
	return {
		workspace,
		cleanup: () => {
			try {
				rmSync(workspace, { recursive: true, force: true });
			} catch {
				/* idempotent */
			}
		},
	};
}
