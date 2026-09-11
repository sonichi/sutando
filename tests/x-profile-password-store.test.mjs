#!/usr/bin/env node
/**
 * Cookie encryption must not depend on the login Keychain.
 *
 * Measured 2026-09-10 on the 24/7 node: `security find-generic-password -s
 * "Chrome for Testing Safe Storage"` returns rc=44 from the core's context, so the
 * key the GUI login wrote is unreachable there. Chrome drops every cookie it cannot
 * decrypt on load, so each headless `check` silently signed the profile out and no
 * re-login survived. `--password-store=basic` keeps the key in the profile itself.
 *
 * The invariant is that BOTH launch sites carry it: the GUI `login` (LaunchServices)
 * and the Playwright `check`/`post`. If they disagree, one writes cookies the other
 * cannot read — the same failure with a different key.
 *
 * Run: node tests/x-profile-password-store.test.mjs
 */
import { readFileSync } from 'node:fs';

const SRC = new URL('../skills/x-twitter/x-post-browser.mjs', import.meta.url);
const src = readFileSync(SRC, 'utf8');
let fails = 0;
const check = (cond, msg) => {
  console.log((cond ? '  ok   ' : '  FAIL ') + msg);
  if (!cond) fails++;
};

check(/const PASSWORD_STORE_ARG = '--password-store=basic';/.test(src),
  'the flag is defined once, as a named constant');
check((src.match(/PASSWORD_STORE_ARG/g) || []).length === 3,
  `defined once and used at BOTH sites (found ${(src.match(/PASSWORD_STORE_ARG/g) || []).length} refs, want 3)`);
check(!/--password-store=basic/.test(src.replace(/const PASSWORD_STORE_ARG = '--password-store=basic';/, '')),
  'no second literal — the two sites cannot drift apart');

// Declared before BOTH uses: `const` has no hoisting, so a later declaration
// makes the GUI login throw ReferenceError instead of launching.
const decl = src.indexOf('const PASSWORD_STORE_ARG');
const uses = [...src.matchAll(/PASSWORD_STORE_ARG/g)].map((m) => m.index).filter((i) => i !== decl);
check(uses.every((i) => i > decl), 'declared before every use (temporal dead zone)');

// Site 1: the LaunchServices GUI login argv.
const loginBlock = src.slice(src.indexOf("execFileSync('open'"), src.indexOf("execFileSync('open'") + 400);
check(loginBlock.includes('PASSWORD_STORE_ARG'), 'GUI login argv carries the flag');

// Site 2: the Playwright persistent context.
const pwBlock = src.slice(src.indexOf('launchPersistentContext'), src.indexOf('launchPersistentContext') + 500);
check(pwBlock.includes('PASSWORD_STORE_ARG'), 'Playwright launch carries the flag');
check(pwBlock.includes("ignoreDefaultArgs: ['--use-mock-keychain']"),
  'the mock-keychain strip is still present (not replaced by this change)');

console.log(fails ? `\n${fails} FAILED` : '\nall ok');
process.exit(fails ? 1 : 0);
