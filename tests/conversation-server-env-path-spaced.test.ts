/**
 * Regression guard for #2228 (conversation-server half): the phone
 * conversation-server loaded its `.env` from
 * `new URL('../../../.env', import.meta.url).pathname`. `URL.pathname` stays
 * percent-encoded, so on the desktop-bundled install
 * (".../Application Support/…") the path became ".../Application%20Support/.env"
 * — a file that does not exist — and dotenv silently loaded nothing, with no
 * error. The fix uses fileURLToPath (as the same file already does for
 * `_phoneSkillDir`).
 *
 * The sibling site src/voice-context.ts is fixed separately in PR #2206; this
 * test covers only the conversation-server site, which #2206 does not touch.
 *
 * The module lives at a fixed, unspaced repo path, so importing it here cannot
 * reproduce the spaced-path condition. Test 1 exercises the resolution
 * technique in a real spaced temp dir via subprocess; test 2 source-guards the
 * actual call site so a revert to `.pathname` fails here (mutation-checked).
 *
 * Runs under `tsx --test` (npm test); needs no build.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CONVERSATION_SERVER = 'skills/phone-conversation/scripts/conversation-server.ts';

test('fileURLToPath resolves an import.meta.url .env path on a spaced install; .pathname does not', () => {
  const base = mkdtempSync(join(tmpdir(), 'sutando-2228-'));
  // Mirror the ../../../.env climb from scripts/ up to a repo root, with a
  // space in an ancestor segment (the crux of the bug).
  const scriptDir = join(base, 'Application Support', 'repo', 'skills', 'phone', 'scripts');
  mkdirSync(scriptDir, { recursive: true });
  const repoRoot = join(base, 'Application Support', 'repo');
  writeFileSync(join(repoRoot, '.env'), 'PROBE=1\n');
  try {
    const probe = join(scriptDir, 'probe.mjs');
    writeFileSync(probe, `import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';
const bad = new URL('../../../.env', import.meta.url).pathname;
const good = fileURLToPath(new URL('../../../.env', import.meta.url));
process.stdout.write(JSON.stringify({
  badEncoded: bad.includes('%20'), badExists: existsSync(bad),
  goodExists: existsSync(good),
}));\n`);
    const out = JSON.parse(execFileSync(process.execPath, [probe], { encoding: 'utf8' }));
    assert.equal(out.badEncoded, true, '.pathname must stay percent-encoded');
    assert.equal(out.badExists, false, 'the encoded .env path must not exist');
    assert.equal(out.goodExists, true, 'the fileURLToPath .env path must exist');
  } finally {
    rmSync(base, { recursive: true, force: true });
  }
});

test(`${CONVERSATION_SERVER} loads .env via fileURLToPath, never new URL(import.meta.url).pathname (#2228)`, () => {
  const src = readFileSync(join(REPO_ROOT, CONVERSATION_SERVER), 'utf8');
  // Drop line comments so the explanatory comment naming the pattern can't self-trip.
  const code = src.split('\n').filter((l) => !l.trimStart().startsWith('//')).join('\n');
  // Narrow by design: only `.pathname` on a URL built from import.meta.url — never
  // the legitimate `url.pathname === '/route'` HTTP-routing uses elsewhere.
  const hit = /new URL\([^)]*import\.meta\.url[^)]*\)\.pathname/.exec(code);
  assert.equal(hit, null, hit ? `forbidden pattern present: ${hit[0]}` : '');
});
