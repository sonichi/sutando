#!/usr/bin/env node
/**
 * Sutando browser automation — lightweight Playwright wrapper.
 *
 * Usage:
 *   node src/browser.mjs <url>                          # get page text
 *   node src/browser.mjs <url> screenshot               # full-page screenshot → path
 *   node src/browser.mjs <url> "click:#submit"          # click a selector
 *   node src/browser.mjs <url> "fill:#email:me@x.com"   # fill an input
 *   node src/browser.mjs <url> pdf                       # save as PDF → path
 *
 * Uses system Chrome (no bundled browser download needed).
 * Output goes to stdout; errors to stderr.
 */

import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const url = process.argv[2];
if (!url) {
  console.error('Usage: node src/browser.mjs <url> [action]');
  process.exit(1);
}

const actions = process.argv.slice(3);
// Per-user temp dir: a shared /tmp/sutando-screenshots is owned by whichever
// macOS account created it first and EACCES-blocks every other account.
const SCREENSHOT_DIR = process.env.SUTANDO_SCREENSHOT_DIR || join(tmpdir(), 'sutando-screenshots');
mkdirSync(SCREENSHOT_DIR, { recursive: true });

const browser = await chromium.launch({
  channel: 'chrome',
  headless: true,
});

try {
  const page = await browser.newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });

  if (actions.length === 0) {
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
        console.log(`Filled: ${selector} = ${value}`);
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
  await browser.close();
}
