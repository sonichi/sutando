#!/usr/bin/env node
/**
 * Sutando X poster — browser path (no developer portal, no API keys).
 *
 * Posts through x.com's web UI using a PERSISTENT Chrome profile, so the
 * owner's one-time cost is a single sign-in — everything after that runs
 * headless with the saved session. This is the zero-dev-portal alternative
 * to the OAuth1 API path in x-post.py.
 *
 * Profile dir (persists the login) resolves from $X_BROWSER_PROFILE (declared
 * in this skill's manifest.json), else `<workspace>/data/x-browser-profile` via
 * scripts/sutando-config.sh. It is per-host, holds live session cookies, and is
 * excluded from vault sync by the workspace contract's `data/` exclusion.
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
 * So, by default:
 *   - login  → `open` (LaunchServices GUI) → REAL keychain, findable window.
 *   - check/post → Playwright with ignoreDefaultArgs:['--use-mock-keychain']
 *                  → REAL keychain → can decrypt what login wrote.
 * What must never happen is a MIX. $X_BROWSER_MOCK_KEYCHAIN=1 moves BOTH sides
 * to the mock key, for a host whose login-keychain password is unknown or out of
 * sync with the account password — there the real-keychain prompt cannot be
 * answered and sign-in is otherwise impossible. The cookie store is then
 * encrypted with a well-known key, so the profile dir alone grants the session;
 * it is per-host and excluded from sync.
 *
 * Usage:
 *   node x-post-browser.mjs login          # headed — owner signs in once
 *   node x-post-browser.mjs check          # probe: is the profile signed in?
 *   node x-post-browser.mjs post "<text>"  # compose + publish a tweet
 *   node x-post-browser.mjs post "<text>" --dry-run   # stop before publish
 *   node x-post-browser.mjs timeline <handle> [--limit N]   # read an author's posts
 *
 * Exit codes: 0 ok, 2 not-signed-in, 1 error.
 */

import { chromium } from 'playwright';
import { mkdirSync, existsSync, readdirSync, rmSync, copyFileSync, readFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { normalizeComposerText, composerMatches } from './composer-text.mjs';
import { gcftPids, classifyLsofProbe, execTimedOut } from './profile-match.mjs';
import { resolveProfileDir } from './profile-dir.mjs';
import { readManifestConfig, resolveSetting } from './manifest-config.mjs';

async function readComposer(page) {
  return await page.$eval('[data-testid="tweetTextarea_0"]', (el) => el.innerText ?? el.textContent ?? '');
}

function failComposerMismatch(requested, actual) {
  const r = normalizeComposerText(requested);
  const a = normalizeComposerText(actual);
  console.error('composer read-back MISMATCH — refusing to publish (fail closed).');
  console.error(`  requested (${r.length} chars): ${JSON.stringify(r.slice(0, 200))}`);
  console.error(`  in composer (${a.length} chars): ${JSON.stringify(a.slice(0, 200))}`);
  process.exit(3);
}


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

/** Chrome encrypts its cookie store with a key from the macOS login keychain
 *  ("Chrome Safe Storage"). On a host whose login keychain password is unknown
 *  or out of sync with the account password, that prompt cannot be answered and
 *  sign-in is impossible. With the mock key nothing touches the keychain — but
 *  login and playback MUST agree, or the saved cookies cannot be decrypted and
 *  the profile reads as signed out. So it is one switch for both paths.
 *  Trade-off: the cookie store is then encrypted with a well-known key, so the
 *  profile directory alone is enough to use the session. It is per-host and
 *  excluded from sync. */
const MOCK_KEYCHAIN = /^(1|true|yes)$/i.test(process.env.X_BROWSER_MOCK_KEYCHAIN || '');

const cmd = process.argv[2];
const arg = process.argv[3];
const dryRun = process.argv.includes('--dry-run');

/** Repo root, so the canonical workspace resolver can be invoked from here. */
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

/** The workspace, via the ONE resolver every other service uses. Empty if it
 *  cannot be resolved (running outside a checkout). */
function workspaceDir() {
  try {
    return execFileSync('bash', [join(REPO_ROOT, 'scripts', 'sutando-config.sh'), 'workspace'], {
      encoding: 'utf8',
    }).trim();
  } catch {
    return '';
  }
}

/** Where the profile lived before #2133 declared this setting. Kept as ONE
 *  constant, referenced only to migrate — never as a default. */
const LEGACY_PROFILE_DIR = join(homedir(), '.sutando', 'x-browser-profile');

/** The `config` block this skill DECLARES. Read here because this script runs as
 *  its own node process and so inherits nothing from the voice-agent loader's
 *  process.env — see ./manifest-config.mjs for why that distinction matters. */
const MANIFEST_CONFIG = readManifestConfig({
  manifestPath: join(dirname(fileURLToPath(import.meta.url)), 'manifest.json'),
  readFile: readFileSync,
});

/** env override > manifest config > built-in default, per skills/MANIFEST.md. */
const setting = (key, fallback) =>
  resolveSetting(key, { env: process.env, config: MANIFEST_CONFIG, fallback }).value;

/** Durable Chrome profile holding the X login. Precedence + rationale live in
 *  ./profile-dir.mjs, which is importable and unit-tested; this only supplies the
 *  real filesystem and the real workspace resolver. The override now also honors a
 *  manifest-declared value, not just the env var — it ships empty, so the derived
 *  per-workspace path is unchanged. */
const _profile = resolveProfileDir({
  env: setting('X_BROWSER_PROFILE', ''),
  workspace: workspaceDir(),
  legacyDir: LEGACY_PROFILE_DIR,
  exists: existsSync,
});
if (_profile.notice) console.error(`[x-twitter] ${_profile.notice}`);
const PROFILE_DIR = _profile.dir;
const SHOT_DIR = '/tmp/sutando-screenshots';
mkdirSync(PROFILE_DIR, { recursive: true });
mkdirSync(SHOT_DIR, { recursive: true });

if (!cmd || !['login', 'check', 'post', 'timeline'].includes(cmd)) {
  console.error('Usage: node x-post-browser.mjs <login|check|post|timeline> [text|handle] [--dry-run] [--limit N]');
  process.exit(1);
}
if (cmd === 'post' && !arg) {
  console.error('post requires tweet text');
  process.exit(1);
}

const { app: CHROME_APP, bin: CHROME_BIN } = resolveChromium();

/** Which GCfT procs hold THIS profile, or `{known: false}` if the probe couldn't say.
 *  Argv-safe: pgrep runs via execFileSync (no shell), and PROFILE_DIR is matched in JS,
 *  so a profile path with quotes/metacharacters can't break or inject a command
 *  (qingyun review, #2133). The match itself lives in ./profile-match.mjs — it
 *  gates a SIGKILL, so it is exact and independently tested.
 *
 *  Returns `{known: true, pids}` when the lsof probe can be trusted (including the
 *  documented exit-1-with-output case), or `{known: false, pids: []}` when it can't —
 *  permission denied, lsof missing, or our own timeout. Callers MUST fail closed on
 *  `known: false`: it means "cannot say", never "zero holders" (qingyun, #2133 P1 —
 *  the prior version collapsed both to an empty list and releaseProfileLock() then
 *  deleted the singleton files regardless, risking a second Chrome launching against
 *  a still-active profile). */
function pidsForProfile() {
  let pgrepOut;
  try {
    pgrepOut = execFileSync('pgrep', ['-fl', 'Google Chrome for Testing'], { encoding: 'utf8' });
  } catch (e) {
    // Only exit 1 is "no match"; 2/3 and ENOENT mean pgrep could not answer.
    if (e?.status !== 1) return { known: false, pids: [] };
    return { known: true, pids: [] };
  }
  // Flattened argv cannot say WHOSE profile a pid holds; `+D` lets the kernel answer.
  // `timeout` bounds the recursive walk so a stuck probe can't hang lock release.
  let probe;
  try {
    const out = execFileSync('lsof', ['-w', '-F', 'pn', '+D', PROFILE_DIR], {
      encoding: 'utf8',
      timeout: 5000,
    });
    probe = classifyLsofProbe({ threw: false, killed: false, stdout: out });
  } catch (e) {
    // lsof exits 1 even when it PRINTS holders, so a throw is not proof of "none".
    // execTimedOut, not e.killed: execFileSync's timeout error carries no `killed`.
    probe = classifyLsofProbe({ threw: true, killed: execTimedOut(e), stdout: e && e.stdout });
  }
  if (!probe.known) return { known: false, pids: [] };
  if (probe.pids.length === 0) {
    // Confirmed: nothing holds the profile. Kill NOTHING. The string predicate is not
    // a fallback — it cannot decide the ordinary launch shape either, so offering it
    // would be pretend-safety in front of a SIGKILL.
    return { known: true, pids: [] };
  }
  // AND: hold a file in OUR profile, AND be a Chrome-for-Testing process (not a helper).
  const gcft = new Set(gcftPids(pgrepOut));
  return { known: true, pids: probe.pids.filter((pid) => gcft.has(pid)) };
}

/** Kill any GCfT holding THIS profile and clear the SingletonLock, so the next
 *  launch (open or Playwright) doesn't collide on the single-instance lock. */
function releaseProfileLock() {
  try {
    for (const pid of pidsForProfile().pids) {
      try { process.kill(parseInt(pid, 10), 'SIGTERM'); } catch {}
    }
  } catch {}
  try { execFileSync('sleep', ['1']); } catch {}
  try {
    for (const pid of pidsForProfile().pids) {
      try { process.kill(parseInt(pid, 10), 'SIGKILL'); } catch {}
    }
  } catch {}
  // Only clear the singleton files once a probe taken AFTER the kill pass confirms no
  // holder remains. An unknown probe (permission denied, lsof missing, our own timeout)
  // or a holder that survived SIGKILL both mean "cannot say the profile is free" —
  // deleting the lock then would let a second Chrome launch against a still-active
  // profile and corrupt its state (qingyun, #2133 P1). Leaving it is a visible,
  // recoverable "already in use" error on next launch — the same safe direction this
  // file already commits to for the argv-match predicate in profile-match.mjs.
  const confirmed = pidsForProfile();
  if (!confirmed.known || confirmed.pids.length > 0) return;
  try { rmSync(join(PROFILE_DIR, 'SingletonLock'), { force: true }); } catch {}
  try { rmSync(join(PROFILE_DIR, 'SingletonCookie'), { force: true }); } catch {}
  try { rmSync(join(PROFILE_DIR, 'SingletonSocket'), { force: true }); } catch {}
}

/** Read the on-disk cookie DB READ-ONLY (copy first to dodge the WAL/lock) and
 *  report whether the *authenticated* session cookie is present. Non-disruptive
 *  — never opens the profile, so it can run WHILE the login window is up. Cookie
 *  NAMES are cleartext even though values are keychain-encrypted, so this needs
 *  no decryption.
 *
 *  Sentinel is `auth_token` ONLY — the cookie X sets exclusively after a
 *  successful sign-in. NOT `ct0` (a CSRF token X flushes for guest/pre-auth
 *  sessions) nor `twid` alone: keying login-completion on those false-positives
 *  before the owner finishes the flow, killing the window early so the next
 *  `check`/`post` sees an unsigned-in profile and exits 2 (flaky login). */
function authTokenOnDisk() {
  const src = join(PROFILE_DIR, 'Default', 'Cookies');
  if (!existsSync(src)) return 0;
  const tmp = join(tmpdir(), `x-cookies-peek-${process.pid}.db`);
  try {
    copyFileSync(src, tmp);
    const n = execFileSync('sqlite3', [
      tmp,
      "SELECT COUNT(*) FROM cookies WHERE name = 'auth_token' AND (host_key='.x.com' OR host_key='x.com');",
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
    ...(MOCK_KEYCHAIN ? ['--use-mock-keychain'] : []),
    'https://x.com/login',
  ]);
  console.error(
    'A "Google Chrome for Testing" window is opening (Cmd+Tab to it — it has its own Dock icon). ' +
    'Sign in to X (Google/Apple/email all work — no automation flag). ' +
    'I\'ll detect completion automatically; you can leave the window and I\'ll close it.'
  );

  const SENTINEL = setting('X_LOGIN_DONE_SENTINEL', '/tmp/x-login-done');
  try { rmSync(SENTINEL, { force: true }); } catch {}
  const iters = parseInt(setting('X_LOGIN_TIMEOUT_ITERS', '120'), 10) || 120; // ~10min
  for (let i = 0; i < iters; i++) {
    execFileSync('sleep', ['5']);
    if (authTokenOnDisk() >= 1 || existsSync(SENTINEL)) {
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
  // Strip it only when the GUI login also used the real keychain; keeping the
  // two consistent is the whole requirement.
  ...(MOCK_KEYCHAIN ? {} : { ignoreDefaultArgs: ['--use-mock-keychain'] }),
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

  // Reading an author's own posts. The v2 API can do this too, but it is
  // credit-metered and the account's credits run out; this path costs nothing
  // and reaches further back than recent-search's 7 days.
  if (cmd === 'timeline') {
    if (!arg) { console.error('usage: x-post-browser.mjs timeline <handle> [--limit N]'); process.exit(2); }
    if (!(await isSignedIn(page))) {
      console.error('not signed in — run: node x-post-browser.mjs login');
      process.exit(2);
    }
    const li = process.argv.indexOf('--limit');
    const want = li > -1 ? parseInt(process.argv[li + 1], 10) || 50 : 50;
    await page.goto(`https://x.com/${arg.replace(/^@/, '')}`,
                    { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(2500);
    const seen = new Map();
    // Scroll until the page stops yielding new posts: a virtualised timeline
    // drops what scrolls past, so collect on every pass, not at the end.
    for (let pass = 0; pass < 40 && seen.size < want; pass++) {
      const batch = await page.$$eval('article[data-testid="tweet"]', (arts) => arts.map((a) => {
        const link = a.querySelector('a[href*="/status/"]');
        const time = a.querySelector('time');
        const body = a.querySelector('[data-testid="tweetText"]');
        const href = link ? link.getAttribute('href') : '';
        const m = href.match(/\/status\/(\d+)/);
        return m ? { id: m[1], url: `https://x.com${href.split('/photo/')[0]}`,
                     at: time ? time.getAttribute('datetime') : '',
                     text: body ? body.innerText : '' } : null;
      }).filter(Boolean));
      const before = seen.size;
      for (const t of batch) if (!seen.has(t.id)) seen.set(t.id, t);
      if (seen.size === before && pass > 2) break;   // exhausted, not merely slow
      await page.mouse.wheel(0, 3000);
      await page.waitForTimeout(1200);
    }
    // A pinned post sits first in the DOM regardless of age, so DOM order is not
    // chronological. Sort before slicing or `--limit` keeps the wrong ones.
    const out = [...seen.values()]
      .sort((a, b) => (b.at || '').localeCompare(a.at || ''))
      .slice(0, want);
    if (!out.length) { console.error('timeline: no posts extracted — UNKNOWN, not an empty account'); process.exit(3); }
    console.log(JSON.stringify(out, null, 2));
    process.exit(0);
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
    const typedDry = await readComposer(page);
    if (!composerMatches(arg, typedDry)) failComposerMismatch(arg, typedDry);
    if (dryRun) {
      const shot = `${SHOT_DIR}/x-dryrun-${Date.now()}.png`;
      await page.screenshot({ path: shot });
      // report what the composer ACTUALLY holds, not what we asked for
      console.log(JSON.stringify({ dryRun: true, wouldPost: typedDry, verified: true, screenshot: shot }));
      process.exit(0);
    }
    // Publish: inline compose button (tweetButtonInline) or modal (tweetButton).
    const btn = await page.waitForSelector(
      '[data-testid="tweetButtonInline"]:not([aria-disabled="true"]), [data-testid="tweetButton"]:not([aria-disabled="true"])',
      { timeout: 15000 }
    );
    // Re-read immediately before the irreversible click — the button lookup above
    // yields the event loop, so the composer could have changed since the first check.
    const finalText = await readComposer(page);
    if (!composerMatches(arg, finalText)) failComposerMismatch(arg, finalText);
    await btn.click();
    await page.waitForTimeout(3000);
    console.log(JSON.stringify({ posted: true, text: finalText, verified: true }));
    process.exit(0);
  }
} catch (err) {
  console.error(`Error: ${err.message}`);
  process.exit(1);
} finally {
  await ctx.close();
}
