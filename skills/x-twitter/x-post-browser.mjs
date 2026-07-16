#!/usr/bin/env node
/**
 * Sutando X poster — browser path (no developer portal, no API keys).
 *
 * Posts through x.com's web UI using a PERSISTENT Chrome profile, so the
 * owner's one-time cost is a single sign-in — everything after that runs
 * headless with the saved session. This is the zero-dev-portal alternative
 * to the OAuth1 API path in x-post.py.
 *
 * Profile dir (persists the login) resolves from $X_BROWSER_PROFILE, else
 * ~/.sutando/x-browser-profile. It is per-host and should NOT be synced.
 *
 * === Keychain consistency (the load-bearing invariant) ===
 * All three commands MUST encrypt/decrypt cookies with the SAME key or the
 * saved session is silently destroyed. macOS Chrome encrypts cookie values
 * (v10) with a key from the login Keychain ("Chrome for Testing Safe Storage").
 * Playwright launches Chromium-for-Testing with `--use-mock-keychain` by
 * DEFAULT, which swaps in a throwaway mock key. Cookies written under the real
 * keychain are then undecryptable under the mock key (and vice-versa), and
 * Chrome DROPS every cookie it can't decrypt on load — wiping the sign-in.
 * (Verified 2026-07-14: a GUI login wrote 9 v10 cookies; a default Playwright
 * `check` opened the profile and left 0 rows.)
 * So:
 *   - login  → `open` (LaunchServices GUI) → REAL keychain, findable window.
 *   - check/post → Playwright with ignoreDefaultArgs:['--use-mock-keychain']
 *                  → REAL keychain → can decrypt what login wrote.
 * Never launch this profile with the mock keychain, ever.
 *
 * Usage:
 *   node x-post-browser.mjs login          # headed — owner signs in once
 *   node x-post-browser.mjs check          # probe: is the profile signed in?
 *   node x-post-browser.mjs post "<text>"  # compose + publish a tweet
 *   node x-post-browser.mjs post "<text>" --dry-run   # stop before publish
 *
 * Exit codes: 0 ok, 2 not-signed-in, 1 error.
 */

import { chromium } from 'playwright';
import { mkdirSync, existsSync, readdirSync, rmSync, copyFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { execSync, execFileSync } from 'node:child_process';

/** The `playwright` npm package pins one Chromium revision, but the installed
 *  build can drift (e.g. package wants chromium-1208, cache has chromium-1228).
 *  Resolve the newest installed "Google Chrome for Testing" .app and return both
 *  the bundle dir (for `open`) and the inner executable (for Playwright). */
function resolveChromium() {
  const cache = join(homedir(), 'Library', 'Caches', 'ms-playwright');
  if (!existsSync(cache)) return {};
  const builds = readdirSync(cache)
    .filter((d) => /^chromium-\d+$/.test(d))
    .sort((a, b) => parseInt(b.split('-')[1], 10) - parseInt(a.split('-')[1], 10));
  for (const b of builds) {
    const app = join(cache, b, 'chrome-mac-arm64', 'Google Chrome for Testing.app');
    const bin = join(app, 'Contents', 'MacOS', 'Google Chrome for Testing');
    if (existsSync(bin)) return { app, bin };
  }
  return {};
}

const cmd = process.argv[2];
const arg = process.argv[3];
const dryRun = process.argv.includes('--dry-run');

const PROFILE_DIR =
  process.env.X_BROWSER_PROFILE || join(homedir(), '.sutando', 'x-browser-profile');
const SHOT_DIR = '/tmp/sutando-screenshots';
mkdirSync(PROFILE_DIR, { recursive: true });
mkdirSync(SHOT_DIR, { recursive: true });

if (!cmd || !['login', 'check', 'post'].includes(cmd)) {
  console.error('Usage: node x-post-browser.mjs <login|check|post> [text] [--dry-run]');
  process.exit(1);
}
if (cmd === 'post' && !arg) {
  console.error('post requires tweet text');
  process.exit(1);
}

const { app: CHROME_APP, bin: CHROME_BIN } = resolveChromium();

/** Kill any GCfT holding THIS profile and clear the SingletonLock, so the next
 *  launch (open or Playwright) doesn't collide on the single-instance lock. */
function releaseProfileLock() {
  try {
    const out = execSync(
      `pgrep -fl "Google Chrome for Testing" | grep -F -- "--user-data-dir=${PROFILE_DIR}" | grep -v -- "--type=" | awk '{print $1}'`,
      { encoding: 'utf8', shell: '/bin/bash' }
    ).trim();
    for (const pid of out.split('\n').filter(Boolean)) {
      try { process.kill(parseInt(pid, 10), 'SIGTERM'); } catch {}
    }
  } catch {}
  try { execSync('sleep 1'); } catch {}
  try {
    const out = execSync(
      `pgrep -fl "Google Chrome for Testing" | grep -F -- "--user-data-dir=${PROFILE_DIR}" | grep -v -- "--type=" | awk '{print $1}'`,
      { encoding: 'utf8', shell: '/bin/bash' }
    ).trim();
    for (const pid of out.split('\n').filter(Boolean)) {
      try { process.kill(parseInt(pid, 10), 'SIGKILL'); } catch {}
    }
  } catch {}
  try { rmSync(join(PROFILE_DIR, 'SingletonLock'), { force: true }); } catch {}
  try { rmSync(join(PROFILE_DIR, 'SingletonCookie'), { force: true }); } catch {}
  try { rmSync(join(PROFILE_DIR, 'SingletonSocket'), { force: true }); } catch {}
}

/** Read the on-disk cookie DB READ-ONLY (copy first to dodge the WAL/lock) and
 *  count the signed-in markers. Non-disruptive — never opens the profile, so it
 *  can run WHILE the login window is up. Cookie NAMES are cleartext even though
 *  values are keychain-encrypted, so this needs no decryption. */
function authMarkersOnDisk() {
  const src = join(PROFILE_DIR, 'Default', 'Cookies');
  if (!existsSync(src)) return 0;
  const tmp = join(tmpdir(), `x-cookies-peek-${process.pid}.db`);
  try {
    copyFileSync(src, tmp);
    const n = execFileSync('sqlite3', [
      tmp,
      "SELECT COUNT(*) FROM cookies WHERE name IN ('auth_token','ct0','twid') AND (host_key='.x.com' OR host_key='x.com');",
    ], { encoding: 'utf8' }).trim();
    return parseInt(n, 10) || 0;
  } catch {
    return 0;
  } finally {
    try { rmSync(tmp, { force: true }); } catch {}
  }
}

// ─── login: GUI launch via LaunchServices (REAL keychain, findable window) ───
if (cmd === 'login') {
  if (!CHROME_APP) {
    console.error('Could not find a "Google Chrome for Testing.app" in the Playwright cache.');
    process.exit(1);
  }
  releaseProfileLock();
  // `open -n -a <full .app path>` launches the SAME binary Playwright uses, but
  // via LaunchServices: a real GUI app with a Dock icon / Cmd+Tab entry / real
  // keychain access — none of which a Playwright raw-exec window gets. Passing
  // the explicit --user-data-dir avoids the orphan-on-default-profile bug.
  execFileSync('open', [
    '-n', '-a', CHROME_APP, '--args',
    `--user-data-dir=${PROFILE_DIR}`,
    '--no-first-run', '--no-default-browser-check',
    'https://x.com/login',
  ]);
  console.error(
    'A "Google Chrome for Testing" window is opening (Cmd+Tab to it — it has its own Dock icon). ' +
    'Sign in to X (Google/Apple/email all work — no automation flag). ' +
    'I\'ll detect completion automatically; you can leave the window and I\'ll close it.'
  );

  const SENTINEL = process.env.X_LOGIN_DONE_SENTINEL || '/tmp/x-login-done';
  try { rmSync(SENTINEL, { force: true }); } catch {}
  const iters = parseInt(process.env.X_LOGIN_TIMEOUT_ITERS || '120', 10) || 120; // ~10min
  for (let i = 0; i < iters; i++) {
    execSync('sleep 5');
    if (authMarkersOnDisk() >= 1 || existsSync(SENTINEL)) {
      // Cookies are already flushed to disk (that's what we detected). Close the
      // GUI window so `check`/`post` can open the profile without a lock clash.
      // Killing after the on-disk flush is safe — the real-keychain cookies
      // survive a subsequent Playwright open (mock keychain stripped there).
      releaseProfileLock();
      console.log(JSON.stringify({ signedIn: true, profile: PROFILE_DIR }));
      process.exit(0);
    }
  }
  console.error('timed out waiting for sign-in');
  releaseProfileLock();
  process.exit(2);
}

// ─── check / post: headless Playwright, REAL keychain (mock stripped) ───
releaseProfileLock();
const ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
  headless: true,
  ...(CHROME_BIN ? { executablePath: CHROME_BIN } : {}),
  viewport: { width: 1280, height: 900 },
  // CRITICAL: strip --use-mock-keychain so cookie values are decrypted with the
  // SAME real login-keychain key the GUI login used. With the mock key, Chrome
  // can't decrypt the saved session and drops every cookie → silent sign-out.
  ignoreDefaultArgs: ['--use-mock-keychain'],
});

/** Signed-in iff the home compose box exists (not redirected to /login). */
async function isSignedIn(page) {
  await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(2500);
  if (/\/(login|i\/flow\/login)/.test(page.url())) return false;
  const box = await page.$('[data-testid="tweetTextarea_0"], [data-testid="SideNav_NewTweet_Button"]');
  return !!box;
}

try {
  const page = ctx.pages()[0] || (await ctx.newPage());

  if (cmd === 'check') {
    const ok = await isSignedIn(page);
    const shot = `${SHOT_DIR}/x-check-${Date.now()}.png`;
    await page.screenshot({ path: shot });
    console.log(JSON.stringify({ signedIn: ok, profile: PROFILE_DIR, screenshot: shot }));
    process.exit(ok ? 0 : 2);
  }

  if (cmd === 'post') {
    if (!(await isSignedIn(page))) {
      console.error('not signed in — run: node x-post-browser.mjs login');
      process.exit(2);
    }
    const box = await page.waitForSelector('[data-testid="tweetTextarea_0"]', { timeout: 15000 });
    await box.click();
    await page.keyboard.type(arg, { delay: 15 });
    await page.waitForTimeout(800);
    if (dryRun) {
      const shot = `${SHOT_DIR}/x-dryrun-${Date.now()}.png`;
      await page.screenshot({ path: shot });
      console.log(JSON.stringify({ dryRun: true, wouldPost: arg, screenshot: shot }));
      process.exit(0);
    }
    // Publish: inline compose button (tweetButtonInline) or modal (tweetButton).
    const btn = await page.waitForSelector(
      '[data-testid="tweetButtonInline"]:not([aria-disabled="true"]), [data-testid="tweetButton"]:not([aria-disabled="true"])',
      { timeout: 15000 }
    );
    await btn.click();
    await page.waitForTimeout(3000);
    console.log(JSON.stringify({ posted: true, text: arg }));
    process.exit(0);
  }
} catch (err) {
  console.error(`Error: ${err.message}`);
  process.exit(1);
} finally {
  await ctx.close();
}
