/* Runs each skills/commons-app-builder/templates/<t>/page.html against a fake window.artifact,
 * the way GUIDE.md step 6 asks, and checks the data path of every control. Hermetic: a tiny DOM
 * shim, no jsdom, no network, no browser. Layout at 390px is NOT checked here — only a real
 * browser lays out CSS; the CSS contract is checked in the Python suite. */
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TPL = join(ROOT, 'skills', 'commons-app-builder', 'templates');
let passed = 0;
const failures = [];
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
  }
  appendChild(node) {
    if (node && node.isFragment) { node.children.forEach((c) => this.appendChild(c)); return node; }
    this.children.push(node);
    if (this.tagName === 'SELECT' && node.tagName === 'OPTION') this.options.push(node);
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
  const bridge = opts.noBridge ? { art: undefined, calls: [], fire: () => {} } : fakeArtifact(opts);
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

console.log(failures.length
  ? `FAIL commons-app-builder templates: ${failures.length} failed\n  ${failures.join('\n  ')}`
  : `all passed — commons-app-builder templates: ${passed} checks`);
process.exit(failures.length ? 1 : 0);
