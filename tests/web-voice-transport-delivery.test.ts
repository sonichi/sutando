/**
 * Browser delivery of the canonical voice transport.
 *
 * src/web-voice-transport.ts is real TypeScript; the page needs JavaScript. The
 * two runtime modes get there differently (source mode compiles on demand,
 * bundled mode ships a prebuilt artifact), and the whole point of the design is
 * that neither can silently serve something stale or wrong. These tests pin the
 * parts that would fail SILENTLY if they drifted:
 *
 *   - the artifact really is an IIFE that installs the expected global (a
 *     format regression would surface only as a dead Connect button)
 *   - the artifact name is identical in all three places that reference it —
 *     the builder, the packaging check, and the serve route
 *
 * They deliberately do NOT copy implementation text into assertions: the
 * transport is compiled and EXECUTED here, so a change that breaks the browser
 * build fails the test rather than passing a string comparison.
 */

import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { spawn, type ChildProcess } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import vm from 'node:vm';

import {
	BROWSER_TRANSPORT_ARTIFACT,
	BROWSER_TRANSPORT_GLOBAL,
	buildBrowserTransportSource,
} from '../scripts/browser-transport-build.mjs';
import { setupTempWorkspace } from './_helpers/temp-workspace.js';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..');

/**
 * The surface the artifact must expose to the page. Deliberately structural
 * rather than importing the real types: this asserts what the *built browser
 * bundle* hands the page, which is the thing that can silently regress — a
 * type import would check the source and prove nothing about the artifact.
 */
interface TransportGlobal {
	VoiceTransport: new (opts?: unknown) => unknown;
	classifyMicError: (name?: string, message?: string) => string;
	downsample: (input: Float32Array, fromRate: number, toRate: number) => Float32Array;
	float32ToInt16: (f32: Float32Array) => Int16Array;
	int16ToFloat32: (buf: ArrayBuffer) => Float32Array;
	[key: string]: unknown;
}

/** Run the IIFE in a bare context and hand back the global it installed. */
function evaluateArtifact(js: string): TransportGlobal | undefined {
	const sandbox: Record<string, unknown> = {};
	vm.createContext(sandbox);
	vm.runInContext(js, sandbox);
	return sandbox[BROWSER_TRANSPORT_GLOBAL] as TransportGlobal | undefined;
}

describe('browser transport delivery — source mode (on-demand compile)', () => {
	it('compiles to an IIFE that installs the global with the transport surface', async () => {
		const js = await buildBrowserTransportSource();

		// No DOM in this sandbox. Loading the artifact must not touch browser
		// APIs — if it did, the <script> tag would throw before the page could
		// even report a problem.
		const api = evaluateArtifact(js);

		assert.ok(api, `${BROWSER_TRANSPORT_GLOBAL} was not defined by the artifact`);
		assert.equal(typeof api.VoiceTransport, 'function', 'VoiceTransport must be constructible');
		for (const fn of ['downsample', 'float32ToInt16', 'int16ToFloat32', 'classifyMicError']) {
			assert.equal(typeof api[fn], 'function', `${fn} must be exported to the page`);
		}
	});

	it('carries the real DSP, not a stub — downsample is identity at equal rates', async () => {
		const api = evaluateArtifact(await buildBrowserTransportSource());
		const input = new Float32Array([0.5, -0.5, 0.25]);
		assert.deepEqual(Array.from(api.downsample(input, 16000, 16000)), [0.5, -0.5, 0.25]);
		// Halving the rate halves the sample count.
		assert.equal(api.downsample(new Float32Array(100), 32000, 16000).length, 50);
	});

	// Amendment R11: the artifact must be the classic-script IIFE build — the
	// page loads it with a plain <script> tag, which cannot execute ESM. An
	// accidental format switch to ESM would parse (as a module) and even lint,
	// but the browser would throw "Unexpected token 'export'" and the Connect
	// button would just be dead.
	it('is a classic script, not ESM (R11): parses as vm.Script and installs the global', async () => {
		const js = await buildBrowserTransportSource();
		// vm.Script compiles CLASSIC scripts only — top-level `import`/`export`
		// (ESM output) is a SyntaxError here, exactly like a <script> tag.
		assert.doesNotThrow(() => new vm.Script(js), 'artifact must compile as a classic script');
		const api = evaluateArtifact(js);
		assert.equal(
			typeof api?.VoiceTransport,
			'function',
			`classic-script evaluation must define ${BROWSER_TRANSPORT_GLOBAL}.VoiceTransport`,
		);
	});

	// Step 15/18 additions ride the same artifact: the page (and any surface
	// loading the IIFE) must see the new exports, not just the original four.
	it('exposes the Step-15/18 surface: classifyMicErrorCode + close codes + failure vocabulary', async () => {
		const api = evaluateArtifact(await buildBrowserTransportSource());
		assert.ok(api, 'global must be defined');
		assert.equal(typeof api!.classifyMicErrorCode, 'function');
		assert.equal((api!.classifyMicErrorCode as (n?: string) => string)('NotAllowedError'), 'permission');
		assert.equal(api!.CLOSE_CODE_CLIENT_BUSY, 4409);
		assert.equal(api!.CLOSE_CODE_SUPERSEDED_BY_TAKEOVER, 4410);
		assert.equal(typeof api!.VOICE_FAILURE_REMEDIATION, 'object');
	});
});

describe('browser transport delivery — artifact name agreement', () => {
	// Three files independently name this artifact. A rename in one place would
	// not fail any build: build:bundle would emit the new name, startup.sh would
	// keep checking the old one (and pass, because the old file lingers in a dev
	// checkout), and the serve route would 503 only at runtime in the packaged
	// app. So the agreement is asserted directly.
	it('startup.sh checks for the artifact build-bundle actually produces', () => {
		const startup = readFileSync(join(repo, 'src/startup.sh'), 'utf8');
		const base = BROWSER_TRANSPORT_ARTIFACT.replace(/\.js$/, '');
		assert.ok(
			startup.includes(base),
			`src/startup.sh must include "${base}" in its required-dist-artifact list`,
		);
	});

	it('web-client serves the same artifact name it is built under', () => {
		const webClient = readFileSync(join(repo, 'src/web-client.ts'), 'utf8');
		assert.ok(
			webClient.includes(BROWSER_TRANSPORT_ARTIFACT),
			`src/web-client.ts must reference "${BROWSER_TRANSPORT_ARTIFACT}"`,
		);
	});

	it('the page global is referenced by the name the build installs', async () => {
		const js = await buildBrowserTransportSource();
		// esbuild emits `var <globalName> = (() => { ... })()`.
		assert.match(js, new RegExp(`\\b${BROWSER_TRANSPORT_GLOBAL}\\b`));
	});
});

// ─── Serve-path smoke (impl plan Step 16, amendment R11) ─────────────────────
//
// The page integration is only real if the RUNNING web-client actually serves
// the artifact at the route the page's <script> names, and serves it as
// classic-script JavaScript defining SutandoVoice.VoiceTransport — NOT ESM.
// Spawns web-client on its own port (pattern: tests/agent-state-endpoint.test.ts).

describe('browser transport delivery — serve path (spawned web-client)', () => {
	const PORT = 18094; // distinct from agent-state-endpoint's 18081
	const { workspace: TEMP_WORKSPACE, cleanup: cleanupTempWorkspace } =
		setupTempWorkspace('transport-delivery');
	let child: ChildProcess | null = null;

	async function ensureStarted(): Promise<void> {
		if (child) return;
		child = spawn('npx', ['tsx', 'src/web-client.ts'], {
			cwd: repo,
			env: {
				...process.env,
				CLIENT_PORT: String(PORT),
				PORT: '19902',
				CLIENT_HOST: '127.0.0.1',
				SUTANDO_WORKSPACE: TEMP_WORKSPACE,
				SUTANDO_TEST_MODE: '1',
			},
			stdio: 'ignore',
		});
		const deadline = Date.now() + 20_000;
		while (Date.now() < deadline) {
			try {
				const res = await fetch(`http://127.0.0.1:${PORT}/sse-status`);
				if (res.ok) return;
			} catch {
				/* not ready */
			}
			await delay(200);
		}
		throw new Error('web-client did not start within 20s');
	}

	after(async () => {
		if (child && !child.killed) {
			await new Promise<void>((resolve) => {
				const hardKill = setTimeout(() => {
					try {
						child!.kill('SIGKILL');
					} catch {
						/* already dead */
					}
					resolve();
				}, 2_000);
				child!.once('exit', () => {
					clearTimeout(hardKill);
					resolve();
				});
				child!.kill('SIGTERM');
			});
		}
		cleanupTempWorkspace();
	});

	it('GET /web-voice-transport.js → 200 classic-script IIFE defining the transport global (R11)', async () => {
		await ensureStarted();
		const res = await fetch(`http://127.0.0.1:${PORT}/web-voice-transport.js`);
		assert.equal(res.status, 200);
		assert.match(res.headers.get('content-type') ?? '', /javascript/);
		const js = await res.text();

		// Classic script, not ESM: must compile under vm.Script (a top-level
		// `export`/`import` would throw exactly like it would in <script>).
		assert.doesNotThrow(() => new vm.Script(js), 'served JS must be a classic script (IIFE), not ESM');
		const api = evaluateArtifact(js);
		assert.ok(api, `served JS must define ${BROWSER_TRANSPORT_GLOBAL}`);
		assert.equal(
			typeof api!.VoiceTransport,
			'function',
			`served JS must define ${BROWSER_TRANSPORT_GLOBAL}.VoiceTransport`,
		);
		assert.equal(typeof api!.classifyMicErrorCode, 'function', 'Step-15 export rides the served artifact');
	});

	it('the served page loads the artifact via a classic <script> tag (no type="module")', async () => {
		await ensureStarted();
		const res = await fetch(`http://127.0.0.1:${PORT}/`);
		assert.equal(res.status, 200);
		const html = await res.text();
		assert.ok(
			html.includes('<script src="/web-voice-transport.js"></script>'),
			'page must load the served artifact with a classic script tag',
		);
		assert.ok(
			!/<script[^>]*type="module"[^>]*web-voice-transport/.test(html),
			'the transport must not be loaded as an ESM module',
		);
		// The page instantiates the canonical transport — the inline copies are gone.
		assert.match(html, /new SutandoVoice\.VoiceTransport\(/);
	});
});
