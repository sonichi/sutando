// Fake `playwright` for the media behaviour test: records an ordered event log
// so a removed setInputFiles or a dropped `await` changes the log, not a token.
import { appendFileSync } from 'node:fs';
const LOG = process.env.XSTUB_LOG;
const rec = (e) => appendFileSync(LOG, e + '\n');
let attachmentsReady = false;

const page = {
  url: () => 'https://x.com/home',
  async goto() { rec('goto'); },
  async waitForTimeout() {},
  async screenshot() { rec(`screenshot(attachmentsReady=${attachmentsReady})`); },
  async keyboard_type() {},
  keyboard: { async type(t) { rec('type'); page._typed = t; } },
  async $(sel) { return sel.includes('tweetTextarea') || sel.includes('SideNav') ? handle(sel) : null; },
  async $$(){ return []; },
  async $eval(_sel, _fn) { return page._typed ?? ''; },
  async $$eval() { return []; },
  async waitForSelector(sel) {
    if (sel.includes('attachments')) {
      // Resolve on a LATER tick and flip the flag only then: code that does not
      // await this will screenshot/publish while attachmentsReady is still false.
      rec('wait:attachments:start');
      await new Promise(r => setTimeout(r, 30));
      attachmentsReady = true;
      rec('wait:attachments:resolved');
      return handle(sel);
    }
    if (sel.includes('file')) { rec('found:fileinput'); return handle(sel); }
    rec(`wait:${sel.slice(0, 30)}`);
    return handle(sel);
  },
  async evaluate() { return page._typed ?? ''; },
};
const handle = (_sel) => ({
  async click() { rec('click'); },
  async setInputFiles(_p) { rec('setInputFiles'); },
  async innerText() { return page._typed ?? ''; },
  async textContent() { return page._typed ?? ''; },
  async evaluate() { return page._typed ?? ''; },
  async getAttribute() { return null; },
});
export const chromium = {
  async launchPersistentContext() {
    rec('launch');
    return { pages: () => [page], newPage: async () => page, async close() { rec('close'); } };
  },
};
export default { chromium };
