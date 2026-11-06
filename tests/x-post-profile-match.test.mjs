#!/usr/bin/env node
/**
 * Profile-match guard — qingyun P1 on #2133.
 *
 * WHY THIS EXISTS: `releaseProfileLock()` SIGTERM/SIGKILLs every PID the profile
 * predicate returns. That predicate was `line.includes('--user-data-dir=' + PROFILE_DIR)`,
 * a substring test that ignores argument boundaries — so with `PROFILE_DIR=/tmp/x-profile`
 * a browser running `--user-data-dir=/tmp/x-profile-copy` matched and got killed. qingyun
 * reproduced it on the exact head. The blast radius is somebody's live browser session.
 *
 * These import the PRODUCTION predicate from profile-match.mjs — the same module
 * x-post-browser.mjs imports. They deliberately do NOT re-implement the match: a test that
 * mirrors the logic it checks can pass while the shipped path regresses (#1414).
 *
 * Run: node tests/x-post-profile-match.test.mjs
 */
import { execFileSync } from 'node:child_process';
import {
	lineHoldsProfile,
	pidsHoldingProfile,
	pidsFromLsofFields,
	gcftPids,
	classifyLsofProbe,
	execTimedOut,
} from '../skills/x-twitter/profile-match.mjs';

let failures = 0;
const check = (name, cond, detail = '') => {
	if (cond) { console.log(`  ok   ${name}`); return; }
	console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`);
	failures++;
};

const DIR = '/tmp/x-profile';
const line = (path, extra = '') =>
	`4242 /Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing --user-data-dir=${path}${extra}`;

// --- the reported bug -------------------------------------------------------
check(
	'P1: a PREFIX-COLLIDING profile is NOT matched (would have been killed)',
	lineHoldsProfile(line('/tmp/x-profile-copy'), DIR) === false,
	'substring matching killed an unrelated browser',
);
check(
	'P1: prefix collision with trailing args is still not matched',
	lineHoldsProfile(line('/tmp/x-profile-copy', ' --no-first-run'), DIR) === false,
);
check(
	'a deeper path under ours is a DIFFERENT profile, not ours',
	lineHoldsProfile(line('/tmp/x-profile/nested'), DIR) === false,
);
check(
	'a path we are a suffix of is not ours either',
	lineHoldsProfile(line('/home/other/tmp/x-profile'), DIR) === false,
);
// pgrep -fl flattens argv, so "<dir> copy" is equally one path with a space or two
// arguments. Undecidable -> fail closed, per this module's stated failure direction.
check(
	'P1: a SPACE-SUFFIX profile is NOT matched (would have been killed)',
	lineHoldsProfile(line('/tmp/x-profile copy'), DIR) === false,
	'"/tmp/x-profile copy" was read as our dir plus an argument',
);
check(
	'P1: space-suffix followed by a real flag is still not ours',
	lineHoldsProfile(line('/tmp/x-profile copy', ' --no-first-run'), DIR) === false,
);

// --- must still match, or the lock cleanup silently stops working -----------
check('exact match at end of line', lineHoldsProfile(line(DIR), DIR) === true);
// These three USED to assert true, on the rule that a following `--flag` proves the
// path ended. qingyun disproved it: `--user-data-dir=/p --copy` is equally the single
// path `/p --copy`, so the rule handed an unrelated browser to SIGKILL. Flattened argv
// cannot decide, so this predicate now answers only on an exact value.
check('a following flag is NOT an argv boundary', lineHoldsProfile(line(DIR, ' --no-first-run'), DIR) === false);
check(
	'undecidable when the flag is not the last argument',
	lineHoldsProfile(`4242 chrome --enable-x --user-data-dir=${DIR} --remote-debugging-port=0`, DIR) === false,
);
check(
	"qingyun's exact case: `<dir> --copy` is not claimed",
	lineHoldsProfile(line(DIR, ' --copy'), DIR) === false,
	'a profile literally named "<dir> --copy" would have been killed',
);

// --- helper processes inherit the flag; killing them is wrong ---------------
check(
	'renderer/GPU helpers are excluded',
	lineHoldsProfile(line(DIR, ' --type=renderer'), DIR) === false,
);

// --- degenerate inputs must not throw or match ------------------------------
for (const [name, l, d] of [
	['empty line', '', DIR],
	['null line', null, DIR],
	['empty profile dir', line(DIR), ''],
	['no --user-data-dir at all', '4242 chrome --headless', DIR],
]) {
	check(`degenerate: ${name} does not match`, lineHoldsProfile(l, d) === false);
}

// --- a space then a BARE token is NOT an argument boundary -------------------
// `-copy` is a bare token, not a flag: "/tmp/x-profile -copy" is one path that
// happens to contain a space. Matching it sends SIGKILL to an unrelated browser.
for (const [name, l] of [
	['space then -copy', line(DIR).replace(DIR, `${DIR} -copy`)],
	['space then -copy/Default/x', line(DIR).replace(DIR, `${DIR} -copy/Default/x`)],
]) {
	check(`bare token after space does not match: ${name}`, lineHoldsProfile(l, DIR) === false);
}
check('a real flag after a space is undecidable, so not claimed',
	lineHoldsProfile(line(DIR).replace(DIR, `${DIR} --foo`), DIR) === false);
check('no pid is offered for the disputed line',
	JSON.stringify(pidsHoldingProfile(line(DIR).replace(DIR, `${DIR} -copy`), DIR)) === '[]');

// --- pid extraction ---------------------------------------------------------
const out = [
	line(DIR),                                   // ours            -> 4242
	line('/tmp/x-profile-copy').replace('4242', '5150'),  // NOT ours
	line(DIR, ' --type=gpu-process').replace('4242', '6161'), // helper, skip
	`7777 /Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing --user-data-dir=${DIR} --x`,
].join('\n');
const pids = pidsHoldingProfile(out, DIR);
// 7777's row ends `--user-data-dir=<DIR> --x` — undecidable, so it is no longer offered.
check('pidsHoldingProfile returns only EXACT-match browser PIDs', JSON.stringify(pids) === JSON.stringify(['4242']), JSON.stringify(pids));
check('pidsHoldingProfile tolerates empty input', JSON.stringify(pidsHoldingProfile('', DIR)) === '[]');
check('pidsHoldingProfile tolerates null input', JSON.stringify(pidsHoldingProfile(null, DIR)) === '[]');

// --- the production decider: open descriptors, not argv ---------------------
// `lsof -F pn +D <dir>` — `+D` scopes the search, so the kernel answers "whose
// profile is this" and nothing is parsed out of a path. Verified live before this
// was written: a process holding "<box>/prof --copy/held.txt" returns 0 rows for
// "<box>/prof", while the same query against its own dir returns it (positive control).
const FIELDS = ['p4242', 'n/tmp/x-profile/Default/Cookies', 'n/tmp/x-profile/lockfile',
	'p7777', 'n/tmp/x-profile/Default/History'].join('\n');
check('pidsFromLsofFields collects each process block once',
	JSON.stringify(pidsFromLsofFields(FIELDS)) === JSON.stringify(['4242', '7777']),
	JSON.stringify(pidsFromLsofFields(FIELDS)));
check('a p-block with no open file is not a holder',
	JSON.stringify(pidsFromLsofFields('p4242\np7777\nn/tmp/x-profile/x')) === JSON.stringify(['7777']));
check('a non-numeric p field is ignored',
	JSON.stringify(pidsFromLsofFields('pnotapid\nn/tmp/x-profile/x')) === JSON.stringify([]));
for (const [name, v] of [['empty', ''], ['null', null], ['undefined', undefined]]) {
	check(`pidsFromLsofFields degenerate: ${name}`, JSON.stringify(pidsFromLsofFields(v)) === '[]');
}

// --- gcftPids answers "is this GCfT", never "whose profile" ------------------
const PG = [line(DIR), line('/somewhere/else').replace('4242', '5150'),
	line(DIR, ' --type=renderer').replace('4242', '6161')].join('\n');
check('gcftPids returns every GCfT pid regardless of its profile',
	JSON.stringify(gcftPids(PG)) === JSON.stringify(['4242', '5150']), JSON.stringify(gcftPids(PG)));
check('gcftPids excludes renderer/GPU helpers', !gcftPids(PG).includes('6161'));
for (const [name, v] of [['empty', ''], ['null', null]]) {
	check(`gcftPids degenerate: ${name}`, JSON.stringify(gcftPids(v)) === '[]');
}

// The production intersection: hold a file in OUR dir AND be a GCfT browser.
const holders = pidsFromLsofFields(FIELDS);           // 4242, 7777
const gcft = new Set(gcftPids(PG));                   // 4242, 5150
check('intersection kills only a GCfT process holding our profile',
	JSON.stringify(holders.filter((x) => gcft.has(x))) === JSON.stringify(['4242']));

// --- a REAL execFileSync timeout, not a hand-written fixture ------------------
// The bug was a fixture-shaped belief: `e.killed` is documented on the ASYNC exec
// error, so a fixture that sets it passes while the shipped sync path never does.
let timeoutErr = null;
try {
	execFileSync('sleep', ['5'], { timeout: 250 });
} catch (e) { timeoutErr = e; }
check('control: a real execFileSync timeout was captured', timeoutErr !== null);
check('control: the real timeout error has NO own `killed` property — this is the defect',
	timeoutErr !== null && !Object.prototype.hasOwnProperty.call(timeoutErr, 'killed'),
	timeoutErr && `killed=${JSON.stringify(timeoutErr.killed)}`);
check('the old predicate `!!e.killed` reads FALSE on it',
	timeoutErr !== null && !!timeoutErr.killed === false);
check('execTimedOut reads TRUE on it',
	timeoutErr !== null && execTimedOut(timeoutErr) === true,
	timeoutErr && `code=${timeoutErr.code} signal=${timeoutErr.signal}`);

// A timed-out lsof that printed PART of the holder list is the dangerous shape:
// stdout is non-empty, so without the timeout flag it classifies as a complete read.
const PARTIAL = 'p4242\nn/tmp/x-profile/Singleton';
check('old path: partial stdout from a timeout classifies as KNOWN (unsafe)',
	classifyLsofProbe({ threw: true, killed: !!(timeoutErr && timeoutErr.killed), stdout: PARTIAL }).known === true);
check('fixed path: the same probe classifies as UNKNOWN, so the caller fails closed',
	classifyLsofProbe({ threw: true, killed: execTimedOut(timeoutErr), stdout: PARTIAL }).known === false);

for (const [name, v] of [['null', null], ['undefined', undefined], ['string', 'boom']]) {
	check(`execTimedOut degenerate: ${name}`, execTimedOut(v) === false);
}
check('execTimedOut is false for an ordinary non-zero exit', execTimedOut({ status: 1, code: 1 }) === false);

// A probe called with nothing measured nothing: the tri-state must say so.
for (const [name, v] of [['no arguments', undefined], ['null', null], ['a string', 'oops']]) {
	check(`classifyLsofProbe fails closed: ${name}`, classifyLsofProbe(v).known === false,
		JSON.stringify(classifyLsofProbe(v)));
}
check('a clean empty probe is still a CONFIRMED zero, not unknown',
	classifyLsofProbe({ threw: false, killed: false, stdout: '' }).known === true);

console.log(failures ? `\nFAIL — ${failures} profile-match check(s)` : '\nPASS — x-post profile match');
process.exit(failures ? 1 : 0);
