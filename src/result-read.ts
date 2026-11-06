/** Read a result through the shared receipt and readiness verifier. */
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { findRepoRoot } from './sutando_config.js';
import { requirePython } from './python-binary.js';

let python: string | undefined;
const interpreter = () => python ??= requirePython();

export function readVerifiedResult(path: string): string | null {
	try {
		const root = findRepoRoot(dirname(fileURLToPath(import.meta.url)));
		if (!root) return null;
		return execFileSync(interpreter(), [join(root, 'src', 'result_write.py'), 'read', path],
			{ encoding: 'utf8', timeout: 5000, maxBuffer: Infinity, stdio: ['ignore', 'pipe', 'ignore'] });
	} catch {
		return null;
	}
}

export function publishVerifiedResult(path: string, body: string): void {
	const root = findRepoRoot(dirname(fileURLToPath(import.meta.url)));
	if (!root) throw new Error('Result publisher repository unavailable');
	execFileSync(interpreter(), [join(root, 'src', 'result_publish.py'), path],
		{ input: body, encoding: 'utf8', timeout: 5000, stdio: ['pipe', 'pipe', 'pipe'] });
}
