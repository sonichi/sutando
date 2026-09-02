#!/usr/bin/env node
/**
 * The declared `config` block must actually be READ — qingyun blocker 2 on #2133.
 *
 * manifest.json gained `config` for the three settings this skill reads, but nothing
 * consulted it: every "manifest.json" in the skill was a COMMENT, and each call site
 * was still `process.env.X || '<literal>'`. A declaration with no reader is inert —
 * an operator editing a declared value got silence — and it was invisible because the
 * declared values equal the hardcoded literals. These pin the rung that was missing.
 *
 * The last two checks are the load-bearing ones: they re-derive the key list FROM the
 * shipped script and the shipped manifest, so a setting added to either side without
 * the other fails HERE rather than becoming the next undeclared env var.
 *
 * Imports the PRODUCTION resolver with injected deps — no real filesystem, no browser.
 *
 * Run: node tests/x-post-manifest-config.test.mjs
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readManifestConfig, resolveSetting } from '../skills/x-twitter/manifest-config.mjs';

let failures = 0;
const check = (name, cond, detail = '') => {
	if (cond) { console.log(`  ok   ${name}`); return; }
	console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`);
	failures++;
};

const SKILL = join(dirname(fileURLToPath(import.meta.url)), '..', 'skills', 'x-twitter');
const CONFIG = { X_LOGIN_DONE_SENTINEL: '/from-manifest', X_BROWSER_PROFILE: '' };

// ── precedence ───────────────────────────────────────────────────────────────
let r = resolveSetting('X_LOGIN_DONE_SENTINEL', {
	env: { X_LOGIN_DONE_SENTINEL: '/from-env' }, config: CONFIG, fallback: '/builtin',
});
check('an env override beats the manifest', r.value === '/from-env' && r.source === 'env', r.value);

r = resolveSetting('X_LOGIN_DONE_SENTINEL', { env: {}, config: CONFIG, fallback: '/builtin' });
check('THE MISSING RUNG: the manifest beats the built-in default',
	r.value === '/from-manifest' && r.source === 'manifest', `${r.value} (${r.source})`);

r = resolveSetting('X_LOGIN_TIMEOUT_ITERS', { env: {}, config: CONFIG, fallback: '120' });
check('the built-in default applies when neither is set',
	r.value === '120' && r.source === 'default', r.value);

// ── empty means unset, at every rung ─────────────────────────────────────────
r = resolveSetting('X_LOGIN_DONE_SENTINEL', {
	env: { X_LOGIN_DONE_SENTINEL: '' }, config: CONFIG, fallback: '/builtin',
});
check('an EMPTY env value is treated as unset, not as ""',
	r.value === '/from-manifest' && r.source === 'manifest', r.value);

r = resolveSetting('X_BROWSER_PROFILE', { env: {}, config: CONFIG, fallback: '/derived' });
check('an EMPTY manifest value falls through to the derived path',
	r.value === '/derived' && r.source === 'default', r.value);
// X_BROWSER_PROFILE ships as "" exactly so this happens: resolving it to "" would
// hand profile-dir.mjs a truthy-empty override and put the Chrome profile at the
// filesystem root instead of under the workspace.

// ── a config file must never be able to break posting ────────────────────────
check('a MISSING manifest yields {} rather than throwing',
	Object.keys(readManifestConfig({
		manifestPath: '/nonexistent/manifest.json', readFile: readFileSync,
	})).length === 0);

check('a MALFORMED manifest yields {} rather than throwing',
	Object.keys(readManifestConfig({
		manifestPath: 'ignored', readFile: () => '{ this is not json',
	})).length === 0);

check('a manifest with no config block yields {}',
	Object.keys(readManifestConfig({
		manifestPath: 'ignored', readFile: () => '{"name":"x-twitter"}',
	})).length === 0);

check('a non-object config yields {} rather than a string index',
	Object.keys(readManifestConfig({
		manifestPath: 'ignored', readFile: () => '{"config":"nope"}',
	})).length === 0);

// ── the real manifest, from the real location ────────────────────────────────
const real = readManifestConfig({
	manifestPath: join(SKILL, 'manifest.json'), readFile: readFileSync,
});
check('the shipped manifest is readable from where the script looks',
	Object.keys(real).length > 0, JSON.stringify(real));

// ── drift guards: derive the key list from the shipped artifacts ─────────────
const script = readFileSync(join(SKILL, 'x-post-browser.mjs'), 'utf8');
const used = [...new Set([...script.matchAll(/setting\(\s*'([A-Z0-9_]+)'/g)].map((m) => m[1]))];
check('the script resolves its settings through the contract', used.length > 0, `${used}`);

const undeclared = used.filter((k) => !(k in real));
check('every setting the script READS is declared in the manifest',
	undeclared.length === 0, `undeclared: ${undeclared}`);

const unread = Object.keys(real).filter((k) => !used.includes(k));
check('every setting the manifest DECLARES is read by the script',
	unread.length === 0, `declared but inert: ${unread}`);
// ^ this is the exact defect above: a declared key nothing consults.

const bare = [...script.matchAll(/process\.env\.(X_[A-Z0-9_]+)/g)].map((m) => m[1]);
check('no bare process.env read bypasses the contract', bare.length === 0, `${bare}`);

const SECRETS = ['X_API_KEY', 'X_API_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_TOKEN_SECRET', 'X_BEARER_TOKEN'];
check('no OAuth1 secret is declared in the committed manifest',
	SECRETS.every((s) => !(s in real)));

console.log(failures ? `\n${failures} failure(s)` : '\nall passed');
process.exit(failures ? 1 : 0);
