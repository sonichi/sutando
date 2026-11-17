/* Runs each skills/commons-app-builder/templates/<t>/page.html against a fake window.artifact,
 * the way GUIDE.md step 6 asks, and checks the data path of every control. Hermetic: a tiny DOM
 * shim, no jsdom, no network, no browser. Layout at 390px is NOT checked here — only a real
 * browser lays out CSS; the CSS contract is checked in the Python suite. */
// Pin a zone with daylight saving and a negative UTC offset. CI runs in UTC, where the DST and
// local-date cases would pass without testing anything.
process.env.TZ = 'America/Los_Angeles';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TPL = join(ROOT, 'skills', 'commons-app-builder', 'templates');
let passed = 0;
const failures = [];
// A page that throws inside an async handler produces an unhandled rejection, which would kill
// the suite with a stack trace instead of a named failure. Record them and report at the end:
// "the page threw and never recovered" is a result, not a crash.
const unhandled = [];
process.on('unhandledRejection', (e) => {
  unhandled.push(e && e.message ? e.message : String(e));
});
const check = (name, fn) => {
  try { fn(); passed += 1; } catch (e) { failures.push(`${name}: ${e.message}`); }
};

/* ---------------------------------------------------------------- DOM shim */
class Txt {
  constructor(v) { this.isText = true; this._v = String(v); }
  get textContent() { return this._v; }
  set textContent(v) { this._v = String(v); }
}
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = {};
    this.style = {};
    this.listeners = {};
    this.disabled = false;
    this.hidden = false;
    this.className = '';
    this.id = '';
    this.value = '';
    this.onclick = null;
    if (this.tagName === 'SELECT') this.options = [];
  }
  // A real DOM keeps the text node when a child is appended after textContent is set.
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  set textContent(v) {
    const t = v == null ? '' : String(v);
    this.children = t === '' ? [] : [new Txt(t)];
    // A real <select> loses its options when its content is replaced.
    if (this.tagName === 'SELECT') this.options = [];
  }
  appendChild(node) {
    if (node && node.isFragment) { node.children.forEach((c) => this.appendChild(c)); return node; }
    this.children.push(node);
    if (this.tagName === 'SELECT' && node.tagName === 'OPTION') {
      this.options.push(node);
      if (!this.value) this.value = node.value;      // the browser selects the first option
    }
    return node;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  dispatch(name, ev) { (this.listeners[name] || []).forEach((f) => f(ev || {})); }
  click() { if (this.disabled) return false; if (this.onclick) { this.onclick({}); return true; } return false; }
  all() { return this.children.reduce((a, c) => a.concat(c.all ? [c, ...c.all()] : [c]), []); }
  find(pred) { return this.all().find(pred) || null; }
  buttons() { return this.all().filter((e) => e.tagName === 'BUTTON'); }
}
class Frag extends El { constructor() { super('fragment'); this.isFragment = true; } }

const makeDom = (html) => {
  const body = new El('body');
  // Create the elements the page expects by id, read straight from its own markup.
  const ids = [...html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)].map((m) => [m[1], m[2]]);
  const byId = new Map();
  ids.forEach(([tag, id]) => {
    const el = new El(tag);
    el.id = id;
    byId.set(id, el);
    body.appendChild(el);
  });
  const document = {
    body,
    getElementById: (id) => byId.get(id) || null,
    createElement: (t) => new El(t),
    createDocumentFragment: () => new Frag(),
  };
  return { document, body, byId };
};

const scriptOf = (html) => {
  const m = /<script>([\s\S]*?)<\/script>/.exec(html);
  assert.ok(m, 'page has an inline script');
  return m[1];
};

/** vm-created objects have the sandbox's prototype: copy to a host object before comparing. */
const plain = (o) => JSON.parse(JSON.stringify(o));

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 24) => { for (let i = 0; i < n; i += 1) await tick(); };

/* ------------------------------------------------------------- fake bridge */
const fakeArtifact = (opts = {}) => {
  const calls = [];
  const rows = opts.rows || [];
  const props = opts.props;
  let onChangeFn = null;
  let writeAnswers = (opts.writeAnswers || []).slice();
  const answer = () => (writeAnswers.length ? writeAnswers.shift() : { ok: true });
  const snap = () => ({ id: 'd1', name: opts.dbName, props, rows });
  const state = new Map(Object.entries(opts.state || {}));
  return {
    calls,
    snapshot: snap,
    fire: (s) => onChangeFn && onChangeFn(s === undefined ? snap() : s),
    art: {
      ready: Promise.resolve(),
      me: opts.me === null ? null : (opts.me || { id: 'u1', name: 'Tester', kind: 'human' }),
      app: opts.app === null ? null : { id: 'a1', name: 'T', database: 'd1' },
      now: () => (opts.now === undefined ? Date.parse('2026-11-11T12:00:00Z') : opts.now),
      members: async () => [{ id: 'u1', name: 'Tester', avatar: null, kind: 'human' }],
      personId: async (cell) => {
        if (opts.consent === false) throw new Error('page permission required');
        return cell && cell[0] === '@me:x' ? 'u1' : null;
      },
      state: {
        get: async (k) => state.get(k),
        set: async (k, v) => { state.set(k, v); return { ok: true }; },
        delete: async (k) => { state.delete(k); return { ok: true }; },
        keys: async () => [...state.keys()],
        all: async () => Object.fromEntries(state),
        on: () => () => {},
      },
      emit: () => {},
      on: () => () => {},
      pages: { list: async () => [], read: async () => null },
      db: {
        list: async () => (opts.dbs === undefined ? [{ id: 'd1', name: opts.dbName }] : opts.dbs),
        read: async () => { if (opts.readThrows) throw new Error(opts.readThrows); return snap(); },
        write: async (...a) => { calls.push(['write', ...a]); return answer(); },
        createRow: async (...a) => { calls.push(['createRow', ...a]); const r = answer(); return r.ok ? { ok: true, rowId: 'r' + calls.length } : r; },
        onChange: (d, fn) => { onChangeFn = fn; return () => {}; },
      },
    },
  };
};

const run = async (template, opts = {}) => {
  const html = readFileSync(join(TPL, template, 'page.html'), 'utf8');
  const dom = makeDom(html);
  const bridge = opts.noBridge ? { art: undefined, calls: [], fire: () => {} } : (opts.bridge || fakeArtifact(opts));
  const location = { hash: opts.hash || '' };
  const sandbox = {
    window: { artifact: bridge.art, addEventListener: () => {} },
    document: dom.document,
    location,
    setTimeout,
    clearTimeout,
    Promise,
    Date,
    Number,
    Math,
    String,
    Object,
    Array,
    JSON,
    console,
    RegExp,
    Error,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle(opts.ticks);
  return { ...dom, ...bridge, location, text: () => dom.body.textContent };
};

/* ================================================================= TRACKER */
const TRACKER_PROPS = [
  { id: 't', name: 'Item', type: 'title' },
  { id: 's', name: 'Status', type: 'status', options: [
    { name: 'Requested', group: 'todo' }, { name: 'Working', group: 'doing' },
    { name: 'Done', group: 'done' }, { name: 'Blocked', group: 'doing' }] },
  { id: 'o', name: 'Owner', type: 'person' },
  { id: 'd', name: 'Due', type: 'date' },
  { id: 'n', name: 'Notes', type: 'text' },
  { id: 'c', name: 'Outcome', type: 'text' },
  { id: 'a', name: 'Actor', type: 'person' },
  { id: 'b', name: 'Created by', type: 'created_by' },
];
const trackerOpts = (over = {}) => ({ dbName: 'Tracker — Items', props: TRACKER_PROPS, ...over });

{
  const r = await run('tracker', { noBridge: true });
  check('tracker/no-bridge-preview', () => {
    assert.match(r.text(), /Open this page in s\.space/);
    assert.equal(r.byId.get('add').disabled, true);
  });
}
{
  const r = await run('tracker', trackerOpts({ rows: [] }));
  check('tracker/empty-state', () => assert.match(r.text(), /Nothing tracked yet/));
  check('tracker/status-options-filled', () => assert.equal(r.byId.get('status').options.length, 4));
  check('tracker/add-enabled-with-title-prop', () => assert.equal(r.byId.get('add').disabled, false));

  r.byId.get('title').value = 'Fix the sink';
  r.byId.get('status').value = 'Working';
  r.byId.get('due').value = '2026-12-01';
  r.byId.get('add').click();
  await settle();
  check('tracker/add-writes-one-createRow', () => {
    assert.equal(r.calls.length, 1);
    const [kind, db, values] = r.calls[0];
    assert.equal(kind, 'createRow');
    assert.equal(db, 'd1');
    assert.deepEqual(plain(values), { Item: 'Fix the sink', Status: 'Working', Due: '2026-12-01' });
  });
  check('tracker/input-cleared-after-success', () => assert.equal(r.byId.get('title').value, ''));
}
{
  const rows = [{ id: 'r1', cells: { Item: 'Mend fence', Status: 'Requested', 'Created by': ['@me:x'] } }];
  const r = await run('tracker', trackerOpts({ rows }));
  check('tracker/row-shown-with-status', () => {
    assert.match(r.text(), /Mend fence/);
    assert.match(r.text(), /Requested/);
  });
  check('tracker/own-row-marked', () => assert.match(r.text(), /Added by you/));
  const done = r.byId.get('list').buttons().find((b) => b.textContent === 'Done');
  check('tracker/status-buttons-exclude-current', () => {
    const labels = r.byId.get('list').buttons().map((b) => b.textContent);
    assert.ok(!labels.includes('Requested'), labels.join(','));
    assert.ok(labels.includes('Done'));
  });
  done.click();
  await settle();
  check('tracker/status-click-writes-status', () =>
    assert.deepEqual(r.calls[0], ['write', 'd1', 'r1', 'Status', 'Done']));
}
{
  const rows = [{ id: 'r1', cells: { Item: 'Mend fence', Status: 'Requested', 'Created by': ['@me:x'] } }];
  const r = await run('tracker', trackerOpts({ rows, consent: false }));
  check('tracker/consent-pending-shows-question-mark', () => {
    assert.match(r.text(), /Added by \?/);
    assert.match(r.text(), /who is who/);
    assert.ok(!/Added by you/.test(r.text()));
  });
}
{
  const r = await run('tracker', trackerOpts({
    rows: [], writeAnswers: [{ ok: false, reason: "waiting for the viewer’s answer" }, { ok: true }] }));
  r.byId.get('title').value = 'Retry me';
  r.byId.get('add').click();
  await settle(40);
  check('tracker/consent-refusal-retries-and-hints', () => {
    assert.match(r.text(), /Allow this page to edit data/);
    assert.ok(r.calls.length >= 1);
  });
}
{
  const r = await run('tracker', trackerOpts({ rows: [], dbs: [] , app: null}));
  check('tracker/database-missing-error', () => assert.match(r.text(), /database is missing from this room/));
}
{
  const r = await run('tracker', trackerOpts({ rows: [] }));
  r.fire(null);
  await settle();
  check('tracker/database-removed-error', () => assert.match(r.text(), /was removed from this room/));
}

/* ==================================================================== POLL */
const POLL_PROPS = [
  { id: 't', name: 'Voter', type: 'title' },
  { id: 'c', name: 'Choice', type: 'select', options: [{ name: 'Tea' }, { name: 'Coffee' }] },
  { id: 'm', name: 'Comment', type: 'text' },
  { id: 'b', name: 'Created by', type: 'created_by' },
];
const pollOpts = (over = {}) => ({ dbName: 'Poll — Votes', props: POLL_PROPS, ...over });

{
  const r = await run('poll', pollOpts({ rows: [] }));
  check('poll/empty-state', () => assert.match(r.text(), /No votes yet/));
  check('poll/two-choice-buttons', () => assert.equal(r.byId.get('choices').buttons().length, 2));
  r.byId.get('comment').value = 'please';
  r.byId.get('choices').buttons()[1].click();
  await settle();
  check('poll/vote-creates-one-row', () => {
    assert.equal(r.calls.length, 1);
    const [kind, , values] = r.calls[0];
    assert.equal(kind, 'createRow');
    assert.deepEqual(plain(values), { Choice: 'Coffee', Voter: 'Tester', Comment: 'please' });
  });
}
{
  const rows = [
    { id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } },
    { id: 'r2', cells: { Voter: 'Other', Choice: 'Coffee', 'Created by': ['@other:x'] } },
    { id: 'r3', cells: { Voter: 'Third', Choice: 'Tea', 'Created by': ['@third:x'] } },
  ];
  const r = await run('poll', pollOpts({ rows }));
  check('poll/tally-and-percentages', () => {
    assert.match(r.text(), /3 votes/);
    assert.match(r.text(), /Tea.*2.*67%/s);
    assert.match(r.text(), /Coffee.*1.*33%/s);
  });
  check('poll/own-vote-identified-via-created-by', () => {
    assert.match(r.text(), /you voted Tea/);
    assert.match(r.text(), /\(yours\)/);
  });
  check('poll/own-choice-button-disabled', () => {
    const tea = r.byId.get('choices').buttons().find((b) => /Tea/.test(b.textContent));
    assert.equal(tea.disabled, true);
  });
  const coffee = r.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent));
  coffee.click();
  await settle();
  check('poll/changing-vote-writes-not-creates', () => {
    assert.equal(r.calls.length, 1);
    assert.deepEqual(r.calls[0], ['write', 'd1', 'r1', 'Choice', 'Coffee']);
  });
}
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  const r = await run('poll', pollOpts({ rows, consent: false }));
  check('poll/consent-pending-hides-ownership', () => {
    assert.match(r.text(), /your vote: \?/);
    assert.match(r.text(), /who is who/);
    assert.ok(!/you voted/.test(r.text()));
  });
}
{
  const r = await run('poll', { noBridge: true });
  check('poll/no-bridge-preview', () => assert.match(r.text(), /Open this page in s\.space to vote/));
}

/* ================================================================= PLANNER */
const PLANNER_PROPS = [
  { id: 't', name: 'Item', type: 'title' },
  { id: 'd', name: 'Day', type: 'date' },
  { id: 's', name: 'Slot', type: 'select', options: [{ name: 'Morning' }, { name: 'Afternoon' }, { name: 'Evening' }] },
  { id: 'e', name: 'State', type: 'status', options: [{ name: 'Planned', group: 'todo' }, { name: 'Done', group: 'done' }, { name: 'Dropped', group: 'done' }] },
  { id: 'w', name: 'Who', type: 'person' },
  { id: 'b', name: 'Created by', type: 'created_by' },
];
// 2026-11-11 is a Wednesday; its Monday is 2026-11-09.
const plannerOpts = (over = {}) => ({ dbName: 'Planner — Items', props: PLANNER_PROPS, hash: 'week=2026-11-09', ...over });

{
  const r = await run('planner', plannerOpts({ rows: [] }));
  check('planner/seven-day-columns', () => assert.equal(r.byId.get('week').children.length, 7));
  check('planner/week-range-from-hash', () => assert.equal(r.byId.get('range').textContent, '2026-11-09 → 2026-11-15'));
  check('planner/empty-days', () => assert.match(r.byId.get('week').textContent, /Nothing planned/));
  check('planner/slot-options-filled', () => assert.equal(r.byId.get('slot').options.length, 3));

  r.byId.get('title').value = 'Dentist';
  r.byId.get('day').value = '2026-11-12';
  r.byId.get('slot').value = 'Morning';
  r.byId.get('add').click();
  await settle();
  check('planner/add-writes-day-slot-state', () => {
    const [kind, , values] = r.calls[0];
    assert.equal(kind, 'createRow');
    assert.deepEqual(plain(values), { Item: 'Dentist', Day: '2026-11-12', Slot: 'Morning', State: 'Planned' });
  });
}
{
  const rows = [
    { id: 'r1', cells: { Item: 'Dentist', Day: '2026-11-12', Slot: 'Morning', State: 'Planned' } },
    { id: 'r2', cells: { Item: 'Someday thing', State: 'Planned' } },
    { id: 'r3', cells: { Item: 'Next week', Day: '2026-11-20', State: 'Planned' } },
  ];
  const r = await run('planner', plannerOpts({ rows }));
  check('planner/item-lands-in-its-day', () => {
    const thu = r.byId.get('week').children[3];           // Mon..Thu
    assert.match(thu.textContent, /Dentist/);
  });
  check('planner/next-week-item-not-shown', () => assert.ok(!/Next week/.test(r.byId.get('week').textContent)));
  check('planner/dayless-item-is-unscheduled-not-hidden', () => {
    assert.equal(r.byId.get('unscheduledTitle').hidden, false);
    assert.match(r.byId.get('unscheduled').textContent, /Someday thing/);
  });
  const done = r.byId.get('week').buttons().find((b) => b.textContent === 'Done');
  done.click();
  await settle();
  check('planner/done-writes-state', () => assert.deepEqual(r.calls[0], ['write', 'd1', 'r1', 'State', 'Done']));
}
{
  const r = await run('planner', plannerOpts({ rows: [] }));
  r.byId.get('next').click();
  await settle();
  check('planner/next-week-advances-seven-days', () => {
    assert.equal(r.byId.get('range').textContent, '2026-11-16 → 2026-11-22');
    assert.match(r.location.hash, /2026-11-16/);
  });
  r.byId.get('prev').click();
  r.byId.get('prev').click();
  await settle();
  check('planner/prev-week-goes-back', () => assert.equal(r.byId.get('range').textContent, '2026-11-02 → 2026-11-08'));
}
{
  const r = await run('planner', { noBridge: true });
  check('planner/no-bridge-preview', () => assert.match(r.text(), /Open this page in s\.space/));
}

/* =============================================================== TURN GAME */
const GAME_PROPS = [
  { id: 't', name: 'Move', type: 'title' },
  { id: 'g', name: 'Game', type: 'number' },
  { id: 's', name: 'Seat', type: 'select', options: [{ name: 'X' }, { name: 'O' }] },
  { id: 'r', name: 'Row', type: 'number' },
  { id: 'c', name: 'Col', type: 'number' },
  { id: 'b', name: 'Created by', type: 'created_by' },
];
const mv = (id, game, seat, row, col) => ({ id, cells: { Game: game, Seat: seat, Row: row, Col: col } });
const gameOpts = (over = {}) => ({ dbName: 'Game — Moves', props: GAME_PROPS, ...over });
const SEATED = { 'seat.X': { id: 'u1', name: 'Tester' }, 'seat.O': { id: 'u2', name: 'Opponent' } };

{
  const r = await run('turn-game', gameOpts({ rows: [], state: {} }));
  check('game/nine-cells', () => assert.equal(r.byId.get('board').children.length, 9));
  check('game/seat-needed-before-playing', () => {
    assert.match(r.byId.get('status').textContent, /Take a seat to play/);
    assert.ok(r.byId.get('board').buttons().every((b) => b.disabled));
  });
  check('game/both-seats-offered', () => {
    const labels = r.byId.get('seats').buttons().map((b) => b.textContent);
    assert.ok(labels.includes('Play as X') && labels.includes('Play as O'), labels.join(','));
  });
}
{
  const r = await run('turn-game', gameOpts({ rows: [], state: SEATED }));
  check('game/x-moves-first-on-empty-board', () => assert.match(r.byId.get('status').textContent, /Your turn as X/));
  const played = r.byId.get('board').children[4].click();   // centre
  await settle();
  check('game/tap-appends-a-move-row', () => {
    assert.ok(played);
    const [kind, , values] = r.calls[0];
    assert.equal(kind, 'createRow');
    assert.deepEqual(plain(values), { Game: 1, Seat: 'X', Row: 2, Col: 2, Move: 'X r2c2' });
  });
  check('game/no-write-call-for-a-move', () => assert.ok(!r.calls.some((c) => c[0] === 'write')));
}
{
  // One move placed: it is O's turn, so the X viewer waits and cannot play.
  const r = await run('turn-game', gameOpts({ rows: [mv('m1', 1, 'X', 2, 2)], state: SEATED }));
  check('game/turn-follows-move-count', () => assert.match(r.byId.get('status').textContent, /Waiting for O/));
  check('game/board-locked-off-turn', () => assert.ok(r.byId.get('board').buttons().every((b) => b.disabled)));
  check('game/replayed-mark-shown', () => assert.equal(r.byId.get('board').children[4].textContent, 'X'));
}
{
  const rows = [mv('m1', 1, 'X', 1, 1), mv('m2', 1, 'O', 2, 1), mv('m3', 1, 'X', 1, 2),
                mv('m4', 1, 'O', 2, 2), mv('m5', 1, 'X', 1, 3)];
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/winner-detected-from-rows', () => assert.match(r.byId.get('status').textContent, /^X wins\.$/));
  check('game/board-locked-after-a-win', () => assert.ok(r.byId.get('board').buttons().every((b) => b.disabled)));
  const again = r.byId.get('seats').buttons().find((b) => b.textContent === 'New game');
  check('game/new-game-enabled-when-over', () => assert.equal(again.disabled, false));
  again.click();
  await settle();
  check('game/new-game-bumps-the-game-number', () => {
    const [, , values] = r.calls[0];
    assert.equal(plain(values).Game, 2);
  });
}
{
  // A second row for one square must not overwrite the first: first move wins it.
  const rows = [mv('m1', 1, 'X', 1, 1), mv('m2', 1, 'O', 1, 1)];
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/duplicate-square-keeps-the-first-move', () => {
    assert.equal(r.byId.get('board').children[0].textContent, 'X');
    assert.match(r.byId.get('sub').textContent, /1 move/);
  });
}
{
  // Only the highest Game number is replayed.
  const rows = [mv('m1', 1, 'X', 1, 1), mv('m2', 2, 'X', 3, 3)];
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/only-the-current-game-is-replayed', () => {
    assert.equal(r.byId.get('board').children[0].textContent, '');
    assert.equal(r.byId.get('board').children[8].textContent, 'X');
    assert.match(r.byId.get('sub').textContent, /Game 2/);
  });
}
{
  const r = await run('turn-game', { noBridge: true });
  check('game/no-bridge-preview-shows-a-board', () => {
    assert.match(r.text(), /Open this page in s\.space to play/);
    assert.equal(r.byId.get('board').children.length, 9);
    assert.ok(r.byId.get('board').buttons().every((b) => b.disabled));
  });
}
{
  const r = await run('turn-game', gameOpts({ rows: [], state: SEATED, dbs: [], app: null }));
  check('game/database-missing-error', () => assert.match(r.text(), /database is missing from this room/));
}

/* ===================================== review fixes (sonichi, #6308) ===== */
/* 1. poll: one vote per voter, counted from created_by — repeat rows count once. */
{
  const rows = [
    { id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } },
    { id: 'r2', cells: { Voter: 'Tester', Choice: 'Coffee', 'Created by': ['@me:x'] } },
    { id: 'r3', cells: { Voter: 'Other', Choice: 'Tea', 'Created by': ['@other:x'] } },
  ];
  const r = await run('poll', pollOpts({ rows }));
  check('poll/repeat-rows-count-once', () => {
    assert.match(r.byId.get('sub').textContent, /^2 votes/);     // not 3
    assert.match(r.text(), /1 extra row .*not counted/);
  });
  check('poll/latest-row-is-the-persons-vote', () => {
    // r2 (Coffee) is this voter's last row, so Coffee has 1 and Tea has 1.
    assert.match(r.text(), /Coffee.*1.*50%/s);
    assert.match(r.text(), /Tea.*1.*50%/s);
  });
  check('poll/own-vote-is-the-latest', () => assert.match(r.byId.get('sub').textContent, /you voted Coffee/));
}
{
  // A voter with no created_by (should not happen; the server stamps it) still counts once each.
  const rows = [
    { id: 'r1', cells: { Voter: 'A', Choice: 'Tea' } },
    { id: 'r2', cells: { Voter: 'B', Choice: 'Tea' } },
  ];
  const r = await run('poll', pollOpts({ rows }));
  check('poll/rows-without-created-by-count-separately', () =>
    assert.match(r.byId.get('sub').textContent, /^2 votes/));
}
/* 2. poll: consent granted during the retry edits the voter's row, never adds a second. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  let allow = false;                                  // consent arrives mid-retry
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const calls = [];
  const snap = () => ({ id: 'd1', name: 'Poll — Votes', props: POLL_PROPS, rows });
  const art = {
    ready: Promise.resolve(),
    me: { id: 'u1', name: 'Tester', kind: 'human' },
    app: { id: 'a1', name: 'T', database: 'd1' },
    now: () => Date.now(),
    members: async () => [],
    personId: async (cell) => {
      if (!allow) throw new Error('page permission required');
      return cell && cell[0] === '@me:x' ? 'u1' : null;
    },
    state: { get: async () => undefined, set: async () => ({ ok: true }), delete: async () => ({ ok: true }), on: () => () => {} },
    emit: () => {}, on: () => () => {},
    pages: { list: async () => [], read: async () => null },
    db: {
      list: async () => [{ id: 'd1', name: 'Poll — Votes' }],
      read: async () => snap(),
      // Before the viewer answers, the bridge refuses writes too: that is what makes save() retry.
      write: async (...a) => { calls.push(['write', ...a]); return allow ? { ok: true } : { ok: false, reason: 'waiting for the viewer’s answer' }; },
      createRow: async (...a) => { calls.push(['createRow', ...a]); return allow ? { ok: true, rowId: 'rX' } : { ok: false, reason: 'waiting for the viewer’s answer' }; },
      onChange: () => () => {},
    },
  };
  const sandbox = { window: { artifact: art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  // Before consent the page cannot tell whose vote is whose.
  check('poll/retry-starts-without-consent', () => assert.match(dom.body.textContent, /your vote: \?/));
  const coffee = dom.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent));
  const clicked = coffee.click();
  await settle(4);
  allow = true;                                        // the viewer answers the prompt
  // save() waits 1500ms between attempts, so the retry needs real time, not microtask ticks.
  await new Promise((ok) => setTimeout(ok, 1800));
  await settle();
  check('poll/consent-mid-retry-edits-not-duplicates', () => {
    assert.ok(clicked);
    assert.ok(calls.length >= 1, 'a call was made');
    // Pre-consent attempts are refused by the bridge, so they create nothing. Once consent
    // lands, ownership is re-resolved and the retry EDITS the voter's row.
    assert.deepEqual(calls[calls.length - 1].slice(0, 4), ['write', 'd1', 'r1', 'Choice']);
    const created = calls.filter((c) => c[0] === 'createRow');
    assert.ok(created.length <= 1, 'at most the one refused attempt: ' + JSON.stringify(calls));
  });
}
/* 3. poll: a second control cannot fire while the first write is pending. */
{
  let release = null;
  const held = new Promise((r) => { release = r; });
  const rows = [];
  const base = fakeArtifact(pollOpts({ rows }));
  const calls = base.calls;
  base.art.db.createRow = async (...a) => { calls.push(['createRow', ...a]); await held; return { ok: true, rowId: 'r1' }; };
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  const bs = dom.byId.get('choices').buttons();
  bs[0].click();
  await settle(3);
  const second = bs[1].click();
  await settle(3);
  release({ ok: true });
  await settle();
  check('poll/sibling-controls-locked-while-writing', () => {
    assert.equal(second, false, 'the sibling was disabled');
    assert.equal(calls.filter((c) => c[0] === 'createRow').length, 1, JSON.stringify(calls));
  });
}
/* 4. turn-game: a row claiming the wrong seat is skipped, not drawn out of turn. */
{
  const rows = [mv('m1', 1, 'X', 1, 1), mv('m2', 1, 'X', 2, 2)];   // second X on O's turn
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/out-of-turn-row-skipped', () => {
    assert.equal(r.byId.get('board').children[0].textContent, 'X');
    assert.equal(r.byId.get('board').children[4].textContent, '', 'the out-of-turn row is not drawn');
    assert.match(r.byId.get('sub').textContent, /1 move/);
    assert.match(r.byId.get('status').textContent, /Waiting for O/);
  });
}
{
  // A seatless row (a table-view row with no Seat) takes the turn it lands on.
  const rows = [mv('m1', 1, 'X', 1, 1), { id: 'm2', cells: { Game: 1, Row: 2, Col: 2 } }];
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/seatless-row-takes-the-turn-it-lands-on', () => {
    assert.equal(r.byId.get('board').children[4].textContent, 'O');
    assert.match(r.byId.get('status').textContent, /Your turn as X/);
  });
}
/* 5. turn-game: the board locks while a move is in flight. */
{
  let release = null;
  const held = new Promise((r) => { release = r; });
  const base = fakeArtifact(gameOpts({ rows: [], state: SEATED }));
  const calls = base.calls;
  base.art.db.createRow = async (...a) => { calls.push(['createRow', ...a]); await held; return { ok: true, rowId: 'r1' }; };
  const html = readFileSync(join(TPL, 'turn-game', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  const cells = dom.byId.get('board').children;
  cells[0].click();
  await settle(3);
  const second = cells[8].click();
  await settle(3);
  release({ ok: true });
  await settle();
  check('game/board-locked-while-a-move-is-in-flight', () => {
    assert.equal(second, false);
    assert.equal(calls.filter((c) => c[0] === 'createRow').length, 1, JSON.stringify(calls));
  });
}
/* 6. planner: Next keeps advancing across a daylight-saving fallback. */
{
  // 2026-11-01 is the US fallback. Week of Mon 2026-10-26 must advance, not stick.
  const r = await run('planner', plannerOpts({ rows: [], hash: 'week=2026-10-26' }));
  check('planner/dst-start-week', () => assert.equal(r.byId.get('range').textContent, '2026-10-26 → 2026-11-01'));
  r.byId.get('next').click();
  await settle();
  check('planner/next-advances-across-dst', () =>
    assert.equal(r.byId.get('range').textContent, '2026-11-02 → 2026-11-08'));
  r.byId.get('next').click();
  await settle();
  check('planner/next-advances-again-after-dst', () =>
    assert.equal(r.byId.get('range').textContent, '2026-11-09 → 2026-11-15'));
  r.byId.get('prev').click();
  r.byId.get('prev').click();
  await settle();
  check('planner/prev-crosses-dst-back', () =>
    assert.equal(r.byId.get('range').textContent, '2026-10-26 → 2026-11-01'));
  check('planner/seven-days-across-dst', () => assert.equal(r.byId.get('week').children.length, 7));
}

/* ============================== Codex review fixes (#6308 @ 0e1a160c) ==== */
/* P2: a watch update during the save must not leave the live choices disabled. */
{
  const rows = [];
  const base = fakeArtifact(pollOpts({ rows }));
  // createRow delivers the new vote through the watch BEFORE acknowledging, which is the
  // schedule that redraws the buttons while busy is still true.
  base.art.db.createRow = async (d, cells) => {
    base.calls.push(['createRow', d, cells]);
    rows.push({ id: 'r1', cells: { ...cells, 'Created by': ['@me:x'] } });
    base.fire();
    await new Promise((ok) => setTimeout(ok, 0));
    return { ok: true, rowId: 'r1' };
  };
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('choices').buttons()[0].click();   // vote Tea
  await settle(40);
  check('poll/watch-during-save-leaves-controls-usable', () => {
    const bs = dom.byId.get('choices').buttons();
    const labels = bs.map((b) => b.textContent + ':' + b.disabled);
    // The voter's own choice stays disabled (it is already theirs); the OTHER must be clickable.
    const other = bs.find((b) => /Coffee/.test(b.textContent));
    assert.ok(other, 'the other choice is still rendered: ' + labels.join(', '));
    assert.equal(other.disabled, false, 'not stuck disabled after the save: ' + labels.join(', '));
  });
  check('poll/watch-during-save-counts-the-vote', () =>
    assert.match(dom.byId.get('sub').textContent, /1 vote/));
}
/* P2: a refused comment is retried on its own and reported, never silently dropped. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  const base = fakeArtifact(pollOpts({ rows }));
  const seen = [];
  base.art.db.write = async (d, row, prop, value) => {
    seen.push([prop, value]);
    base.calls.push(['write', d, row, prop, value]);
    return prop === 'Comment' ? { ok: false, reason: 'not allowed by the viewer' } : { ok: true };
  };
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('comment').value = 'New comment';
  dom.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await settle(40);
  check('poll/refused-comment-is-reported', () => {
    const text = dom.body.textContent;
    assert.match(text, /comment was not/i, 'the refusal is shown: ' + text.slice(0, 200));
    assert.match(text, /not allowed by the viewer/, 'with the reason');
  });
  check('poll/refused-comment-keeps-the-text', () =>
    assert.equal(dom.byId.get('comment').value, 'New comment'));
  check('poll/comment-retry-does-not-rewrite-choice', () => {
    const choiceWrites = seen.filter(([prop]) => prop === 'Choice').length;
    assert.equal(choiceWrites, 1, 'Choice written once: ' + JSON.stringify(seen));
    assert.ok(seen.some(([prop]) => prop === 'Comment'), 'the comment was attempted');
  });
}
/* A rate-limited comment retries (GUIDE's retryable reasons) instead of reporting at once. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  const base = fakeArtifact(pollOpts({ rows }));
  let commentTries = 0;
  base.art.db.write = async (d, row, prop, value) => {
    base.calls.push(['write', d, row, prop, value]);
    if (prop !== 'Comment') return { ok: true };
    commentTries += 1;
    return commentTries === 1 ? { ok: false, reason: 'rate limit' } : { ok: true };
  };
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('comment').value = 'Retry me';
  dom.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await new Promise((ok) => setTimeout(ok, 1800));        // save() waits 1500ms between attempts
  await settle();
  check('poll/rate-limited-comment-retries', () => {
    assert.equal(commentTries, 2, 'tried twice');
    assert.ok(!/comment was not/i.test(dom.body.textContent), 'and is not reported as failed');
  });
  check('poll/rate-limited-comment-retry-does-not-rewrite-choice', () => {
    const choiceWrites = base.calls.filter((c) => c[0] === 'write' && c[3] === 'Choice').length;
    assert.equal(choiceWrites, 1, JSON.stringify(base.calls));
  });
}
/* A comment that saves is cleared, so it is not sent twice by accident. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  const r = await run('poll', pollOpts({ rows }));
  r.byId.get('comment').value = 'Good one';
  r.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await settle(30);
  check('poll/saved-comment-is-cleared', () => assert.equal(r.byId.get('comment').value, ''));
  check('poll/saved-comment-is-written', () =>
    assert.ok(r.calls.some((c) => c[0] === 'write' && c[3] === 'Comment' && c[4] === 'Good one'),
      JSON.stringify(r.calls)));
}

/* ================= Codex re-review of 1ae40ceb: the same window, everywhere ==== */
/* A fake whose write/createRow delivers a watch snapshot BEFORE acknowledging, which is the
 * schedule that redraws controls while the save is still pending. */
const withWatchDuringSave = (template, opts, mutate) => {
  const base = fakeArtifact(opts);
  const html = readFileSync(join(TPL, template, 'page.html'), 'utf8');
  const dom = makeDom(html);
  const wrap = (name) => {
    const inner = base.art.db[name];
    base.art.db[name] = async (...a) => {
      base.calls.push([name, ...a]);
      if (mutate) mutate(a);
      base.fire();                                   // the watch lands first
      await new Promise((ok) => setTimeout(ok, 0));
      return name === 'createRow' ? { ok: true, rowId: 'rNew' } : { ok: true };
    };
    return inner;
  };
  wrap('createRow');
  wrap('write');
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: opts.hash || '' }, setTimeout, clearTimeout, Promise, Date, Number, Math,
    String, Object, Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  return { ...dom, ...base, text: () => dom.body.textContent };
};

/* turn-game: after a winning move and a New game, the controls must not be stuck. */
{
  const rows = [mv('m1', 1, 'X', 1, 1), mv('m2', 1, 'O', 2, 1), mv('m3', 1, 'X', 1, 2),
                mv('m4', 1, 'O', 2, 2), mv('m5', 1, 'X', 1, 3)];
  const r = withWatchDuringSave('turn-game', gameOpts({ rows, state: SEATED }),
    ([, cells]) => { if (cells && cells.Game) rows.push({ id: 'm9', cells }); });
  await settle();
  const again = r.byId.get('seats').buttons().find((b) => b.textContent === 'New game');
  again.click();
  await settle(40);
  check('game/new-game-not-stuck-after-a-watch-during-save', () => {
    const live = r.byId.get('seats').buttons().find((b) => b.textContent === 'New game');
    assert.ok(live, 'the button is still rendered');
    // Game 2 is empty, so New game is correctly disabled now — what must NOT happen is the
    // board staying locked for the player whose turn it is.
    const cells = r.byId.get('board').buttons();
    const playable = cells.filter((c) => !c.disabled).length;
    assert.ok(playable > 0, 'X can move in the new game; playable=' + playable);
  });
}
/* turn-game: a REFUSED move must leave the board usable — it is still that player's turn, so
 * this is the case where the post-save redraw is observable (after a SUCCESSFUL move the board
 * is legitimately locked for the mover either way, which is why that made a poor test). */
{
  const rows = [];
  const base = fakeArtifact(gameOpts({ rows, state: SEATED }));
  base.art.db.createRow = async (d, cells) => {
    base.calls.push(['createRow', d, cells]);
    base.fire();                                     // a redraw lands while busy is true
    await new Promise((ok) => setTimeout(ok, 0));
    return { ok: false, reason: 'not allowed by the viewer' };
  };
  const html = readFileSync(join(TPL, 'turn-game', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('board').children[0].click();
  await settle(40);
  check('game/refused-move-leaves-the-board-usable', () => {
    const playable = dom.byId.get('board').buttons().filter((c) => !c.disabled).length;
    assert.equal(playable, 9, 'still X to move on an empty board; playable=' + playable);
  });
  check('game/refused-move-is-reported', () =>
    assert.match(dom.body.textContent, /not allowed by the viewer/));
}

/* tracker: a snapshot during a pending save must NOT re-enable Add, so a second click cannot
 * queue a duplicate row. */
{
  const rows = [];
  let release = null;
  const held = new Promise((ok) => { release = ok; });
  const base = fakeArtifact(trackerOpts({ rows }));
  base.art.db.createRow = async (d, cells) => {
    base.calls.push(['createRow', d, cells]);
    base.fire();                                     // a redraw arrives mid-save
    await held;
    return { ok: true, rowId: 'r1' };
  };
  const html = readFileSync(join(TPL, 'tracker', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('title').value = 'Only once';
  dom.byId.get('add').click();
  await settle(6);
  check('tracker/add-stays-disabled-through-a-redraw', () =>
    assert.equal(dom.byId.get('add').disabled, true));
  const second = dom.byId.get('add').click();
  await settle(6);
  release({ ok: true });
  await settle(20);
  check('tracker/no-duplicate-row-from-a-second-click', () => {
    assert.equal(second, false, 'the second click was refused');
    assert.equal(base.calls.filter((c) => c[0] === 'createRow').length, 1, JSON.stringify(base.calls));
  });
  check('tracker/add-usable-again-after-the-save', () =>
    assert.equal(dom.byId.get('add').disabled, false));
}
/* planner: the same guard on its Add. */
{
  const rows = [];
  let release = null;
  const held = new Promise((ok) => { release = ok; });
  const base = fakeArtifact(plannerOpts({ rows }));
  base.art.db.createRow = async (d, cells) => {
    base.calls.push(['createRow', d, cells]);
    base.fire();
    await held;
    return { ok: true, rowId: 'r1' };
  };
  const html = readFileSync(join(TPL, 'planner', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: 'week=2026-11-09' }, setTimeout, clearTimeout, Promise, Date, Number, Math,
    String, Object, Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('title').value = 'Only once';
  dom.byId.get('add').click();
  await settle(6);
  check('planner/add-stays-disabled-through-a-redraw', () =>
    assert.equal(dom.byId.get('add').disabled, true));
  const second = dom.byId.get('add').click();
  await settle(6);
  release({ ok: true });
  await settle(20);
  check('planner/no-duplicate-row-from-a-second-click', () => {
    assert.equal(second, false);
    assert.equal(base.calls.filter((c) => c[0] === 'createRow').length, 1, JSON.stringify(base.calls));
  });
}
/* Text typed DURING a retry is never erased: the clear only fires if the box still holds what
 * was submitted. */
for (const [template, opts, field, typed] of [
  ['tracker', trackerOpts({ rows: [] }), 'title', 'typed during the retry'],
  ['planner', plannerOpts({ rows: [] }), 'title', 'typed during the retry'],
]) {
  const base = fakeArtifact({ ...opts, writeAnswers: [
    { ok: false, reason: 'waiting for the viewer’s answer' }, { ok: true, rowId: 'r1' }] });
  const html = readFileSync(join(TPL, template, 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: opts.hash || '' }, setTimeout, clearTimeout, Promise, Date, Number, Math,
    String, Object, Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get(field).value = 'first item';
  dom.byId.get('add').click();
  await settle(6);
  dom.byId.get(field).value = typed;                 // the reader types while it retries
  await new Promise((ok) => setTimeout(ok, 1800));
  await settle();
  check(`${template}/retry-does-not-erase-new-text`, () =>
    assert.equal(dom.byId.get(field).value, typed));
}
/* And the ordinary case still clears. */
{
  const r = await run('tracker', trackerOpts({ rows: [] }));
  r.byId.get('title').value = 'Clear me';
  r.byId.get('add').click();
  await settle(20);
  check('tracker/successful-add-clears-the-box', () => assert.equal(r.byId.get('title').value, ''));
}

/* ========== Codex review of 39b4dcb4: overlapping renders, integer coordinates ===== */
/* Mounts a template whose personId is SLOW while `slow.on` is true, so a render started then
 * finishes after a later one. */
const mountWithSlowPersonId = (template, opts) => {
  const slow = { on: true };
  const base = fakeArtifact(opts);
  const fast = base.art.personId;
  base.art.personId = async (cell) => {
    if (slow.on) await new Promise((ok) => setTimeout(ok, 60));
    return fast(cell);
  };
  let fire = null;
  base.art.db.onChange = (d, fn) => { fire = fn; return () => {}; };
  const html = readFileSync(join(TPL, template, 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  return { ...dom, ...base, slow, fire: (snap) => fire(snap) };
};
const wait = (ms) => new Promise((ok) => setTimeout(ok, ms));

/* poll: an older render that resolves last must not overwrite the newer snapshot's result. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  const r = mountWithSlowPersonId('poll', pollOpts({ rows }));
  await settle(4);                                   // the initial render is stuck in personId
  r.slow.on = false;
  r.fire({ id: 'd1', name: 'Poll — Votes', props: POLL_PROPS,
    rows: [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Coffee', 'Created by': ['@me:x'] } }] });
  await settle(10);                                  // the newer render finishes first
  await wait(120);                                   // then the older one resolves
  await settle(10);
  check('poll/older-render-does-not-overwrite-newer', () => {
    assert.match(r.byId.get('sub').textContent, /you voted Coffee/);
    const coffee = r.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent));
    assert.match(coffee.textContent, /your vote/);
  });
}
/* tracker: a render still resolving when the database is removed must not bring the page back. */
{
  const rows = [{ id: 'r1', cells: { Item: 'Mend fence', Status: 'Requested', 'Created by': ['@me:x'] } }];
  const r = mountWithSlowPersonId('tracker', trackerOpts({ rows }));
  await settle(4);                                   // the initial render is stuck in personId
  r.fire(null);                                      // the database is removed meanwhile
  await settle(4);
  await wait(120);                                   // the stale render resolves after removal
  await settle(10);
  check('tracker/removal-wins-over-a-stale-render', () => {
    assert.match(r.body.textContent, /was removed from this room/);
    assert.equal(r.byId.get('add').disabled, true, 'Add stays disabled');
    assert.ok(!/Mend fence/.test(r.byId.get('list').textContent), 'the old rows are not redrawn');
  });
}
/* tracker: same ordering rule as poll, on the "yours" marker. */
{
  const rows = [{ id: 'r1', cells: { Item: 'Old', Status: 'Requested', 'Created by': ['@other:x'] } }];
  const r = mountWithSlowPersonId('tracker', trackerOpts({ rows }));
  await settle(4);
  r.slow.on = false;
  r.fire({ id: 'd1', name: 'Tracker — Items', props: TRACKER_PROPS,
    rows: [{ id: 'r2', cells: { Item: 'New', Status: 'Requested', 'Created by': ['@me:x'] } }] });
  await settle(10);
  await wait(120);
  await settle(10);
  check('tracker/older-render-does-not-overwrite-newer', () => {
    const list = r.byId.get('list').textContent;
    assert.match(list, /New/);
    assert.ok(!/Old/.test(list), 'the superseded snapshot is not drawn: ' + list);
    assert.match(list, /Added by you/);
  });
}
/* turn-game: a fractional coordinate is not a move, so it cannot pass the turn invisibly. */
{
  const rows = [{ id: 'm1', cells: { Game: 1, Seat: 'X', Row: 1.5, Col: 1 } }];
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/fractional-row-is-not-a-move', () => {
    assert.match(r.byId.get('sub').textContent, /0 moves/);
    assert.match(r.byId.get('status').textContent, /Your turn as X/);
    assert.ok(r.byId.get('board').children.every((c) => c.textContent === ''), 'no mark drawn');
  });
}
{
  const rows = [{ id: 'm1', cells: { Game: 1, Seat: 'X', Row: 2, Col: '2.0' } }];
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/integer-valued-string-still-counts', () =>
    assert.equal(r.byId.get('board').children[4].textContent, 'X'));
}

/* ===================== Codex review of aba958a7 + the audit it prompted ===== */
/* turn-game: X then O before either write resolves must not seat one viewer twice. */
{
  let release = null;
  const held = new Promise((ok) => { release = ok; });
  const base = fakeArtifact(gameOpts({ rows: [], state: {} }));
  const sets = [];
  const store = new Map();
  base.art.state.set = async (k, v) => { sets.push(k); await held; store.set(k, v); return { ok: true }; };
  base.art.state.get = async (k) => store.get(k);
  const html = readFileSync(join(TPL, 'turn-game', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  const seatButtons = () => dom.byId.get('seats').buttons();
  seatButtons().find((b) => b.textContent === 'Play as X').click();
  await settle(4);
  const second = seatButtons().find((b) => b.textContent === 'Play as O');
  const clicked = second ? second.click() : false;
  release();
  await settle(20);
  check('game/one-viewer-cannot-claim-both-seats', () => {
    assert.equal(clicked, false, 'the second seat button was refused');
    assert.deepEqual(sets, ['seat.X'], 'only one seat written: ' + JSON.stringify(sets));
  });
}
/* turn-game: a removed database stays removed. */
{
  const r = await run('turn-game', gameOpts({ rows: [mv('m1', 1, 'X', 1, 1)], state: SEATED }));
  r.fire(null);
  await settle();
  check('game/removal-clears-the-board-and-seats', () => {
    assert.match(r.body.textContent, /was removed from this room/);
    assert.equal(r.byId.get('board').children.length, 0);
    assert.equal(r.byId.get('seats').children.length, 0);
  });
}
/* planner: navigating weeks after removal must not redraw the deleted rows. */
{
  const rows = [{ id: 'r1', cells: { Item: 'Dentist', Day: '2026-11-12', Slot: 'Morning', State: 'Planned' } }];
  const r = await run('planner', plannerOpts({ rows }));
  r.fire(null);
  await settle();
  r.byId.get('next').click();
  r.byId.get('prev').click();
  await settle();
  check('planner/navigation-after-removal-stays-removed', () => {
    assert.match(r.body.textContent, /was removed from this room/);
    assert.ok(!/Dentist/.test(r.byId.get('week').textContent), 'deleted rows are not redrawn');
    assert.equal(r.byId.get('add').disabled, true);
  });
}
/* tracker, planner: a write that settles AFTER the database is removed must not re-enable Add. */
for (const [template, opts] of [['tracker', trackerOpts({ rows: [] })], ['planner', plannerOpts({ rows: [] })]]) {
  let release = null;
  const held = new Promise((ok) => { release = ok; });
  let fire = null;
  const base = fakeArtifact(opts);
  base.art.db.onChange = (d, fn) => { fire = fn; return () => {}; };
  base.art.db.createRow = async (d, cells) => { base.calls.push(['createRow', d, cells]); await held; return { ok: true, rowId: 'r1' }; };
  const html = readFileSync(join(TPL, template, 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: opts.hash || '' }, setTimeout, clearTimeout, Promise, Date, Number, Math,
    String, Object, Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('title').value = 'In flight';
  dom.byId.get('add').click();
  await settle(4);
  fire(null);                                        // removed while the write is pending
  await settle(4);
  release();
  await settle(20);
  check(`${template}/pending-write-after-removal-keeps-add-disabled`, () => {
    assert.equal(dom.byId.get('add').disabled, true);
    assert.match(dom.body.textContent, /was removed from this room/);
  });
}
/* tracker: "today" is the reader's calendar day, not UTC's. At 19:00 in Los Angeles on Nov 11
 * it is already Nov 12 in UTC; an item due Nov 11 is due today, not overdue. */
{
  const rows = [{ id: 'r1', cells: { Item: 'Due today', Status: 'Requested', Due: '2026-11-11' } },
                { id: 'r2', cells: { Item: 'Due yesterday', Status: 'Requested', Due: '2026-11-10' } }];
  const r = await run('tracker', trackerOpts({ rows, now: Date.parse('2026-11-12T03:00:00Z') }));
  const meta = (name) => r.byId.get('list').children
    .find((li) => li.textContent.includes(name)).children.find((c) => /\bmeta\b/.test(c.className));
  check('tracker/due-today-is-not-overdue-west-of-utc', () =>
    assert.ok(!/\bover\b/.test(meta('Due today').className), meta('Due today').className));
  check('tracker/due-yesterday-is-overdue', () =>
    assert.match(meta('Due yesterday').className, /\bover\b/));
}

/* ============ Codex review of 71317680: replay stops at the win; __proto__ keys ===== */
/* turn-game: X completes the bottom row; an O row appended afterwards completes the top row.
 * The winner is the player who finished first, and the later row is not replayed. */
{
  const rows = [
    mv('m1', 1, 'X', 3, 1), mv('m2', 1, 'O', 1, 1), mv('m3', 1, 'X', 3, 2),
    mv('m4', 1, 'O', 1, 2), mv('m5', 1, 'X', 3, 3),   // X wins the bottom row here
    mv('m6', 1, 'O', 1, 3),                           // appended afterwards: must not count
  ];
  const r = await run('turn-game', gameOpts({ rows, state: SEATED }));
  check('game/winner-is-whoever-finished-first', () =>
    assert.match(r.byId.get('status').textContent, /^X wins\.$/));
  check('game/moves-after-the-win-are-not-replayed', () => {
    assert.equal(r.byId.get('board').children[2].textContent, '', 'the appended O is not drawn');
    assert.match(r.byId.get('sub').textContent, /5 moves/);
  });
}
/* poll: an option named __proto__ is counted like any other name. */
{
  const props = [
    { id: 't', name: 'Voter', type: 'title' },
    { id: 'c', name: 'Choice', type: 'select', options: [{ name: '__proto__' }, { name: 'constructor' }, { name: 'Tea' }] },
    { id: 'm', name: 'Comment', type: 'text' },
    { id: 'b', name: 'Created by', type: 'created_by' },
  ];
  const rows = [
    { id: 'r1', cells: { Voter: 'A', Choice: '__proto__', 'Created by': ['@a:x'] } },
    { id: 'r2', cells: { Voter: 'B', Choice: '__proto__', 'Created by': ['@b:x'] } },
    { id: 'r3', cells: { Voter: 'C', Choice: 'constructor', 'Created by': ['@c:x'] } },
    { id: 'r4', cells: { Voter: 'D', Choice: 'Tea', 'Created by': ['@d:x'] } },
  ];
  const r = await run('poll', { dbName: 'Poll — Votes', props, rows });
  check('poll/proto-named-option-is-counted', () => {
    assert.match(r.byId.get('sub').textContent, /^4 votes/);
    const text = r.byId.get('results').textContent;
    assert.match(text, /__proto__.*2.*50%/s, text.slice(0, 160));
    assert.match(text, /constructor.*1.*25%/s);
  });
}
/* tracker: a status named __proto__ gets a real chip and does not crash the sort. */
{
  const props = TRACKER_PROPS.map((p) => (p.name === 'Status'
    ? { ...p, options: [{ name: '__proto__', group: 'todo' }, { name: 'Done', group: 'done' }] }
    : p));
  const rows = [{ id: 'r1', cells: { Item: 'Odd status', Status: '__proto__' } },
                { id: 'r2', cells: { Item: 'Normal', Status: 'Done' } }];
  const r = await run('tracker', { dbName: 'Tracker — Items', props, rows });
  check('tracker/proto-named-status-renders', () => {
    const list = r.byId.get('list').textContent;
    assert.match(list, /Odd status/);
    assert.match(list, /__proto__/);
    const chip = r.byId.get('list').all().find((e) => /^chip /.test(e.className || ''));
    assert.match(chip.className, /^chip (todo|doing|done)$/, chip.className);
  });
  check('tracker/proto-named-status-counts', () =>
    assert.match(r.byId.get('counts').textContent, /__proto__: 1/));
}

/* ================== Codex review of e1fb1724: stale options, lost errors ===== */
/* tracker, planner: an option renamed or removed in the room leaves the picker. */
{
  const r = await run('tracker', trackerOpts({ rows: [] }));
  check('tracker/picker-starts-with-every-status', () =>
    assert.deepEqual(r.byId.get('status').options.map((o) => o.value),
      ['Requested', 'Working', 'Done', 'Blocked']));
  r.byId.get('status').value = 'Working';
  const renamed = TRACKER_PROPS.map((p) => (p.name === 'Status'
    ? { ...p, options: [{ name: 'Requested', group: 'todo' }, { name: 'In progress', group: 'doing' }] }
    : p));
  r.fire({ id: 'd1', name: 'Tracker — Items', props: renamed, rows: [] });
  await settle(10);
  check('tracker/renamed-status-replaces-the-stale-option', () => {
    const values = r.byId.get('status').options.map((o) => o.value);
    assert.deepEqual(values, ['Requested', 'In progress'], values.join(','));
    assert.ok(!values.includes('Working'), 'the removed option is gone');
  });
  check('tracker/selection-falls-back-when-its-option-vanished', () =>
    assert.equal(r.byId.get('status').value, 'Requested'));
}
{
  const r = await run('planner', plannerOpts({ rows: [] }));
  const renamed = PLANNER_PROPS.map((p) => (p.name === 'Slot'
    ? { ...p, options: [{ name: 'Early' }, { name: 'Late' }] } : p));
  r.fire({ id: 'd1', name: 'Planner — Items', props: renamed, rows: [] });
  await settle(10);
  check('planner/renamed-slot-replaces-the-stale-options', () =>
    assert.deepEqual(r.byId.get('slot').options.map((o) => o.value), ['Early', 'Late']));
}
/* tracker: a surviving selection is kept across a redraw. */
{
  const r = await run('tracker', trackerOpts({ rows: [] }));
  r.byId.get('status').value = 'Done';
  r.fire();
  await settle(10);
  check('tracker/surviving-selection-is-kept', () => assert.equal(r.byId.get('status').value, 'Done'));
}
/* poll: a Choice column emptied of options clears the tally and the buttons. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'A', Choice: 'Tea', 'Created by': ['@a:x'] } }];
  const r = await run('poll', pollOpts({ rows }));
  check('poll/tally-shown-while-options-exist', () => assert.match(r.byId.get('results').textContent, /Tea/));
  const emptied = POLL_PROPS.map((p) => (p.name === 'Choice' ? { ...p, options: [] } : p));
  r.fire({ id: 'd1', name: 'Poll — Votes', props: emptied, rows });
  await settle(10);
  check('poll/emptied-options-clear-the-stale-tally', () => {
    assert.equal(r.byId.get('results').textContent, '', 'results cleared');
    assert.equal(r.byId.get('choices').children.length, 0, 'no votable buttons left');
    assert.match(r.body.textContent, /no options yet/);
  });
}
/* turn-game: a failed seat claim or release reports it — the post-save re-render clears notes,
 * so the message has to be shown after it. This regressed when that re-render was added. */
for (const [label, failing] of [['claim', 'set'], ['release', 'delete']]) {
  const state = failing === 'delete' ? { 'seat.X': { id: 'u1', name: 'Tester' } } : {};
  const base = fakeArtifact(gameOpts({ rows: [], state }));
  base.art.state[failing] = async () => { throw new Error('storage refused the seat'); };
  const html = readFileSync(join(TPL, 'turn-game', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  const btn = dom.byId.get('seats').buttons()
    .find((b) => (failing === 'set' ? b.textContent === 'Play as X' : /^Leave seat/.test(b.textContent)));
  assert.ok(btn, `the ${label} button exists`);
  btn.click();
  await settle(20);
  check(`game/failed-seat-${label}-is-reported`, () =>
    assert.match(dom.body.textContent, /storage refused the seat/, dom.body.textContent.slice(0, 160)));
}

/* ============== Codex review of e8058a2f: a throw must not lock the page ===== */
/* A rejecting write (not a {ok:false} refusal — a real rejection) must still leave the page
 * usable. The bridge documents write/createRow as never rejecting, but a page that trusts that
 * is one bridge bug away from a dead UI. */
const mountRejecting = (template, opts, method) => {
  const base = fakeArtifact(opts);
  base.art.db[method] = async () => { throw new Error('bridge blew up'); };
  const html = readFileSync(join(TPL, template, 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: opts.hash || '' }, setTimeout, clearTimeout, Promise, Date, Number, Math,
    String, Object, Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  return { ...dom, ...base };
};
for (const [template, opts] of [['tracker', trackerOpts({ rows: [] })], ['planner', plannerOpts({ rows: [] })]]) {
  const r = mountRejecting(template, opts, 'createRow');
  await settle();
  r.byId.get('title').value = 'Boom';
  r.byId.get('add').click();
  await settle(30);
  check(`${template}/a-rejected-write-does-not-lock-add`, () =>
    assert.equal(r.byId.get('add').disabled, false, 'Add is usable again'));
  check(`${template}/a-rejected-write-is-reported`, () =>
    assert.match(r.body.textContent, /bridge blew up/));
  // And a second attempt is actually possible.
  r.byId.get('title').value = 'Again';
  const second = r.byId.get('add').click();
  await settle(20);
  check(`${template}/a-second-attempt-is-possible`, () => assert.equal(second, true));
}
{
  const r = mountRejecting('poll', pollOpts({ rows: [] }), 'createRow');
  await settle();
  r.byId.get('choices').buttons()[0].click();
  await settle(30);
  check('poll/a-rejected-vote-does-not-lock-the-choices', () => {
    const stuck = r.byId.get('choices').buttons().filter((b) => b.disabled).length;
    assert.equal(stuck, 0, 'no choice left disabled');
  });
  check('poll/a-rejected-vote-is-reported', () => assert.match(r.body.textContent, /bridge blew up/));
}
{
  const r = mountRejecting('turn-game', gameOpts({ rows: [], state: SEATED }), 'createRow');
  await settle();
  r.byId.get('board').children[0].click();
  await settle(30);
  check('game/a-rejected-move-does-not-lock-the-board', () => {
    const playable = r.byId.get('board').buttons().filter((c) => !c.disabled).length;
    assert.equal(playable, 9, 'the board is usable again; playable=' + playable);
  });
  check('game/a-rejected-move-is-reported', () => assert.match(r.body.textContent, /bridge blew up/));
}
/* poll: the vote row deleted between a refused comment write and its retry — the case
 * sonichi-dot reported as still reproducing. It must not throw, and must not lock the page. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  const base = fakeArtifact(pollOpts({ rows }));
  let commentTries = 0;
  base.art.db.write = async (d, row, prop, value) => {
    base.calls.push(['write', d, row, prop, value]);
    if (prop !== 'Comment') return { ok: true };
    commentTries += 1;
    if (commentTries === 1) {
      rows.length = 0;                                 // the vote is deleted mid-retry
      return { ok: false, reason: 'rate limit' };
    }
    return { ok: true };
  };
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('comment').value = 'A note';
  dom.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await new Promise((ok) => setTimeout(ok, 1800));
  await settle(20);
  check('poll/vote-deleted-mid-comment-retry-does-not-lock', () => {
    const stuck = dom.byId.get('choices').buttons().filter((b) => b.disabled).length;
    assert.equal(stuck, 0, 'no choice left disabled');
  });
  check('poll/vote-deleted-mid-comment-retry-is-reported', () =>
    assert.match(dom.body.textContent, /no longer in this room/));
  check('poll/vote-deleted-mid-comment-retry-keeps-the-text', () =>
    assert.equal(dom.byId.get('comment').value, 'A note'));
}
/* poll: a comment refused after the vote saved can be retried on its own. */
{
  const rows = [{ id: 'r1', cells: { Voter: 'Tester', Choice: 'Tea', 'Created by': ['@me:x'] } }];
  const base = fakeArtifact(pollOpts({ rows }));
  let fail = true;
  base.art.db.write = async (d, row, prop, value) => {
    base.calls.push(['write', d, row, prop, value]);
    if (prop !== 'Comment') return { ok: true };
    if (fail) return { ok: false, reason: 'not allowed by the viewer' };
    rows[0].cells.Comment = value;
    return { ok: true };
  };
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  await settle();
  dom.byId.get('comment').value = 'My note';
  dom.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await settle(30);
  check('poll/failed-comment-leaves-a-way-to-retry', () => {
    const btn = dom.byId.get('saveComment');
    assert.equal(btn.hidden, false, 'Save comment is offered');
    assert.equal(btn.disabled, false, 'and is usable');
    assert.equal(dom.byId.get('comment').value, 'My note', 'the text is kept');
  });
  fail = false;
  dom.byId.get('saveComment').click();
  await settle(30);
  check('poll/retried-comment-saves-without-revoting', () => {
    const comments = base.calls.filter((c) => c[3] === 'Comment');
    assert.equal(comments.length, 2, 'the comment was written twice: ' + JSON.stringify(comments));
    const choices = base.calls.filter((c) => c[3] === 'Choice');
    assert.equal(choices.length, 1, 'and the vote was not recast');
    assert.equal(dom.byId.get('comment').value, '', 'the box is cleared on success');
  });
}

/* ========= Chi's change at 1bafa08b: a THROWN comment write is reported and kept ===== */
const mountPollWith = (rows, write) => {
  const base = fakeArtifact(pollOpts({ rows }));
  let fire = null;
  base.art.db.onChange = (d, fn) => { fire = fn; return () => {}; };
  base.art.db.write = (...a) => { base.calls.push(['write', ...a]); return write(...a); };
  const html = readFileSync(join(TPL, 'poll', 'page.html'), 'utf8');
  const dom = makeDom(html);
  const sandbox = { window: { artifact: base.art, addEventListener: () => {} }, document: dom.document,
    location: { hash: '' }, setTimeout, clearTimeout, Promise, Date, Number, Math, String, Object,
    Array, JSON, console, RegExp, Error };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(scriptOf(html)).runInContext(sandbox);
  return { ...dom, ...base, fire: (snap) => fire(snap) };
};
const own = (id, choice, extra = {}) => ({ id, cells: { Voter: 'Tester', Choice: choice, 'Created by': ['@me:x'], ...extra } });

/* After the vote saves, the Comment write REJECTS: the warn note shows and the text stays. */
{
  const r = mountPollWith([own('r1', 'Tea')], async (d, row, prop) => {
    if (prop === 'Comment') throw new Error('bridge dropped the comment');
    return { ok: true };
  });
  await settle();
  r.byId.get('comment').value = 'Keep me';
  r.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await settle(30);
  check('poll/thrown-comment-after-vote-is-reported', () => {
    assert.match(r.body.textContent, /vote was saved, but the comment was not/);
    assert.match(r.body.textContent, /bridge dropped the comment/);
  });
  check('poll/thrown-comment-after-vote-keeps-the-text', () =>
    assert.equal(r.byId.get('comment').value, 'Keep me'));
  check('poll/thrown-comment-after-vote-leaves-save-comment', () => {
    const b = r.byId.get('saveComment');
    assert.equal(b.hidden, false);
    assert.equal(b.disabled, false);
  });
}
/* The same throw through the Save comment button. */
{
  const r = mountPollWith([own('r1', 'Tea')], async (d, row, prop) => {
    if (prop === 'Comment') throw new Error('bridge dropped the comment');
    return { ok: true };
  });
  await settle();
  r.byId.get('comment').value = 'Keep me too';
  r.byId.get('comment').dispatch('input');
  r.byId.get('saveComment').click();
  await settle(30);
  check('poll/thrown-save-comment-is-reported-and-kept', () => {
    assert.match(r.body.textContent, /comment was not saved: bridge dropped the comment/);
    assert.equal(r.byId.get('comment').value, 'Keep me too');
  });
}
/* sonichi-dot: a newer own-vote row arriving before the retry must NOT receive the older text.
 * The retry stays on the row the vote was cast on. */
{
  const rows = [own('r1', 'Tea')];
  let tries = 0;
  const r = mountPollWith(rows, async (d, row, prop) => {
    if (prop !== 'Comment') return { ok: true };
    tries += 1;
    if (tries === 1) {
      // A newer vote of this viewer lands, with its own newer comment.
      rows.push(own('r2', 'Coffee', { Comment: 'the newer comment' }));
      r.fire({ id: 'd1', name: 'Poll — Votes', props: POLL_PROPS, rows: rows.slice() });
      return { ok: false, reason: 'rate limit' };
    }
    return { ok: true };
  });
  await settle();
  r.byId.get('comment').value = 'the older comment';
  r.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await new Promise((ok) => setTimeout(ok, 1800));
  await settle(30);
  check('poll/comment-retry-stays-on-its-own-row', () => {
    const commentWrites = r.calls.filter((c) => c[0] === 'write' && c[3] === 'Comment');
    assert.ok(commentWrites.length >= 2, 'it retried: ' + JSON.stringify(commentWrites));
    assert.ok(commentWrites.every((c) => c[2] === 'r1'), 'every attempt targets r1: ' + JSON.stringify(commentWrites));
    assert.ok(!commentWrites.some((c) => c[2] === 'r2'), 'the newer row is never overwritten');
  });
}

/* ===== sonichi-dot at d50372e5: the target must span the Choice await too ===== */
/* Hold the Choice write; while it is pending a newer own row r2 (with its own comment) arrives.
 * The comment must go to r1, the row the Choice write edited — never to r2. */
{
  const rows = [own('r1', 'Tea')];
  let release = null;
  const held = new Promise((ok) => { release = ok; });
  const r = mountPollWith(rows, async (d, row, prop) => {
    if (prop === 'Choice') { await held; return { ok: true }; }
    return { ok: true };
  });
  await settle();
  r.byId.get('comment').value = 'older text';
  r.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await settle(6);                                   // the Choice write is now pending
  rows.push(own('r2', 'Coffee', { Comment: 'newer comment' }));
  r.fire({ id: 'd1', name: 'Poll — Votes', props: POLL_PROPS, rows: rows.slice() });
  await settle(10);                                  // the re-render moved myRow to r2
  release();
  await settle(30);
  check('poll/comment-follows-the-row-the-choice-write-edited', () => {
    const choice = r.calls.find((c) => c[3] === 'Choice');
    const comment = r.calls.find((c) => c[3] === 'Comment');
    assert.equal(choice[2], 'r1', 'the vote edited r1');
    assert.ok(comment, 'the comment was written');
    assert.equal(comment[2], 'r1', 'and to r1, not r2: ' + JSON.stringify(r.calls));
  });
}
/* The voted row is DELETED during the Choice await: report it and keep the text, never clear. */
{
  const rows = [own('r1', 'Tea')];
  let release = null;
  const held = new Promise((ok) => { release = ok; });
  const r = mountPollWith(rows, async (d, row, prop) => {
    if (prop === 'Choice') { await held; return { ok: true }; }
    return { ok: true };
  });
  await settle();
  r.byId.get('comment').value = 'unsaved text';
  r.byId.get('choices').buttons().find((b) => /Coffee/.test(b.textContent)).click();
  await settle(6);
  rows.length = 0;                                   // r1 is deleted while the vote is pending
  r.fire({ id: 'd1', name: 'Poll — Votes', props: POLL_PROPS, rows: [] });
  await settle(10);
  release();
  await settle(30);
  check('poll/row-deleted-during-the-vote-reports-the-comment', () =>
    assert.match(r.body.textContent, /no longer in this room/));
  check('poll/row-deleted-during-the-vote-keeps-the-text', () =>
    assert.equal(r.byId.get('comment').value, 'unsaved text'));
  check('poll/row-deleted-during-the-vote-writes-no-comment', () =>
    assert.ok(!r.calls.some((c) => c[3] === 'Comment'), JSON.stringify(r.calls)));
}

/* ============== research-desk and task-board: two databases, the viewer's own agents ===== */
// A fake with several databases and artifact.agent, for the two templates that ask agents.
const fakeMulti = (opts = {}) => {
  const calls = [];
  const dbs = opts.dbs;                                 // {id: {name, props, rows}}
  const watchers = {};
  let answers = (opts.writeAnswers || []).slice();
  const answer = () => (answers.length ? answers.shift() : { ok: true });
  const ACCOUNTS = opts.accounts || { '@me:x': 'u1', '@desk.agent:x': 'u2', '@other:x': 'u3' };
  const MEMBERS = opts.members || [
    { id: 'u1', name: 'Tester', avatar: null, kind: 'human' },
    { id: 'u2', name: 'Desk Agent', avatar: null, kind: 'agent' },
    { id: 'u3', name: 'Other', avatar: null, kind: 'human' },
  ];
  return {
    calls,
    fire: (dbId, s) => watchers[dbId] && watchers[dbId](s === undefined ? dbs[dbId] : s),
    art: {
      ready: Promise.resolve(),
      me: opts.me || { id: 'u1', name: 'Tester', kind: 'human' },
      app: opts.app === null ? null : { id: 'a1', name: 'T', database: opts.primary },
      now: () => Date.parse('2026-11-11T12:00:00Z'),
      members: async () => { if (opts.consent === false) throw new Error('page permission required'); return MEMBERS; },
      personId: async (cell) => {
        if (opts.consent === false) throw new Error('page permission required');
        return (cell && ACCOUNTS[cell[0]]) || null;
      },
      state: { get: async () => undefined, set: async () => ({ ok: true }), delete: async () => ({ ok: true }),
        keys: async () => [], all: async () => ({}), on: () => () => {} },
      emit: () => {},
      on: () => () => {},
      pages: { list: async () => [], read: async () => null },
      agent: {
        // agents may be a function of the calls so far: [] until the page is allowed, then the list.
        list: async () => (typeof opts.agents === 'function' ? opts.agents(calls) : (opts.agents || [])),
        sendTask: async (req) => {
          calls.push(['sendTask', req]);
          if (Array.isArray(opts.sendAnswer)) return opts.sendAnswer.length ? opts.sendAnswer.shift() : { ok: true, eventId: 'tabc' };
          return opts.sendAnswer || { ok: true, eventId: 'tabc' };
        },
      },
      db: {
        list: async () => Object.entries(dbs).map(([id, d]) => ({ id, name: d.name })),
        read: async (id) => dbs[id],
        write: async (...a) => { calls.push(['write', ...a]); return answer(); },
        createRow: async (...a) => { calls.push(['createRow', ...a]); const r = answer(); return r.ok ? { ok: true, rowId: 'r' + calls.length } : r; },
        onChange: (id, fn) => { watchers[id] = fn; return () => {}; },
      },
    },
  };
};

const FINDING_PROPS = [
  { id: 't', name: 'Finding', type: 'title' },
  { id: 'p', name: 'Topic', type: 'text' },
  { id: 's', name: 'Source', type: 'url' },
  { id: 'b', name: 'Basis', type: 'text' },
  { id: 'c', name: 'Change', type: 'text' },
  { id: 'st', name: 'State', type: 'status', options: [{ name: 'Proposed', group: 'todo' }, { name: 'Accepted', group: 'done' }, { name: 'Rejected', group: 'done' }] },
  { id: 'cb', name: 'Created by', type: 'created_by' },
];
const REVIEW_PROPS = [
  { id: 't', name: 'Review', type: 'title' },
  { id: 'f', name: 'Finding', type: 'text' },
  { id: 'v', name: 'Verdict', type: 'select', options: [{ name: 'Accepted' }, { name: 'Rejected' }] },
  { id: 'n', name: 'Note', type: 'text' },
  { id: 'cb', name: 'Created by', type: 'created_by' },
];
const deskDbs = (findings = [], reviews = []) => ({
  d1: { id: 'd1', name: 'Research Desk — Findings', props: FINDING_PROPS, rows: findings },
  d2: { id: 'd2', name: 'Research Desk — Reviews', props: REVIEW_PROPS, rows: reviews },
});
const AGENTS = [
  { id: 'u' + 'a'.repeat(64), name: 'Desk Agent', kind: 'agent', userId: '@desk.agent:x' },
  { id: 'u' + 'b'.repeat(64), name: 'Home Agent', kind: 'agent' },
];

{
  const r = await run('research-desk', { noBridge: true });
  check('research-desk/no-bridge-preview', () => {
    assert.match(r.text(), /Open this page in s\.space/);
    assert.equal(r.byId.get('add').disabled, true);
  });
}
{
  const b = fakeMulti({ dbs: deskDbs(), primary: 'd1', agents: AGENTS });
  const r = await run('research-desk', { bridge: b });
  check('research-desk/empty-state', () => assert.match(r.text(), /No findings yet/));
  // Static markup: the DOM shim builds only the elements a page addresses, so read the source.
  check('research-desk/dm-disclaimer-shown', () =>
    assert.match(readFileSync(join(TPL, 'research-desk', 'page.html'), 'utf8'),
      /private DM with you[\s\S]*progress does not come back to this page yet/));
  r.byId.get('claim').value = 'Tides lag the moon';
  r.byId.get('source').value = 'not a link';
  r.byId.get('add').click();
  await settle();
  check('research-desk/source-required', () => {
    assert.equal(b.calls.length, 0);
    assert.match(r.text(), /needs a source link/);
  });
  r.byId.get('source').value = 'https://example.org/tides';
  r.byId.get('topic').value = 'Oceans';
  r.byId.get('basis').value = 'Table 2';
  r.byId.get('add').click();
  await settle();
  check('research-desk/propose-writes-one-row', () => {
    assert.equal(b.calls.length, 1);
    const [kind, db, values] = b.calls[0];
    assert.equal(kind, 'createRow');
    assert.equal(db, 'd1');
    assert.deepEqual(plain(values), { Source: 'https://example.org/tides', State: 'Proposed',
      Finding: 'Tides lag the moon', Topic: 'Oceans', Basis: 'Table 2' });
  });
}
{
  const findings = [
    { id: 'r1', cells: { Finding: 'Claim A', Topic: 'T1', Source: 'https://a.example', State: 'Accepted', 'Created by': ['@desk.agent:x'] } },
    { id: 'r2', cells: { Finding: 'Claim B', Topic: 'T1', Source: 'https://b.example', 'Created by': ['@other:x'] } },
  ];
  // r1's State column says Accepted, but only an agent reviewed it: it must read as proposed.
  const reviews = [
    { id: 'v1', cells: { Review: 'Claim A', Finding: 'r1', Verdict: 'Accepted', 'Created by': ['@desk.agent:x'] } },
    { id: 'v2', cells: { Review: 'Claim B', Finding: 'r2', Verdict: 'Accepted', 'Created by': ['@me:x'] } },
  ];
  const b = fakeMulti({ dbs: deskDbs(findings, reviews), primary: 'd1', agents: AGENTS });
  const r = await run('research-desk', { bridge: b });
  check('research-desk/agent-review-does-not-accept', () => {
    const li = r.byId.get('list').children[0];
    assert.match(li.textContent, /Claim A[\s\S]*Proposed/);
    assert.match(li.textContent, /not counted: Accepted by Desk Agent \(agent\)\. A person has not decided yet/);
    assert.match(li.textContent, /Proposed by Desk Agent \(agent\)/);
  });
  check('research-desk/person-review-accepts', () => {
    const li = r.byId.get('list').children[1];
    assert.match(li.textContent, /Claim B[\s\S]*Accepted/);
    assert.match(li.textContent, /Accepted by Tester/);
  });
  check('research-desk/report-has-only-accepted-with-source', () => {
    const rep = r.byId.get('report').textContent;
    assert.match(rep, /Claim B/);
    assert.match(rep, /https:\/\/b\.example/);
    assert.ok(!/Claim A/.test(rep), rep);
  });
  const accept = r.byId.get('list').children[0].buttons().find((x) => x.textContent === 'Accept');
  accept.click();
  await settle(40);
  check('research-desk/accept-writes-review-then-state', () => {
    assert.deepEqual(plain(b.calls[0]), ['createRow', 'd2', { Finding: 'r1', Verdict: 'Accepted', Review: 'Claim A' }]);
    assert.deepEqual(plain(b.calls[1]), ['write', 'd1', 'r1', 'State', 'Accepted']);
  });
  const ask = r.byId.get('list').children[1].buttons().find((x) => /Ask my agent/.test(x.textContent));
  ask.click();
  await settle(20);
  check('research-desk/ask-sends-own-agent-task-with-row', () => {
    const sent = b.calls.find((c) => c[0] === 'sendTask');
    assert.ok(sent, JSON.stringify(b.calls));
    assert.equal(sent[1].agentId, AGENTS[0].id);
    assert.match(sent[1].text, /Claim B/);
    assert.match(sent[1].key, /^[a-z0-9][a-z0-9._-]{0,63}$/i);
    assert.deepEqual(plain(sent[1].row), { database: 'd1', rowId: 'r2' });
    assert.match(r.text(), /answer arrives in your private DM/);
  });
}
{
  const b = fakeMulti({ dbs: deskDbs([{ id: 'r1', cells: { Finding: 'X', Source: 'https://x.example' } }]), primary: 'd1',
    agents: AGENTS, sendAnswer: { ok: false, reason: 'request outcome unknown' } });
  const r = await run('research-desk', { bridge: b });
  const ask = () => r.byId.get('list').children[0].buttons().find((x) => /Ask my agent/.test(x.textContent));
  ask().click(); await settle(20);
  ask().click(); await settle(20);
  check('research-desk/unknown-outcome-retries-same-key', () => {
    const keys = b.calls.filter((c) => c[0] === 'sendTask').map((c) => c[1].key);
    assert.equal(keys.length, 2);
    assert.equal(keys[0], keys[1]);
    assert.match(r.text(), /retry the same request/);
  });
}
{
  const b = fakeMulti({ dbs: deskDbs([{ id: 'r1', cells: { Finding: 'X', Source: 'https://x.example', 'Created by': ['@me:x'] } }]),
    primary: 'd1', agents: [], consent: false });
  const r = await run('research-desk', { bridge: b });
  check('research-desk/consent-pending-shows-question-marks', () => {
    assert.match(r.text(), /Proposed by \?/);
    assert.match(r.text(), /who is who/);
  });
  check('research-desk/no-agents-disables-asking', () => assert.equal(r.byId.get('ask-topic-go').disabled, true));
}
{
  const b = fakeMulti({ dbs: deskDbs(), primary: 'd1', agents: AGENTS,
    writeAnswers: [{ ok: false, reason: 'waiting for the viewer’s answer' }, { ok: true }] });
  const r = await run('research-desk', { bridge: b });
  r.byId.get('claim').value = 'Retry me';
  r.byId.get('source').value = 'https://r.example';
  r.byId.get('add').click();
  await settle(40);
  check('research-desk/consent-refusal-retries-and-hints', () => assert.match(r.text(), /Allow this page to edit data/));
}
{
  const b = fakeMulti({ dbs: deskDbs(), primary: 'd1' });
  const r = await run('research-desk', { bridge: b });
  b.fire('d1', null);
  await settle();
  check('research-desk/database-removed-error', () => assert.match(r.text(), /was removed from this room/));
}

// sonichi-dot P2: a later agent review must not revoke a person's decision; and the controls.
for (const [label, order, expect] of [
  ['human-then-agent-stays-accepted', ['@me:x', '@desk.agent:x'], 'Accepted'],
  ['agent-then-human-is-accepted', ['@desk.agent:x', '@me:x'], 'Accepted'],
  ['departed-reviewer-is-not-counted', ['@gone:x'], 'Proposed'],
]) {
  const verdict = (by, i) => ({ id: 'v' + i, cells: { Review: 'X', Finding: 'r1',
    Verdict: by === '@desk.agent:x' ? 'Rejected' : 'Accepted', 'Created by': [by] } });
  const b = fakeMulti({ dbs: deskDbs([{ id: 'r1', cells: { Finding: 'Claim X', Topic: 'T', Source: 'https://x.example' } }],
    order.map(verdict)), primary: 'd1', accounts: { '@me:x': 'u1', '@desk.agent:x': 'u2', '@gone:x': 'u9' } });
  const r = await run('research-desk', { bridge: b });
  check(`research-desk/${label}`, () => {
    const li = r.byId.get('list').children[0];
    assert.match(li.textContent, new RegExp('Claim X[\\s\\S]*' + expect));
    const rep = r.byId.get('report').textContent;
    if (expect === 'Accepted') assert.match(rep, /Claim X[\s\S]*Accepted by Tester/);
    else assert.match(rep, /Nothing accepted yet/);
  });
}

const TASK_PROPS = [
  { id: 't', name: 'Task', type: 'title' },
  { id: 's', name: 'Status', type: 'status', options: [
    { name: 'Requested', group: 'todo' }, { name: 'Working', group: 'doing' },
    { name: 'Blocked', group: 'doing' }, { name: 'Done', group: 'done' }] },
  { id: 'b', name: 'Blocker', type: 'text' },
  { id: 'r', name: 'Result', type: 'text' },
  { id: 'a', name: 'Actor', type: 'person' },
  { id: 'cb', name: 'Created by', type: 'created_by' },
];
const boardDbs = (rows = []) => ({ d1: { id: 'd1', name: 'Task Board — Tasks', props: TASK_PROPS, rows } });

{
  const r = await run('task-board', { noBridge: true });
  check('task-board/no-bridge-preview', () => assert.match(r.text(), /Open this page in s\.space/));
}
{
  const b = fakeMulti({ dbs: boardDbs(), primary: 'd1', agents: AGENTS });
  const r = await run('task-board', { bridge: b });
  check('task-board/empty-state', () => assert.match(r.text(), /No tasks yet/));
  check('task-board/agent-choices-say-where', () => {
    const labels = r.byId.get('who').options.map((o) => o.textContent);
    assert.deepEqual(labels, ['No agent (a person does it)', 'Desk Agent (in this room)', 'Home Agent (asked in your DM)']);
  });
  check('task-board/says-what-it-cannot-show', () =>
    assert.match(readFileSync(join(TPL, 'task-board', 'page.html'), 'utf8'), /no completion tracking for agent requests yet/));
  r.byId.get('title').value = 'Book the venue';
  r.byId.get('who').value = AGENTS[0].id;
  r.byId.get('add').click();
  await settle(30);
  check('task-board/in-room-agent-is-named-in-actor-no-dm', () => {
    assert.deepEqual(plain(b.calls[0]), ['createRow', 'd1', { Status: 'Requested', Task: 'Book the venue', Actor: ['@desk.agent:x'] }]);
    assert.ok(!b.calls.some((c) => c[0] === 'sendTask'));
  });
}
{
  const b = fakeMulti({ dbs: boardDbs(), primary: 'd1', agents: AGENTS });
  const r = await run('task-board', { bridge: b });
  r.byId.get('title').value = 'Buy stamps';
  r.byId.get('who').value = AGENTS[1].id;
  r.byId.get('add').click();
  await settle(40);
  check('task-board/outside-agent-gets-dm-task-after-row', () => {
    assert.deepEqual(plain(b.calls[0]), ['createRow', 'd1', { Status: 'Requested', Task: 'Buy stamps' }]);
    const sent = b.calls[1];
    assert.equal(sent[0], 'sendTask');
    assert.equal(sent[1].agentId, AGENTS[1].id);
    assert.deepEqual(plain(sent[1].row), { database: 'd1', rowId: 'r1' });
    assert.match(r.text(), /cannot show its progress or result/);
  });
}
{
  const rows = [
    { id: 'r1', cells: { Task: 'Fix printer', Status: 'Blocked', Blocker: 'needs a part', Actor: ['@desk.agent:x'] } },
    { id: 'r2', cells: { Task: 'Send invoice', Status: 'Done', Result: 'sent on Monday' } },
  ];
  const b = fakeMulti({ dbs: boardDbs(rows), primary: 'd1', agents: AGENTS });
  const r = await run('task-board', { bridge: b });
  check('task-board/shows-agent-blocker-result', () => {
    assert.match(r.text(), /Agent: Desk Agent/);
    assert.match(r.text(), /Blocked on: needs a part/);
    assert.match(r.text(), /Result: sent on Monday/);
  });
  const btn = r.byId.get('cols').buttons().find((x) => x.textContent === 'Working');
  btn.click();
  await settle(20);
  check('task-board/status-click-writes-status', () => assert.deepEqual(plain(b.calls[0]), ['write', 'd1', 'r1', 'Status', 'Working']));
}
{
  const b = fakeMulti({ dbs: boardDbs(), primary: 'd1' });
  const r = await run('task-board', { bridge: b });
  b.fire('d1', null);
  await settle();
  check('task-board/database-removed-error', () => assert.match(r.text(), /was removed from this room/));
}

for (const tpl of ['research-desk', 'task-board']) {
  const dbsFor = tpl === 'research-desk' ? deskDbs() : boardDbs();
  const b = fakeMulti({ dbs: dbsFor, primary: 'd1', agents: { ok: false, reason: 'page permission required' } });
  const before = unhandled.length;
  const r = await run(tpl, { bridge: b });
  await settle(10);
  check(`${tpl}/agent-list-refusal-is-not-an-array`, () => {
    assert.equal(unhandled.length, before, unhandled.slice(before).join('; '));
    assert.match(r.text(), tpl === 'research-desk' ? /No findings yet/ : /No tasks yet/);
  });
}

/* ===== core review of 901bb356: agents after consent, retry, unchecked State write, mutants ===== */
// agent.list() answers [] until the page is allowed; the first allowed write is what allows it.
const afterFirstWrite = (calls) => (calls.some((c) => c[0] === 'createRow' || c[0] === 'write') ? AGENTS : []);
{
  const b = fakeMulti({ dbs: boardDbs(), primary: 'd1', agents: afterFirstWrite });
  const r = await run('task-board', { bridge: b });
  const before = r.byId.get('who').options.length;
  r.byId.get('title').value = 'First task';
  r.byId.get('add').click();
  await settle(40);
  check('task-board/agents-reread-after-first-allowed-write', () => {
    assert.equal(before, 1);
    assert.deepEqual(r.byId.get('who').options.map((o) => o.value), ['', AGENTS[0].id, AGENTS[1].id]);
  });
}
{
  let open = false;
  const b = fakeMulti({ dbs: boardDbs(), primary: 'd1', agents: () => (open ? AGENTS : []) });
  const r = await run('task-board', { bridge: b });
  open = true;
  r.byId.get('who').dispatch('focus');
  await settle(20);
  check('task-board/agents-reread-when-picker-opens', () =>
    assert.equal(r.byId.get('who').options.length, 3));
}
{
  const b = fakeMulti({ dbs: deskDbs(), primary: 'd1', agents: afterFirstWrite });
  const r = await run('research-desk', { bridge: b });
  const before = r.byId.get('ask-topic-go').disabled;
  r.byId.get('claim').value = 'A claim';
  r.byId.get('source').value = 'https://c.example';
  r.byId.get('add').click();
  await settle(40);
  check('research-desk/agents-reread-after-first-allowed-write', () => {
    assert.equal(before, true);
    assert.equal(r.byId.get('ask-topic-go').disabled, false);
    assert.equal(r.byId.get('agent').options.length, 2);
  });
}
{
  let open = false;
  const b = fakeMulti({ dbs: deskDbs(), primary: 'd1', agents: () => (open ? AGENTS : []) });
  const r = await run('research-desk', { bridge: b });
  open = true;
  r.byId.get('agent').dispatch('pointerdown');
  await settle(20);
  check('research-desk/agents-reread-when-picker-opens', () =>
    assert.equal(r.byId.get('ask-topic-go').disabled, false));
}
// A failed DM ask is retried from its row with the SAME key, and adds no second row.
{
  const b = fakeMulti({ dbs: boardDbs(), primary: 'd1', agents: AGENTS,
    sendAnswer: [{ ok: false, reason: 'declined' }, { ok: true, eventId: 't2' }] });
  const r = await run('task-board', { bridge: b });
  r.byId.get('title').value = 'Renew the domain';
  r.byId.get('who').value = AGENTS[1].id;
  r.byId.get('add').click();
  await settle(40);
  check('task-board/refused-ask-is-reported-not-sent', () => {
    assert.match(r.text(), /not sent to Home Agent: declined/);
    assert.ok(!/Sent to Home Agent in your private DM/.test(r.text()), r.text());
  });
  // The board shows the row from the next snapshot; the fake's onChange is driven by hand.
  b.fire('d1', { id: 'd1', name: 'Task Board — Tasks', props: TASK_PROPS,
    rows: [{ id: 'r1', cells: { Task: 'Renew the domain', Status: 'Requested' } }] });
  await settle(20);
  const again = r.byId.get('cols').buttons().find((x) => /Send to Home Agent again/.test(x.textContent));
  check('task-board/failed-ask-offers-retry-on-its-row', () => assert.ok(again, r.text()));
  if (again) { again.click(); await settle(40); }
  check('task-board/retry-reuses-the-key-and-adds-no-row', () => {
    const sends = b.calls.filter((c) => c[0] === 'sendTask').map((c) => c[1]);
    assert.equal(sends.length, 2);
    assert.equal(sends[0].key, sends[1].key);
    assert.deepEqual(plain(sends[1].row), { database: 'd1', rowId: 'r1' });
    assert.equal(b.calls.filter((c) => c[0] === 'createRow').length, 1);
    assert.match(r.text(), /Sent to Home Agent in your private DM/);
    assert.ok(!r.byId.get('cols').buttons().some((x) => /again/.test(x.textContent)));
  });
}
{
  const b = fakeMulti({ dbs: boardDbs(), primary: 'd1', agents: AGENTS,
    writeAnswers: [{ ok: false, reason: 'no edit rights' }] });
  const r = await run('task-board', { bridge: b });
  r.byId.get('title').value = 'Not saved';
  r.byId.get('who').value = AGENTS[1].id;
  r.byId.get('add').click();
  await settle(40);
  check('task-board/no-ask-when-the-row-was-not-saved', () => {
    assert.ok(!b.calls.some((c) => c[0] === 'sendTask'), JSON.stringify(b.calls));
    assert.match(r.text(), /no edit rights/);
  });
}
// research desk: the review lands but the State write is refused: say so.
{
  const findings = [{ id: 'r1', cells: { Finding: 'Claim S', Source: 'https://s.example' } }];
  const b = fakeMulti({ dbs: deskDbs(findings), primary: 'd1', agents: AGENTS,
    writeAnswers: [{ ok: true }, { ok: false, reason: 'no edit rights on Findings' }] });
  const r = await run('research-desk', { bridge: b });
  r.byId.get('list').children[0].buttons().find((x) => x.textContent === 'Accept').click();
  await settle(40);
  check('research-desk/refused-state-write-is-reported', () => {
    assert.equal(b.calls[1][0], 'write');
    assert.match(r.text(), /State column[\s\S]*was not updated: no edit rights on Findings/);
  });
}

if (unhandled.length) {
  failures.push(`a page threw and left its promise unhandled (so a control may be stuck): ${[...new Set(unhandled)].join('; ')}`);
}
console.log(failures.length
  ? `FAIL commons-app-builder templates: ${failures.length} failed\n  ${failures.join('\n  ')}`
  : `all passed — commons-app-builder templates: ${passed} checks`);
process.exit(failures.length ? 1 : 0);
