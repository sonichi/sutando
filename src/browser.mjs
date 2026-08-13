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
 *   node src/browser.mjs <url> screenshot --timeout=60000
 *
 * Uses system Chrome with a Sutando-owned persistent profile (no bundled
 * browser download needed). Set SUTANDO_BROWSER_PROFILE to override its path,
 * or SUTANDO_BROWSER_HEADLESS=0 / pass --headed to watch automation live.
 * Output goes to stdout; errors to stderr.
 */

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
const MAX_COMMAND_TIMEOUT_MS = 300000;
const timeoutOptions = rawActions.filter((action) => action.startsWith('--timeout='));
if (timeoutOptions.length > 1 || (timeoutOptions[0] && !/^--timeout=[1-9]\d*$/.test(timeoutOptions[0]))) {
  console.error('Error: --timeout must be one positive integer in milliseconds');
  process.exit(1);
}
const commandTimeoutMs = timeoutOptions[0] ? Number(timeoutOptions[0].slice('--timeout='.length)) : 45000;
if (commandTimeoutMs > MAX_COMMAND_TIMEOUT_MS) {
  console.error(`Error: --timeout cannot exceed ${MAX_COMMAND_TIMEOUT_MS} milliseconds`);
  process.exit(1);
}
// Keep part of the advertised command-level budget for closing a page/context
// that becomes available just as the operation itself times out.
const requestedCleanupBudgetMs = Math.min(5000, Math.max(10, Math.floor(commandTimeoutMs / 5)));
const cleanupBudgetMs = setupMode ? 0 : Math.min(Math.max(0, commandTimeoutMs - 1), requestedCleanupBudgetMs);
const commandDeadline = setupMode ? Infinity : Date.now() + commandTimeoutMs;
const operationDeadline = commandDeadline - cleanupBudgetMs;
const actions = rawActions.filter((action) => action !== '--headed' && !action.startsWith('--timeout='));
const waitActionMs = (action) => parseInt(action.slice(5)) || 2000;
const declaredWaitMs = setupMode ? 0 : actions
  .filter((action) => action.startsWith('wait:'))
  .reduce((total, action) => total + waitActionMs(action), 0);
const operationBudgetMs = commandTimeoutMs - cleanupBudgetMs;
if (declaredWaitMs >= operationBudgetMs) {
  console.error(
    `Error: wait actions require ${declaredWaitMs}ms, exceeding the ${commandTimeoutMs}ms command budget `
    + `(${operationBudgetMs}ms available before cleanup); pass a larger --timeout`,
  );
  process.exit(1);
}
// Per-user temp dir: a shared /tmp/sutando-screenshots is owned by whichever
// macOS account created it first and EACCES-blocks every other account.
const SCREENSHOT_DIR = process.env.SUTANDO_SCREENSHOT_DIR || join(tmpdir(), 'sutando-screenshots');
mkdirSync(SCREENSHOT_DIR, { recursive: true });
mkdirSync(PROFILE_DIR, { recursive: true });

class BrowserInterruption extends Error {
  constructor(signal) {
    super(`interrupted by ${signal}`);
    this.exitCode = signal === 'SIGINT' ? 130 : 143;
  }
}

class BrowserCommandTimeout extends Error {
  constructor(timeoutMs) {
    super(`browser command timed out after ${timeoutMs}ms`);
  }
}

async function closeQuietly(resource) {
  if (!resource || typeof resource.close !== 'function') return;
  try {
    await resource.close();
  } catch {
    // A parent close may already have closed this resource.
  }
}

const { chromium } = await import('playwright');
let context;
let page;
let browser;
let launchPromise;
let cleanupPromise;
let stopping = false;

async function cleanup() {
  if (cleanupPromise) return cleanupPromise;
  // Start every close even if another resource is already closed or stalls.
  cleanupPromise = Promise.allSettled([
    closeQuietly(page),
    closeQuietly(context),
    closeQuietly(browser),
  ]);
  return cleanupPromise;
}

async function settleBefore(promise, deadline) {
  if (!Number.isFinite(deadline)) return promise;
  const remaining = Math.max(0, deadline - Date.now());
  if (!remaining) return false;
  let timer;
  const settled = await Promise.race([
    promise.then(() => true, () => true),
    new Promise((resolve) => { timer = setTimeout(() => resolve(false), remaining); }),
  ]);
  clearTimeout(timer);
  return settled;
}

function remainingOperationTimeout(defaultMs) {
  if (!Number.isFinite(operationDeadline)) return defaultMs;
  return Math.max(1, operationDeadline - Date.now());
}

let rejectInterruption;
const interruption = new Promise((resolve, reject) => {
  rejectInterruption = reject;
});
let receivedSignal;
const signalHandlers = new Map(['SIGINT', 'SIGTERM'].map((signal) => {
  const handler = () => {
    if (receivedSignal) return;
    receivedSignal = signal;
    // Restore Node's default behavior immediately. A second signal remains an
    // operator escape hatch instead of becoming a no-op during cleanup.
    for (const [name, activeHandler] of signalHandlers) process.off(name, activeHandler);
    rejectInterruption(new BrowserInterruption(signal));
  };
  process.on(signal, handler);
  return [signal, handler];
}));

const operation = (async () => {
  const launchTimeoutMs = Math.max(1, Math.min(30000, operationDeadline - Date.now()));
  launchPromise = chromium.launchPersistentContext(PROFILE_DIR, {
    channel: 'chrome',
    headless: !headed,
    viewport: headed ? null : { width: 1440, height: 1000 },
    timeout: launchTimeoutMs,
  });
  const launchedContext = await launchPromise;
  context = launchedContext;
  browser = context.browser?.();
  page = context.pages()[0] || await context.newPage();
  if (stopping) {
    cleanupPromise = null;
    await cleanup();
    return;
  }
  await page.goto(url, {
    waitUntil: 'domcontentloaded',
    timeout: remainingOperationTimeout(30000),
  });

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
        const ms = waitActionMs(action);
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
})();

let timeoutId;
const timeout = setupMode
  ? new Promise(() => {})
  : new Promise((resolve, reject) => {
    const delay = Math.max(0, operationDeadline - Date.now());
    timeoutId = setTimeout(() => reject(new BrowserCommandTimeout(commandTimeoutMs)), delay);
  });

try {
  await Promise.race([operation, interruption, timeout]);
} catch (err) {
  console.error(`Error: ${err.message}`);
  process.exitCode = err.exitCode || 1;
} finally {
  stopping = true;
  clearTimeout(timeoutId);
  // `operation` adopts and closes a context that launches after cancellation.
  // Its wait shares the original command deadline rather than adding a fresh
  // Playwright launch timeout after the advertised bound has elapsed.
  await settleBefore(Promise.allSettled([cleanup(), operation]), commandDeadline);
  for (const [signal, handler] of signalHandlers) process.off(signal, handler);
}
