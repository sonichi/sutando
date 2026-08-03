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

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

import {
	BROWSER_TRANSPORT_ARTIFACT,
	BROWSER_TRANSPORT_GLOBAL,
	buildBrowserTransportSource,
} from '../scripts/browser-transport-build.mjs';

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
