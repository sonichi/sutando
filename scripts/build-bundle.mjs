#!/usr/bin/env node
// build-bundle — compile the node/TS services into self-contained JS artifacts.
//
// WHY: the services run in dev via `npx tsx src/<x>.ts`, which needs node + tsx +
// the full node_modules (~213 MB) at runtime. For the bundled desktop app that's
// too heavy and requires tsx on PATH. This produces `dist/<service>.js` with deps
// inlined (~2.6 MB for voice-agent), so a packaged install runs them with plain
// node: `<bundled-node> dist/voice-agent.js` — no tsx, no node_modules.
// Consumed by ag2space-cinny-desktop packaging (build-services.sh → this script).
//
// NODE VERSION: target node22 — `src/conversation-store.ts` imports `node:sqlite`,
// which is only a builtin on node >= 22.5 (v20 LTS dies at boot with
// ERR_UNKNOWN_BUILTIN_MODULE). The bundled node runtime must be >= 22.5.
//
// FOLLOW-UP (not blocking): a couple of manifest-skill `tools.ts` (obsidian-vault,
// screen-companion) dynamic-import `src/*.js` at runtime; those relative paths
// don't resolve from a bundled artifact. Voice-agent CORE is unaffected — but full
// packaging that wants those inline skill-tools must ship the referenced src files.

import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { statSync } from 'node:fs';
import {
  BROWSER_TRANSPORT_ARTIFACT,
  BROWSER_TRANSPORT_ENTRY,
  browserTransportOptions,
} from './browser-transport-build.mjs';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..');

// Entrypoints that run on node (TypeScript via tsx today).
const ENTRYPOINTS = [
  'src/voice-agent.ts',
  'src/web-client.ts',
  'skills/phone-conversation/scripts/conversation-server.ts',
  'skills/quota-tracker/scripts/credential-proxy.ts',
  'src/observability/boot.ts',
  // Track 9: the call-tiers emitter must run on desktop installs too, where
  // launch-sutando.sh skips startup.sh and there is no tsx — the launcher
  // spawns this dist with the bundled node instead (one emitter impl, no
  // second-language rewrite; the #2129 single-instance PID guard carries over).
  'src/emit-call-tiers.ts',
];

// ESM output + some CJS deps (e.g. dotenv) that call require() / use __dirname.
// Shim them so the bundled ESM artifact runs under plain node.
const CJS_INTEROP_BANNER =
  "import{createRequire as __cr}from'module';" +
  "import{fileURLToPath as __fu}from'url';" +
  "import{dirname as __dn}from'path';" +
  'const require=__cr(import.meta.url);' +
  'const __filename=__fu(import.meta.url);' +
  'const __dirname=__dn(__filename);';

// ws ships optional native accelerators; they're not required for correctness.
const NATIVE_EXTERNAL = ['bufferutil', 'utf-8-validate'];

const human = (n) => (n < 1024 * 1024 ? `${(n / 1024).toFixed(0)}KB` : `${(n / 1024 / 1024).toFixed(1)}MB`);

let failed = false;
for (const entry of ENTRYPOINTS) {
  const base = entry.split('/').pop().replace(/\.ts$/, '');
  const outfile = join(repo, 'dist', `${base}.js`);
  try {
    await build({
      entryPoints: [join(repo, entry)],
      outfile,
      bundle: true,
      platform: 'node',
      format: 'esm',
      target: 'node22', // node:sqlite requires >= 22.5 (conversation-store)
      banner: { js: CJS_INTEROP_BANNER },
      external: NATIVE_EXTERNAL,
      logLevel: 'warning',
    });
    console.log(`  ✓ ${entry.padEnd(52)} → dist/${base}.js  ${human(statSync(outfile).size)}`);
  } catch (err) {
    console.error(`  ✗ ${entry} — ${err.message}`);
    failed = true;
  }
}

// The browser voice transport is a SEPARATE build: platform:browser + IIFE, not
// node/ESM. The packaged web-client serves this file to the page; without it the
// voice UI has no transport at all (there is no inline copy to fall back to), so
// a failure here fails the whole bundle rather than warning.
{
  const outfile = join(repo, 'dist', BROWSER_TRANSPORT_ARTIFACT);
  try {
    await build(browserTransportOptions({ outfile }));
    console.log(
      `  ✓ ${BROWSER_TRANSPORT_ENTRY.padEnd(52)} → dist/${BROWSER_TRANSPORT_ARTIFACT}  ${human(statSync(outfile).size)}  [browser/iife]`,
    );
  } catch (err) {
    console.error(`  ✗ ${BROWSER_TRANSPORT_ENTRY} (browser) — ${err.message}`);
    failed = true;
  }
}

if (failed) {
  console.error('\nbuild:bundle FAILED');
  process.exit(1);
}
console.log('\nbuild:bundle OK — run artifacts with: <node>=22.5+ dist/<service>.js');
