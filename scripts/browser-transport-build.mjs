// browser-transport-build — the ONE esbuild recipe for the browser voice
// transport artifact.
//
// Two callers compile src/web-voice-transport.ts for the browser:
//
//   - scripts/build-bundle.mjs  → writes dist/web-voice-transport.browser.js
//                                 for the packaged desktop runtime
//   - src/web-client.ts         → compiles on demand in source mode, so
//                                 `tsx src/web-client.ts` never serves a stale
//                                 hand-prepared artifact
//
// They share this module so the two modes cannot drift: same entry, same
// target, same global name. If a future change needs different options per
// mode, that difference belongs here as an explicit parameter, not as a second
// copy of the recipe.
//
// NOTE ON PACKAGING: esbuild is a devDependency. The packaged app must never
// reach this module — it reads the prebuilt dist artifact instead. web-client
// imports it through a non-literal specifier so esbuild cannot pull esbuild
// itself into dist/web-client.js.

import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..');

/** Source of truth for the transport entry point. */
export const BROWSER_TRANSPORT_ENTRY = 'src/web-voice-transport.ts';

/** Basename of the built artifact, shared by the builder and the serve route. */
export const BROWSER_TRANSPORT_ARTIFACT = 'web-voice-transport.browser.js';

/**
 * The global the IIFE installs. web-client's page reads
 * `SutandoVoice.VoiceTransport`; renaming this is a breaking change to the
 * served page and must be done in both places at once.
 */
export const BROWSER_TRANSPORT_GLOBAL = 'SutandoVoice';

/**
 * esbuild options for the browser artifact.
 *
 * `platform: 'browser'` + `format: 'iife'` because the page loads it with a
 * plain <script> tag — no module loader, no import map, and no dependency on
 * how the page itself is served. `target: es2020` matches the inline browser
 * code this replaces (optional chaining and nullish coalescing are already used
 * there), so no surface loses a browser it supported today.
 */
export function browserTransportOptions({ outfile } = {}) {
  return {
    entryPoints: [join(repo, BROWSER_TRANSPORT_ENTRY)],
    bundle: true,
    platform: 'browser',
    format: 'iife',
    globalName: BROWSER_TRANSPORT_GLOBAL,
    target: 'es2020',
    logLevel: 'warning',
    ...(outfile ? { outfile } : { write: false }),
  };
}

/**
 * Compile the transport and return the JS as a string, without touching disk.
 * Used by source mode, where writing into the checkout would pollute
 * `git status` and create exactly the stale artifact this design avoids.
 */
export async function buildBrowserTransportSource() {
  const specifier = 'esbuild';
  const { build } = await import(specifier);
  const result = await build(browserTransportOptions());
  const out = result.outputFiles?.[0];
  if (!out) throw new Error('browser transport build produced no output');
  return out.text;
}
