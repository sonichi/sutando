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
import { describe, it, before, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, execSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { ExecProbe } from '../src/python-binary.js';
import {
	PythonUnavailableError,
	isExecutableFile,
	bundledPythonCandidates,
	developerToolsInstalled,
	requirePython,
	resetCacheForTests,
	resolvePython,
	pythonOnPath,
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

	it('uses a NON-stub python from PATH with no developer tools', () => {
		// A python.org framework install (/usr/local/bin/python3) or pyenv has
		// nothing to do with the CLT. An earlier revision declined it whenever
		// the tools were absent, withholding an interpreter that works.
		const py = selectPython({
			bundled: [],
			onPath: '/usr/fake/local/bin/python3',
			isExecutable: () => false,
			toolsInstalled: never,           // must not even be consulted
			isSystemStub: () => false,
		});
		assert.equal(py, '/usr/fake/local/bin/python3');
	});

	it('gates ONLY the system stub on developer tools', () => {
		const args = {
			bundled: [] as string[],
			onPath: STUB,
			isExecutable: () => false,
			isSystemStub: () => true,
		};
		assert.equal(selectPython({ ...args, toolsInstalled: () => true }), STUB);
		assert.equal(selectPython({ ...args, toolsInstalled: () => false }), null);
	});

	it('returns null when PATH has no python at all', () => {
		// The clean-VM case — the whole point of the fix.
		const py = selectPython({
			bundled: [],
			onPath: null,
			isExecutable: () => false,
			toolsInstalled: never,
		});
		assert.equal(py, null);
	});
});

describe('pythonOnPath', () => {
	it('returns the first executable python3 on PATH', () => {
		const found = pythonOnPath('/usr/fake/a:/usr/fake/b', (p) => p === '/usr/fake/b/python3');
		assert.equal(found, '/usr/fake/b/python3');
	});

	it('returns null when PATH has none', () => {
		assert.equal(pythonOnPath('/usr/fake/a', () => false), null);
	});

	it('skips empty PATH entries', () => {
		assert.equal(pythonOnPath('::', () => false), null);
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

	it('produces the EXACT packaged-app path, with no doubled runtime segment', () => {
		// Asserted exactly, not by endsWith. An endsWith check passes against
		// '<Resources>/runtime/bin/runtime/python/bin/python3' — a path that does
		// not exist — which is how the doubled segment shipped (#2475 review).
		const execPath = '/Applications/Sutando.app/Contents/Resources/runtime/bin/node';
		const expected = '/Applications/Sutando.app/Contents/Resources/runtime/python/bin/python3';
		const cands = bundledPythonCandidates(undefined, execPath);
		assert.ok(
			cands.includes(expected),
			`packaged sibling ${expected} missing from ${JSON.stringify(cands, null, 2)}`,
		);
	});

	it('never emits a doubled runtime segment', () => {
		const cands = bundledPythonCandidates(
			'/usr/fake/engine',
			'/Applications/Sutando.app/Contents/Resources/runtime/bin/node',
		);
		const doubled = cands.filter((c) => c.includes(join('runtime', 'bin', 'runtime'))
			|| c.includes(join('runtime', 'runtime')));
		assert.deepEqual(doubled, [], `malformed candidates: ${JSON.stringify(doubled)}`);
	});

	it('tolerates an unknown repo root', () => {
		const cands = bundledPythonCandidates(undefined, '/usr/fake/node/bin/node');
		assert.ok(cands.length > 0);
		assert.ok(cands.every((c) => c.endsWith(join('python', 'bin', 'python3'))));
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

describe('activated call-site shapes (executable probe)', () => {
	// The two consumers both swallow failures in a `catch`, so a unit-correct
	// resolver can still leave both GUI actions silently dead. These probes
	// execute the resolved interpreter through the EXACT shape each call site
	// uses, under a packaged layout whose path contains a space — which is the
	// case shellQuote exists for (#2475 review, P1 activated-path evidence).
	let tmp: string;
	let bundledPy: string;

	before(() => {
		tmp = mkdtempSync(join(tmpdir(), 'sutando-pyprobe-'));
		// Deliberate space, mirroring "/Applications/Sutando.app/...".
		const resources = join(tmp, 'My App', 'Contents', 'Resources');
		mkdirSync(join(resources, 'runtime', 'bin'), { recursive: true });
		mkdirSync(join(resources, 'runtime', 'python', 'bin'), { recursive: true });
		writeFileSync(join(resources, 'runtime', 'bin', 'node'), '', { mode: 0o755 });
		bundledPy = join(resources, 'runtime', 'python', 'bin', 'python3');
		// SELF-CONTAINED: this fixture must never invoke ambient `python3`.
		// An earlier revision was `exec python3 "$@"`, which on the very clean Mac
		// this PR targets resolves to the CLT stub — so running the regression
		// suite there could raise the exact modal the fix prevents, and both probes
		// failed outright under a python-less PATH (@john-the-dev, reviewing
		// #2475). A /bin/sh fake that echoes the argument after `-c` proves the
		// resolved path was executed and received its args intact, with no
		// interpreter installed anywhere.
		writeFileSync(bundledPy, '#!/bin/sh\necho "$2"\n', { mode: 0o755 });
	});

	after(() => rmSync(tmp, { recursive: true, force: true }));

	it('resolves the vendored python from the real packaged layout', () => {
		const fakeNode = join(tmp, 'My App', 'Contents', 'Resources', 'runtime', 'bin', 'node');
		const picked = selectPython({
			bundled: bundledPythonCandidates(undefined, fakeNode),
			isExecutable: isExecutableFile,
			toolsInstalled: () => false,   // no CLT — the case this must survive
		});
		assert.equal(picked, bundledPy);
	});

	// PATH deliberately stripped: proves these probes touch no ambient
	// interpreter, so the suite is safe to run on the target no-CLT host.
	const NO_PATH = { PATH: '/nonexistent' };

	it('meeting-tools shape: execFileSync runs the resolved path', () => {
		const out = execFileSync(bundledPy, ['-c', 'meet-ok'], {
			encoding: 'utf8', timeout: 20_000, env: NO_PATH,
		});
		assert.equal(out.trim(), 'meet-ok');
	});

	it('zoom shape: execSync runs the shell-quoted path containing a space', () => {
		assert.ok(bundledPy.includes(' '), 'fixture must contain a space to be meaningful');
		const out = execSync(`${shellQuote(bundledPy)} -c zoom-ok`, {
			encoding: 'utf8', timeout: 20_000, env: NO_PATH,
		});
		assert.equal(out.trim(), 'zoom-ok');
	});

	it('zoom shape FAILS without shellQuote — proving the quoting is load-bearing', () => {
		assert.throws(() => execSync(`${bundledPy} -c nope`, {
			encoding: 'utf8', stdio: 'pipe', timeout: 20_000, env: NO_PATH,
		}));
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
