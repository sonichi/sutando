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

console.log(failures.length
  ? `FAIL commons-app-builder templates: ${failures.length} failed\n  ${failures.join('\n  ')}`
  : `all passed — commons-app-builder templates: ${passed} checks`);
process.exit(failures.length ? 1 : 0);
