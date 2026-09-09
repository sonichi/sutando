#!/usr/bin/env node
/**
 * A click is not a post. Two clicks on 2026-09-08 returned `posted: true` and neither
 * landed; the publish path printed success after a fixed wait with nothing reading the
 * page. The landing decision now lives in `skills/x-twitter/landing-check.mjs` as
 * `readLanding(page, opts)`, so it runs against a STUB page with no browser — and these
 * arms EXERCISE it (source-grep alone let `if (!landed)` → `if (false && !landed)` pass
 * all ten checks while removing the timeout branch). We assert the three real outcomes
 * through production logic, and the last arm applies that exact mutation to the source
 * and proves it breaks.
 *
 * Run: node tests/x-post-landing-check.test.mjs
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readLanding } from '../skills/x-twitter/landing-check.mjs';

let failures = 0;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  ok   ' : '  FAIL ') + name + (!cond && detail ? ` — ${detail}` : ''));
  if (!cond) failures++;
};

// A browserless page: waitForSelector yields a toast handle whose href is `toastHref`,
// or rejects (→ the production `.catch(() => null)`) when toastHref is null = no toast.
function stubPage(toastHref) {
  return {
    waitForSelector: async () => {
      if (toastHref === null) throw new Error('Timeout 15000ms exceeded');
      return { getAttribute: async () => toastHref };
    },
    screenshot: async () => {},
    $eval: async () => 'Something went wrong',
  };
}
const OPTS = { timeout: 5, shotDir: '/tmp/sutando-screenshots', now: () => 111 };

const main = async () => {
  // 1. A real post: the toast's /status/ link is accepted, url absolutised.
  const ok = await readLanding(stubPage('/Chi_Wang_/status/2097452592427335863'), OPTS);
  check('valid /handle/status/<id> → posted:true', ok.posted === true, JSON.stringify(ok));
  check('posted:true carries the absolutised url',
    ok.url === 'https://x.com/Chi_Wang_/status/2097452592427335863', ok.url);

  // 1b. An already-absolute href is passed through unchanged.
  const okAbs = await readLanding(stubPage('https://x.com/Chi_Wang_/status/2097452592427335863'), OPTS);
  check('absolute href passes through', okAbs.posted === true &&
    okAbs.url === 'https://x.com/Chi_Wang_/status/2097452592427335863', okAbs.url);

  // 2. The false positive that shipped: a stranger's analytics link is NOT a landing.
  const analytics = await readLanding(stubPage('/konstiwohlwend/status/2097235335034056835/analytics'), OPTS);
  check('analytics href → posted:false', analytics.posted === false, JSON.stringify(analytics));

  // 3. Timeout: no toast within the wait → loud failure, not a claimed post.
  const timedOut = await readLanding(stubPage(null), OPTS);
  check('no toast (timeout) → posted:false', timedOut.posted === false, JSON.stringify(timedOut));
  check('timeout decision is loud (reason + screenshot)',
    /no \/status\/ link/.test(timedOut.reason || '') && !!timedOut.screenshot, JSON.stringify(timedOut));
  check('timeout decision is NOT reported as a post', !('url' in timedOut));

  // 4. The reviewer's mutation, applied to source and executed. `if (!landed)` →
  //    `if (false && !landed)` removes the timeout branch; a timeout then reads the
  //    href off a null handle. Assert the mutant does NOT cleanly return posted:false.
  const srcUrl = new URL('../skills/x-twitter/landing-check.mjs', import.meta.url);
  const src = readFileSync(srcUrl, 'utf8');
  check('source still contains the mutated predicate `if (!landed)`',
    src.includes('if (!landed) {'), 'predicate not found — update the mutation arm');
  const mutated = src.replace('if (!landed) {', 'if (false && !landed) {');
  check('mutation actually changed the source', mutated !== src);
  const dir = mkdtempSync(join(tmpdir(), 'landing-mut-'));
  const mutPath = join(dir, 'landing-check.mjs');
  writeFileSync(mutPath, mutated);
  const { readLanding: mutRead } = await import(mutPath);
  let mutantBroke = false;
  try {
    const r = await mutRead(stubPage(null), OPTS);   // timeout case under the mutant
    if (r.posted !== false) mutantBroke = true;       // silently swallowed the timeout
  } catch {
    mutantBroke = true;                               // threw on null.getAttribute
  }
  check('mutation `if (false && !landed)` is caught (mutant mis-handles a timeout)',
    mutantBroke, 'the mutant still returned posted:false — the branch is not exercised');

  console.log(failures ? `\n${failures} FAILED` : '\nall ok');
  process.exit(failures ? 1 : 0);
};
main();
