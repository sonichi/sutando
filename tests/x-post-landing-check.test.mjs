#!/usr/bin/env node
/**
 * A click is not a post. Two clicks on 2026-09-08 returned `posted: true` and neither
 * landed on the timeline; the publish path printed success after a fixed 3s wait with
 * nothing reading the page. These arms pin the shape of the publish path at source
 * level (no browser): success requires an observed /status/ link, failure is loud, and
 * the old `verified: true` — which described the PRE-click composer readback — is gone
 * from the posted line, because a reader took it as landing evidence.
 *
 * Run: node tests/x-post-landing-check.test.mjs
 */
import { readFileSync } from 'node:fs';
const SRC = readFileSync(new URL('../skills/x-twitter/x-post-browser.mjs', import.meta.url), 'utf8');

let failures = 0;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  ok   ' : '  FAIL ') + name + (!cond && detail ? ` — ${detail}` : ''));
  if (!cond) failures++;
};

const click = SRC.indexOf('await btn.click();');
const after = SRC.slice(click);
check('publish path exists', click >= 0);
check('after the click, the page is read for a /status/ link',
  /waitForSelector\([^)]*\/status\//s.test(after), 'no /status/ wait follows btn.click()');
check('posted: true is NOT printed unconditionally after a fixed wait',
  !/waitForTimeout\(\d+\);\s*\n\s*console\.log\(JSON\.stringify\(\{ posted: true/.test(after),
  'the old click; wait; posted:true sequence is still there');
check('a non-landing exits non-zero with a screenshot',
  /posted: false[\s\S]*screenshot[\s\S]*process\.exit\(4\)/.test(after) || /process\.exit\(4\)[\s\S]*posted: false/.test(after),
  'no loud failure branch (exit 4, distinct from the pre-click exit 3)');
check('the landing selector is TOAST-scoped only (a bare /status/ link matched a stranger\'s analytics link)',
  /waitForSelector\('\[data-testid="toast"\] a\[href\*="\/status\/"\]'/.test(after) && !/, a\[href\*="\/status\/"\]\[role="link"\]/.test(after),
  'selector is not toast-only');
// Behaviour, not spelling: pull the href guard's regex literal out of the source
// and run it, so reformatting the pattern cannot silently disarm this arm.
const guard = after.match(/if\s*\(!(\/.*\/)\.test\(h\)\)/);
check('the href guard is a real regex in source', !!guard, 'no /.../.test(h) href guard found after the click');
const hrefRe = guard ? eval(guard[1]) : null;
check('a plain /<handle>/status/<id> href is accepted',
  !!hrefRe && hrefRe.test('/Chi_Wang_/status/2097452592427335863') && hrefRe.test('https://x.com/Chi_Wang_/status/2097452592427335863'),
  'the guard rejects a legitimate post-toast link');
check('an /analytics href is rejected (the false positive that shipped)',
  !!hrefRe && !hrefRe.test('/konstiwohlwend/status/2097235335034056835/analytics'),
  'the guard accepts a stranger analytics link');
check('the posted line carries a url', /posted: true, url/.test(after), 'success has no url field');
check('`verified: true` no longer rides the posted line', !/posted: true[^\n]*verified: true/.test(after),
  'verified still reads as landing evidence');

console.log(failures ? `\n${failures} FAILED` : '\nall ok');
process.exit(failures ? 1 : 0);
