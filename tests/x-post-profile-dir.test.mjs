#!/usr/bin/env node
/**
 * Profile-dir precedence — qingyun P1 #2 on #2133 (config/path contract).
 *
 * The durable profile used to default to an inline `homedir()/.sutando/...` literal
 * instead of the canonical workspace helper, and none of the three env knobs were
 * declared anywhere. This pins the resolution order that replaced it, and — the part
 * that matters operationally — that upgrading does NOT orphan an existing X login.
 *
 * Imports the PRODUCTION resolver; the deps are injected, so no real filesystem or
 * workspace is involved. A test that re-implemented the precedence could stay green
 * while the shipped path drifted (#1414).
 *
 * Run: node tests/x-post-profile-dir.test.mjs
 */
import { resolveProfileDir } from '../skills/x-twitter/profile-dir.mjs';

let failures = 0;
const check = (name, cond, detail = '') => {
	if (cond) { console.log(`  ok   ${name}`); return; }
	console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`);
	failures++;
};
const LEGACY = '/Users/someone/.sutando/x-browser-profile';
const WS = '/repo/workspace';
const CANON = `${WS}/data/x-browser-profile`;
const none = () => false;
const all = () => true;

// 1. explicit override wins over everything
let r = resolveProfileDir({ env: '/tmp/explicit', workspace: WS, legacyDir: LEGACY, exists: all });
check('X_BROWSER_PROFILE wins over both derived paths', r.dir === '/tmp/explicit' && r.source === 'env', r.dir);
check('an explicit override emits no notice', !r.notice);

// 2. clean host -> canonical workspace path
r = resolveProfileDir({ env: '', workspace: WS, legacyDir: LEGACY, exists: none });
check('fresh host derives <workspace>/data/x-browser-profile', r.dir === CANON && r.source === 'workspace', r.dir);
check('no legacy notice when there is no legacy profile', !r.notice);

// 3. THE MIGRATION CASE: a live login must not be orphaned
r = resolveProfileDir({ env: '', workspace: WS, legacyDir: LEGACY, exists: (p) => p === LEGACY });
check('an existing pre-#2133 profile is still used', r.dir === LEGACY && r.source === 'legacy', r.dir);
check('...and says so, naming both paths', !!r.notice && r.notice.includes(LEGACY) && r.notice.includes(CANON), r.notice);

// 4. once migrated, the canonical path wins even though the legacy one lingers
r = resolveProfileDir({ env: '', workspace: WS, legacyDir: LEGACY, exists: all });
check('canonical wins once it exists, legacy left alone', r.dir === CANON && r.source === 'workspace', r.dir);
check('no notice after migration', !r.notice);

// 5. unresolvable workspace degrades loudly, never silently
r = resolveProfileDir({ env: '', workspace: '', legacyDir: LEGACY, exists: none });
check('unresolved workspace is reported, not swallowed', r.source === 'unresolved' && !!r.notice, JSON.stringify(r));
check('unresolved notice names the escape hatch', r.notice.includes('X_BROWSER_PROFILE'), r.notice);

console.log(failures ? `\nFAIL — ${failures} profile-dir check(s)` : '\nPASS — x-post profile dir');
process.exit(failures ? 1 : 0);
