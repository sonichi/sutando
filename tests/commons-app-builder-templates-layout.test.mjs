/* Renders each starter template in a real browser at 360 and 390 px wide with hostile content — a
 * long unbroken Outcome, option name or player name — and fails if the page scrolls sideways.
 *
 * GUIDE.md's phone-width rule is a layout property: the CSS-text checks in the Python suite
 * cannot see it, which is how a 1049px-wide tracker passed review once. This needs a browser,
 * so it SKIPS (exit 0, saying so) when none is found. Set CHROME_PATH to point at one. It does
 * run on GitHub's runners, which ship Chrome.
 * No network: the page is a local file and the templates inline all CSS and JS.
 *
 * The page is measured inside a 390px-wide iframe, not a sized window: --window-size is
 * ignored in headless (it reported 500 and 756 instead of 390), and a frame is how the page
 * really runs. Only the light scheme is rendered - the Python suite proves each template's
 * prefers-color-scheme block sets nothing but colour variables, so dark cannot differ in box
 * sizes. */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TPL = join(ROOT, 'skills', 'commons-app-builder', 'templates');
// 360 is the narrowest phone still common; 390 is the usual one.
const WIDTHS = [360, 390];
const HEIGHT = 844;

const CANDIDATES = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);
// GitHub's runners DO ship Chrome, so this runs in CI as well as locally (verified in the
// node job's log at 1ae40ceb). Where no browser exists it reports a visible SKIP, never a
// false pass.
const browser = process.env.SKIP_BROWSER_TESTS ? null : CANDIDATES.find((p) => existsSync(p));
if (!browser) {
  console.log('all passed — templates layout: SKIPPED, no browser (set CHROME_PATH to run; '
    + 'SKIP_BROWSER_TESTS forces this branch). The CSS-text checks in the Python suite still ran.');
  process.exit(0);
}

/* A fake artifact with hostile but legitimate content: an agent's Outcome holding a long token,
 * a long option name, a long display name. Nothing here is near the bridge's byte cap. */
const LONG = 'x'.repeat(150);
const LONG_NAME = 'Pat ' + 'o'.repeat(120);
const LONG_TITLE = 'Investigate ' + 'y'.repeat(120);
// A select is as wide as its longest option, and a grid column as wide as its widest child: an
// option name someone typed with spaces in it must not widen the add form past the screen.
const LONG_OPTION = 'Waiting on a long answer from the other team ' + 'z'.repeat(40);

const FIXTURES = {
  tracker: {
    db: 'Tracker — Items',
    props: [
      { id: 't', name: 'Item', type: 'title' },
      { id: 's', name: 'Status', type: 'status', options: [
        { name: 'Requested', group: 'todo' }, { name: 'Working', group: 'doing' },
        { name: 'Done', group: 'done' }, { name: 'Blocked', group: 'doing' },
        { name: LONG_OPTION, group: 'doing' }] },
      { id: 'o', name: 'Owner', type: 'person' },
      { id: 'd', name: 'Due', type: 'date' },
      { id: 'n', name: 'Notes', type: 'text' },
      { id: 'c', name: 'Outcome', type: 'text' },
      { id: 'a', name: 'Actor', type: 'person' },
      { id: 'b', name: 'Created by', type: 'created_by' },
    ],
    rows: [{ id: 'r1', cells: { Item: LONG_TITLE, Status: 'Requested', Outcome: LONG, Due: '2026-12-01' } },
           { id: 'r2', cells: { Item: 'Short', Status: LONG_OPTION } }],
  },
  poll: {
    db: 'Poll — Votes',
    props: [
      { id: 't', name: 'Voter', type: 'title' },
      { id: 'c', name: 'Choice', type: 'select', options: [{ name: LONG }, { name: 'Coffee' }] },
      { id: 'm', name: 'Comment', type: 'text' },
      { id: 'b', name: 'Created by', type: 'created_by' },
    ],
    rows: [{ id: 'r1', cells: { Voter: LONG_NAME, Choice: LONG, 'Created by': ['@me:x'] } }],
  },
  planner: {
    db: 'Planner — Items',
    props: [
      { id: 't', name: 'Item', type: 'title' },
      { id: 'd', name: 'Day', type: 'date' },
      { id: 's', name: 'Slot', type: 'select', options: [{ name: 'Morning' }, { name: 'Evening' }, { name: LONG_OPTION }] },
      { id: 'e', name: 'State', type: 'status', options: [{ name: 'Planned', group: 'todo' }, { name: 'Done', group: 'done' }] },
      { id: 'w', name: 'Who', type: 'person' },
      { id: 'b', name: 'Created by', type: 'created_by' },
    ],
    rows: [{ id: 'r1', cells: { Item: LONG_TITLE, Day: '2026-11-11', Slot: 'Morning', State: 'Planned' } }],
  },
  'turn-game': {
    db: 'Game — Moves',
    props: [
      { id: 't', name: 'Move', type: 'title' },
      { id: 'g', name: 'Game', type: 'number' },
      { id: 's', name: 'Seat', type: 'select', options: [{ name: 'X' }, { name: 'O' }] },
      { id: 'r', name: 'Row', type: 'number' },
      { id: 'c', name: 'Col', type: 'number' },
      { id: 'b', name: 'Created by', type: 'created_by' },
    ],
    rows: [{ id: 'm1', cells: { Game: 1, Seat: 'X', Row: 1, Col: 1 } }],
    state: { 'seat.X': { id: 'u1', name: LONG_NAME }, 'seat.O': { id: 'u2', name: LONG_NAME } },
  },
  // Two databases and the viewer's own agents: a long unbroken source link, a long agent name.
  'research-desk': {
    db: 'Research Desk — Findings',
    props: [
      { id: 't', name: 'Finding', type: 'title' },
      { id: 'p', name: 'Topic', type: 'text' },
      { id: 's', name: 'Source', type: 'url' },
      { id: 'b', name: 'Basis', type: 'text' },
      { id: 'c', name: 'Change', type: 'text' },
      { id: 'st', name: 'State', type: 'status', options: [{ name: 'Proposed', group: 'todo' }, { name: 'Accepted', group: 'done' }] },
      { id: 'cb', name: 'Created by', type: 'created_by' },
    ],
    rows: [{ id: 'r1', cells: { Finding: LONG_TITLE, Topic: LONG_OPTION, Source: 'https://example.org/' + LONG, Basis: LONG, Change: LONG, 'Created by': ['@me:x'] } }],
    extra: { d2: { name: 'Research Desk — Reviews', props: [
      { id: 't', name: 'Review', type: 'title' }, { id: 'f', name: 'Finding', type: 'text' },
      { id: 'v', name: 'Verdict', type: 'select', options: [{ name: 'Accepted' }, { name: 'Rejected' }] },
      { id: 'cb', name: 'Created by', type: 'created_by' }],
      rows: [{ id: 'v1', cells: { Review: 'x', Finding: 'r1', Verdict: 'Accepted', 'Created by': ['@me:x'] } }] } },
    members: [{ id: 'u1', name: LONG_NAME, avatar: null, kind: 'human' }],
    agents: [{ id: 'u' + 'a'.repeat(64), name: LONG_NAME, kind: 'agent' }],
  },
  'task-board': {
    db: 'Task Board — Tasks',
    props: [
      { id: 't', name: 'Task', type: 'title' },
      { id: 's', name: 'Status', type: 'status', options: [
        { name: 'Requested', group: 'todo' }, { name: 'Working', group: 'doing' },
        { name: 'Blocked', group: 'doing' }, { name: LONG_OPTION, group: 'done' }] },
      { id: 'b', name: 'Blocker', type: 'text' },
      { id: 'r', name: 'Result', type: 'text' },
      { id: 'a', name: 'Actor', type: 'person' },
      { id: 'cb', name: 'Created by', type: 'created_by' },
    ],
    rows: [{ id: 'r1', cells: { Task: LONG_TITLE, Status: 'Blocked', Blocker: LONG, Result: LONG, Actor: ['@me:x'] } }],
    members: [{ id: 'u1', name: LONG_NAME, avatar: null, kind: 'human' }],
    agents: [{ id: 'u' + 'a'.repeat(64), name: LONG_NAME, kind: 'agent', userId: '@x:y' }],
  },
};

const harness = (f) => `
<script>
window.artifact = (() => {
  const rows = ${JSON.stringify(f.rows)};
  const props = ${JSON.stringify(f.props)};
  const state = new Map(Object.entries(${JSON.stringify(f.state || {})}));
  const snap = () => ({ id: 'd1', name: ${JSON.stringify(f.db)}, props, rows });
  const extra = ${JSON.stringify(f.extra || {})};
  return {
    ready: Promise.resolve(),
    me: { id: 'u1', name: 'Tester', kind: 'human' },
    app: { id: 'a1', name: 'T', database: 'd1' },
    now: () => Date.parse('2026-11-11T12:00:00Z'),
    members: async () => ${JSON.stringify(f.members || [])},
    personId: async (c) => (c && c[0] === '@me:x' ? 'u1' : null),
    state: { get: async (k) => state.get(k), set: async () => ({ ok: true }),
             delete: async () => ({ ok: true }), keys: async () => [...state.keys()],
             all: async () => ({}), on: () => () => {} },
    emit: () => {}, on: () => () => {},
    pages: { list: async () => [], read: async () => null },
    agent: { list: async () => ${JSON.stringify(f.agents || [])}, sendTask: async () => ({ ok: true, eventId: 't1' }) },
    db: { list: async () => [{ id: 'd1', name: ${JSON.stringify(f.db)} }].concat(
            Object.entries(extra).map(([id, d]) => ({ id, name: d.name }))),
          read: async (id) => (extra[id] ? Object.assign({ id }, extra[id]) : snap()), write: async () => ({ ok: true }),
          createRow: async () => ({ ok: true, rowId: 'r9' }), onChange: () => () => {} },
  };
})();
</script>`;

// The measurement rides out in the host's <title>, which --dump-dom prints: no CDP client needed.
const hostPage = (src, WIDTH) => `<!doctype html><html><head><title>pending</title></head>
<body style="margin:0">
<iframe id="f" src="${src}" style="width:${WIDTH}px;height:${HEIGHT}px;border:0"></iframe>
<script>
setTimeout(() => {
  try {
    const d = document.getElementById('f').contentDocument;
    // Name the widest offenders, so a failure says WHAT overflowed, not just that something did.
    const label = (el) => el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : '');
    const all = [...d.querySelectorAll('*')];
    // A box wider than the frame, OR a box whose own content paints past it (an unbreakable
    // word in a 390px box still extends the scrollable area), OR anything reaching past 390.
    const wide = all
      .map((el) => {
        const r = el.getBoundingClientRect();
        return [el, Math.round(r.width), Math.round(r.right), el.scrollWidth - el.clientWidth];
      })
      .filter(([, w, right, over]) => w > ${WIDTH} || right > ${WIDTH} + 1 || over > 1)
      .sort((a, b) => Math.max(b[1], b[2]) - Math.max(a[1], a[2]))
      .slice(0, 6)
      .map(([el, w, right, over]) => label(el) + '[w=' + w + ' right=' + right + ' overflow=' + over + ']')
      .join(' ');
    document.title = 'MEASURED ' + d.documentElement.scrollWidth + ' ' + d.defaultView.innerWidth +
      ' ' + d.body.scrollWidth + ' | ' + wide;
  } catch (e) { document.title = 'ERR ' + e.message; }
}, 900);
</script></body></html>`;

const dir = mkdtempSync(join(tmpdir(), 'tpl-layout-'));
let passed = 0;
const failures = [];

for (const WIDTH of WIDTHS) for (const [name, fixture] of Object.entries(FIXTURES)) {
  const page = readFileSync(join(TPL, name, 'page.html'), 'utf8');
  // The harness must define window.artifact BEFORE the page's own script runs.
  const wired = page.replace('<script>', `${harness(fixture)}\n<script>`);
  const inner = join(dir, `${name}.html`);
  const host = join(dir, `${name}-${WIDTH}-host.html`);
  writeFileSync(inner, wired, 'utf8');
  writeFileSync(host, hostPage(`${name}.html`, WIDTH), 'utf8');
  // A failed LAUNCH is retried once (seen locally, 1 in ~15 runs); a layout assertion is not.
  const launch = () => execFileSync(browser, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
    '--disable-extensions', '--hide-scrollbars', '--allow-file-access-from-files',
    '--virtual-time-budget=2500', '--dump-dom', `file://${host}`,
  ], { encoding: 'utf8', timeout: 120000, stdio: ['ignore', 'pipe', 'ignore'] });
  let dom;
  try {
    try { dom = launch(); } catch { dom = launch(); }
  } catch (e) {
    failures.push(`${name} at ${WIDTH}px: browser failed twice: ${String(e.message).split('\n')[0]}`);
    continue;
  }
  const err = /<title>ERR ([^<]*)/.exec(dom);
  if (err) { failures.push(`${name} at ${WIDTH}px: could not read the frame: ${err[1]}`); continue; }
  const m = /MEASURED (\d+) (\d+) (\d+) \| ([^<]*)/.exec(dom);
  if (!m) { failures.push(`${name} at ${WIDTH}px: no measurement in the rendered DOM`); continue; }
  const [scrollW, innerW, bodyW] = m.slice(1, 4).map(Number);
  const widest = (m[4] || '').trim();
  try {
    assert.equal(innerW, WIDTH, `the frame viewport is ${WIDTH}px wide, got ${innerW}`);
    assert.ok(
      scrollW <= WIDTH,
      `documentElement.scrollWidth ${scrollW} > ${WIDTH} (body ${bodyW}): long unbroken content widens the page. Widest: ${widest || 'none over ' + WIDTH}`
    );
    passed += 1;
  } catch (e) {
    failures.push(`${name} at ${WIDTH}px: ${e.message}`);
  }
}

console.log(failures.length
  ? `FAIL templates layout: ${failures.length} failed\n  ${failures.join('\n  ')}`
  : `all passed — templates layout: ${passed} renders (${Object.keys(FIXTURES).length} templates at ${WIDTHS.join(' and ')} px wide) with long unbroken content, no sideways scroll`);
process.exit(failures.length ? 1 : 0);
