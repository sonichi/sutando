/**
 * Regression tests: TypeScript call sites must RESOLVE python, never hardcode
 * /usr/bin/python3.
 *
 * On macOS /usr/bin/python3 is the Xcode Command Line Tools stub, not python —
 * one inode hardlinked across python3 / git / swift / swiftc / clang / gcc /
 * make. It exists on every Mac; spawning it without the tools installed raises
 * the modal "install command line developer tools" dialog and returns nothing.
 * An absolute path also cannot be shadowed by a real install on PATH.
 *
 * Two call sites spawned it directly:
 *
 *   skills/zoom/tools.ts   execSync(`/usr/bin/python3 -c "…`)     (Join-button click)
 *   src/meeting-tools.ts   execFileSync('/usr/bin/python3', …)    (camera toggle)
 *
 * Sibling fixes: #2469 (git, Python side), #2473 (python, Swift side).
 */
import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { ExecProbe } from '../src/python-binary.js';
import {
	PythonUnavailableError,
	bundledPythonCandidates,
	developerToolsInstalled,
	requirePython,
	resetCacheForTests,
	resolvePython,
	selectPython,
	shellQuote,
} from '../src/python-binary.js';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const STUB = '/usr/bin/py' + 'thon3'; // split so this file's own text is not a literal hit

const never = (): boolean => {
	throw new Error('toolsInstalled probed when an interpreter was already found');
};
const executableOnly = (paths: string[]) => (p: string) => paths.includes(p);

describe('selectPython ordering', () => {
	it('prefers $SUTANDO_PY when it is executable', () => {
		const py = selectPython({
			explicit: '/usr/fake/sutando-py',
			bundled: ['/usr/fake/bundled-py'],
			isExecutable: executableOnly(['/usr/fake/sutando-py', '/usr/fake/bundled-py']),
			toolsInstalled: never,
		});
		assert.equal(py, '/usr/fake/sutando-py');
	});

	it('ignores a $SUTANDO_PY that is not executable', () => {
		// A stale launcher export must not shadow a working bundled runtime.
		const py = selectPython({
			explicit: '/usr/fake/stale-py',
			bundled: ['/usr/fake/bundled-py'],
			isExecutable: executableOnly(['/usr/fake/bundled-py']),
			toolsInstalled: never,
		});
		assert.equal(py, '/usr/fake/bundled-py');
	});

	it('uses the bundled runtime with no developer tools at all', () => {
		// The bundled-install case: a vendored python needs no toolchain.
		const py = selectPython({
			bundled: ['/usr/fake/missing-py', '/usr/fake/bundled-py'],
			isExecutable: executableOnly(['/usr/fake/bundled-py']),
			toolsInstalled: () => false,
		});
		assert.equal(py, '/usr/fake/bundled-py');
	});

	it('falls back to a BARE python3, never the absolute stub path', () => {
		const py = selectPython({
			bundled: [],
			isExecutable: () => false,
			toolsInstalled: () => true,
		});
		assert.equal(py, 'python3');
		assert.notEqual(py, STUB, 'must never return the absolute CLT stub path');
	});

	it('returns null rather than spawning the stub', () => {
		// The clean-VM case — the whole point of the fix.
		const py = selectPython({
			bundled: [],
			isExecutable: () => false,
			toolsInstalled: () => false,
		});
		assert.equal(py, null);
	});
});

describe('developerToolsInstalled', () => {
	it('is true when xcode-select exits zero', () => {
		const ok: ExecProbe = () => Buffer.from('');
		assert.equal(developerToolsInstalled(ok), true);
	});

	it('fails closed when the probe throws', () => {
		const boom: ExecProbe = () => {
			throw new Error('xcode-select missing');
		};
		assert.equal(developerToolsInstalled(boom), false);
	});

	it('asks xcode-select, which is a real binary and does not prompt', () => {
		let called: string | undefined;
		const spy: ExecProbe = (file) => {
			called = file;
			return Buffer.from('');
		};
		developerToolsInstalled(spy);
		assert.equal(called, '/usr/bin/xcode-select');
	});
});

describe('bundledPythonCandidates', () => {
	it('includes the engine sibling documented by sutando-config.sh', () => {
		const cands = bundledPythonCandidates('/usr/fake/engine', '/usr/fake/node/bin/node');
		assert.ok(
			cands.includes(join('/usr/fake', 'runtime', 'python', 'bin', 'python3')),
			`engine sibling missing from ${JSON.stringify(cands)}`,
		);
	});

	it('tolerates an unknown repo root', () => {
		const cands = bundledPythonCandidates(undefined, '/usr/fake/node/bin/node');
		assert.ok(cands.length > 0);
		assert.ok(cands.every((c) => c.endsWith(join('runtime', 'python', 'bin', 'python3'))));
	});
});

describe('requirePython', () => {
	beforeEach(() => resetCacheForTests());

	it('throws PythonUnavailableError when nothing is runnable', () => {
		const saved = process.env.SUTANDO_PY;
		process.env.SUTANDO_PY = '/usr/fake/definitely-not-here';
		try {
			// Only assert the throw shape when this host genuinely has no
			// interpreter; on a developer machine resolvePython() succeeds via the
			// xcode-select tier and there is nothing to assert.
			if (resolvePython() === null) {
				assert.throws(() => requirePython(), PythonUnavailableError);
			} else {
				assert.equal(typeof requirePython(), 'string');
			}
		} finally {
			if (saved === undefined) delete process.env.SUTANDO_PY;
			else process.env.SUTANDO_PY = saved;
			resetCacheForTests();
		}
	});
});

describe('shellQuote', () => {
	it('quotes a path containing spaces', () => {
		assert.equal(shellQuote('/usr/fake/My App/python3'), "'/usr/fake/My App/python3'");
	});

	it('escapes an embedded single quote', () => {
		assert.equal(shellQuote("/usr/fake/it's/python3"), "'/usr/fake/it'\\''s/python3'");
	});
});

describe('call sites are wired to the resolver', () => {
	const sites = [
		join(REPO, 'src', 'meeting-tools.ts'),
		join(REPO, 'skills', 'zoom', 'tools.ts'),
	];

	it('no call site hardcodes the stub path', () => {
		for (const file of sites) {
			// Report line numbers rather than asserting on the file body — a
			// whole-file assertion prints the entire container and buries the hit.
			const hits = readFileSync(file, 'utf8')
				.split('\n')
				.map((line, i) => ({ line, n: i + 1 }))
				.filter(({ line }) => line.includes(`'${STUB}'`) || line.includes(`\`${STUB} `))
				.map(({ line, n }) => `  ${file}:${n}: ${line.trim()}`);
			assert.deepEqual(
				hits,
				[],
				`\n${file} still hardcodes the Xcode-CLT stub. Use requirePython().\n${hits.join('\n')}`,
			);
		}
	});

	it('every call site imports the resolver', () => {
		for (const file of sites) {
			const wired = readFileSync(file, 'utf8').includes('requirePython');
			assert.ok(wired, `${file} does not import requirePython from src/python-binary.js`);
		}
	});
});
