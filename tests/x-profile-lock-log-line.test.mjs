// The CALLER's log line, not the unit. `waitForProfileExit` returns
// `exitedCleanly` and `remaining` that DISAGREE under an unknown probe; an
// earlier version of the log recomputed cleanliness from `remaining`, so an
// unreadable probe printed exited_cleanly=true.
//
// tests/x-profile-lock-wait.test.mjs pins the unit's disagreement, which is why
// reverting the caller's destructure left THAT suite 12/12 — the claim that the
// substitution cannot come back was about a line no test read. This reads it.
//
// Run: node tests/x-profile-lock-log-line.test.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const SRC = readFileSync(join(REPO, 'skills/x-twitter/x-post-browser.mjs'), 'utf8');
let fails = 0;
const ck = (n, c) => { console.log((c ? '  ok   ' : '  FAIL ') + n); if (!c) fails++; };

// Isolate the emitting statement rather than scanning the whole file, so an
// unrelated `remaining.length` elsewhere cannot satisfy or break this.
const m = SRC.match(/console\.error\(\s*`profile-lock:[\s\S]*?\);/);
ck('the profile-lock log statement exists', !!m);
const stmt = m ? m[0] : '';

ck('it prints the exitedCleanly the unit returned', /exited_cleanly=\$\{exitedCleanly\}/.test(stmt));
ck('it does NOT recompute cleanliness from remaining',
   !/exited_cleanly=\$\{[^}]*remaining[^}]*\}/.test(stmt));
ck('the caller actually destructures exitedCleanly',
   /const\s*\{[^}]*\bexitedCleanly\b[^}]*\}\s*=\s*waitForProfileExit/.test(SRC));

// The three observations a surviving session needs to mean anything.
ck('it names which pids were signalled', /SIGTERM->\[\$\{signalled/.test(stmt));
ck('it reports the wait duration', /waited_ms=\$\{waitedMs\}/.test(stmt));
ck('it reports what was force-killed', /sigkilled=\[\$\{remaining/.test(stmt));

console.log(fails === 0 ? '\nall ok' : `\n${fails} FAILED`);
process.exit(fails === 0 ? 0 : 1);
