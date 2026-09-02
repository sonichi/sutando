import { appendFileSync } from 'node:fs';

const mode = process.env.SUTANDO_BROWSER_FAKE_MODE || 'success';
const logPath = process.env.SUTANDO_BROWSER_FAKE_LOG;
let rejectPending;
let hangTimer;

function record(value) {
  if (logPath) appendFileSync(logPath, `${value}\n`, 'utf8');
}

function recordTimed(value) {
  record(value);
  record(`${value}.at=${Date.now()}`);
}

const browser = {
  async close() {
    record('browser.close');
  },
};

const page = {
  async goto(_url, options = {}) {
    record('page.goto');
    record(`page.goto.timeout=${options.timeout}`);
    if (mode === 'error') throw new Error('fixture navigation failed');
    if (mode === 'hang') {
      return new Promise((resolve, reject) => {
        rejectPending = reject;
        hangTimer = setInterval(() => {}, 1000);
      });
    }
  },
  async close() {
    recordTimed('page.close');
    if (mode === 'slow-close') await new Promise((resolve) => setTimeout(resolve, 2000));
    clearInterval(hangTimer);
    rejectPending?.(new Error('fixture page closed'));
  },
  async innerText() {
    return 'fixture text';
  },
  async waitForTimeout(ms) {
    record(`page.wait=${ms}`);
  },
};

const context = {
  browser() {
    return browser;
  },
  pages() {
    return [page];
  },
  async newPage() {
    return page;
  },
  async close() {
    record('context.close');
  },
};

export const chromium = {
  async launchPersistentContext() {
    recordTimed('context.launch');
    if (mode === 'late-launch') {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return context;
  },
};
