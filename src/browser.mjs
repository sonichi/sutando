#!/usr/bin/env node
/**
 * Sutando browser automation — lightweight Playwright wrapper.
 *
 * Usage:
 *   node src/browser.mjs setup [url]                    # open persistent profile for sign-in
 *   node src/browser.mjs profile                        # print persistent profile path
 *   node src/browser.mjs <url>                          # get page text
 *   node src/browser.mjs <url> screenshot               # full-page screenshot → path
 *   node src/browser.mjs <url> "click:#submit"          # click a selector
 *   node src/browser.mjs <url> "fill:#email:me@x.com"   # fill an input
 *   node src/browser.mjs <url> pdf                       # save as PDF → path
 *
 * Uses system Chrome with a Sutando-owned persistent profile (no bundled
 * browser download needed). Set SUTANDO_BROWSER_PROFILE to override its path,
 * or SUTANDO_BROWSER_HEADLESS=0 / pass --headed to watch automation live.
 * Output goes to stdout; errors to stderr.
 */

import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const PROFILE_DIR = (
  process.env.SUTANDO_BROWSER_PROFILE
  || join(
    execFileSync('bash', [join(REPO, 'scripts/sutando-config.sh'), 'workspace'], {
      encoding: 'utf8',
    }).trim(),
    'data/browser-profile',
  )
).replace(/^~(?=\/)/, process.env.HOME || '');
const command = process.argv[2];

if (!command) {
  console.error('Usage: node src/browser.mjs {setup [url]|profile|<url> [action ...] [--headed]}');
  process.exit(1);
}

if (command === 'profile') {
  console.log(PROFILE_DIR);
  process.exit(0);
}

const setupMode = command === 'setup';
const url = setupMode ? (process.argv[3] || 'about:blank') : command;
const rawActions = process.argv.slice(setupMode ? 4 : 3);
const headed = setupMode || rawActions.includes('--headed') || process.env.SUTANDO_BROWSER_HEADLESS === '0';
const actions = rawActions.filter((action) => action !== '--headed');
// Per-user temp dir: a shared /tmp/sutando-screenshots is owned by whichever
// macOS account created it first and EACCES-blocks every other account.
const SCREENSHOT_DIR = process.env.SUTANDO_SCREENSHOT_DIR || join(tmpdir(), 'sutando-screenshots');
mkdirSync(SCREENSHOT_DIR, { recursive: true });
mkdirSync(PROFILE_DIR, { recursive: true });

const context = await chromium.launchPersistentContext(PROFILE_DIR, {
  channel: 'chrome',
  headless: !headed,
  viewport: headed ? null : { width: 1440, height: 1000 },
});

try {
  const page = context.pages()[0] || await context.newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });

  if (setupMode) {
    console.log(`Sutando browser profile: ${PROFILE_DIR}`);
    console.log('Sign in to the sites Sutando may use, then close this browser window.');
    await new Promise((resolve) => context.once('close', resolve));
  } else if (actions.length === 0) {
    // Default: return page text
    const text = await page.innerText('body').catch(() => '');
    console.log(text.slice(0, 10000));
  } else {
    for (const action of actions) {
      if (action === 'screenshot') {
        const path = `${SCREENSHOT_DIR}/browser-${Date.now()}.png`;
        await page.screenshot({ path, fullPage: true });
        console.log(path);
      } else if (action === 'pdf') {
        const path = `${SCREENSHOT_DIR}/page-${Date.now()}.pdf`;
        await page.pdf({ path, format: 'A4' });
        console.log(path);
      } else if (action === 'text') {
        const text = await page.innerText('body').catch(() => '');
        console.log(text.slice(0, 10000));
      } else if (action === 'html') {
        const html = await page.content();
        console.log(html.slice(0, 20000));
      } else if (action.startsWith('click:')) {
        const selector = action.slice(6);
        await page.click(selector, { timeout: 10000 });
        console.log(`Clicked: ${selector}`);
      } else if (action.startsWith('fill:')) {
        const parts = action.split(':');
        const selector = parts[1];
        const value = parts.slice(2).join(':');
        await page.fill(selector, value, { timeout: 10000 });
        console.log(`Filled: ${selector}`);
      } else if (action.startsWith('wait:')) {
        const ms = parseInt(action.slice(5)) || 2000;
        await page.waitForTimeout(ms);
        console.log(`Waited: ${ms}ms`);
      } else if (action.startsWith('select:')) {
        const parts = action.split(':');
        const selector = parts[1];
        const value = parts.slice(2).join(':');
        await page.selectOption(selector, value, { timeout: 10000 });
        console.log(`Selected: ${selector} = ${value}`);
      } else {
        console.error(`Unknown action: ${action}`);
      }
    }
  }
} catch (err) {
  console.error(`Error: ${err.message}`);
  process.exit(1);
} finally {
  if (!setupMode) await context.close();
}
