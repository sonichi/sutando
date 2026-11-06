#!/usr/bin/env node
/**
 * isClearing — qingyun-wu CR on #4239 (pullrequestreview-5192080742): the
 * reason field was wrong in two of the three clearing mechanisms. A quoted
 * empty value and a bare Max-Age=0 were both reported as 'past-expiry'
 * because the reason was re-derived from the raw value after the fact
 * instead of being set by whichever rule actually fired.
 *
 * isClearing is pure and exported, so this needs no browser and no worktree.
 *
 * Run: node tests/x-cookie-forensics-is-clearing.test.mjs
 */
import { isClearing } from '../skills/x-twitter/cookie-forensics.mjs';

let failures = 0;
const check = (name, cond, detail = '') => {
	if (cond) { console.log(`  ok   ${name}`); return; }
	console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`);
	failures++;
};

const cases = [
	['auth_token=; Path=/; Domain=.x.com', { name: 'auth_token', reason: 'empty-value' }],
	['auth_token=""; Path=/', { name: 'auth_token', reason: 'empty-value' }],
	['auth_token=abc; Max-Age=0', { name: 'auth_token', reason: 'max-age-0' }],
	['auth_token=abc; Expires=Thu, 01 Jan 1970 00:00:00 GMT', { name: 'auth_token', reason: 'past-expiry' }],
	['ct0=abc; Max-Age=3600', null],
	['guest_id=abc;', null],
];

for (const [line, expected] of cases) {
	const got = isClearing(line);
	if (expected === null) {
		check(`not clearing: ${line}`, got === null, JSON.stringify(got));
		continue;
	}
	check(
		`${line} -> ${expected.reason}`,
		got && got.name === expected.name && got.reason === expected.reason,
		JSON.stringify(got),
	);
}

console.log(failures ? `\nFAIL — ${failures} isClearing check(s)` : '\nPASS — x-cookie-forensics isClearing');
process.exit(failures ? 1 : 0);
