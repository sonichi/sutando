'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Commons = require('../commons-sdk.js');
const { createFakeRoom, createClock, settle } = require('./fake-room.js');

const MAP_PROPS = [{ id: 'k', name: 'Key', type: 'title' }, { id: 'v', name: 'Value', type: 'text' }];
const TASK_PROPS = [
  { id: 't', name: 'Task', type: 'title' },
  { id: 's', name: 'Status', type: 'select', options: [{ name: 'Open' }, { name: 'Done' }] },
  { id: 'c', name: 'Claim', type: 'text' }, { id: 'r', name: 'Result', type: 'text' },
  { id: 'x', name: 'Removed', type: 'text' }, { id: 'o', name: 'Order', type: 'number' },
  { id: 'b', name: 'Created by', type: 'created_by' },
];

/* One room, two pages: a person and an agent. Sleeps move the fake clock; nothing waits for real. */
async function setup(opts) {
  const o = opts || {};
  const clock = createClock();
  const fake = createFakeRoom({ clock });
  const mapDb = fake.addDatabase('App — State', MAP_PROPS);
  const taskDb = fake.addDatabase('App — Tasks', TASK_PROPS, o.rows);
  const hooks = { now: clock.now, sleep: async (ms) => { clock.advance(ms); } };
  const pa = fake.client(Object.assign({ id: 'u-ana', name: 'Ana', agents: [{ id: 'g1', name: 'Helper', kind: 'claude', userId: '@helper:fake' }, { id: 'g2', name: 'Away', kind: 'claude' }] }, o.a));
  const pb = fake.client(Object.assign({ id: 'u-bot', name: 'Bot', kind: 'agent' }, o.b));
  const a = await Commons.connect(Object.assign({ artifact: pa.artifact }, hooks));
  const b = await Commons.connect(Object.assign({ artifact: pb.artifact }, hooks));
  return { clock, fake, mapDb, taskDb, pa, pb, a, b, hooks };
}

const code = (c) => (e) => e instanceof Commons.CommonsError && e.code === c;
const record = (handle) => { const seen = []; handle.observe((changes, origin) => seen.push({ origin, changes })); return seen; };

/* connect */

test('connect: outside a room it rejects with NO_BRIDGE', async () => {
  await assert.rejects(Commons.connect({ artifact: null }), code('NO_BRIDGE'));
  await assert.rejects(Commons.connect({ artifact: {} }), code('NO_BRIDGE'));
});

test('connect: finds window.artifact, waits for ready, reports capabilities', async () => {
  const fake = createFakeRoom();
  const page = fake.client({ id: 'u1', name: 'Ana' });
  assert.equal(page.artifact.me, null);
  globalThis.artifact = page.artifact;
  try {
    const room = await Commons.connect();
    assert.deepEqual(room.me(), { id: 'u1', name: 'Ana', kind: 'person' });
    assert.deepEqual(room.capabilities, { players: true, authorship: true, agents: true, post: true, requests: false });
    assert.equal(room.app, null);
    assert.equal(room.experimental.requests, null);
  } finally { delete globalThis.artifact; }
});

test('connect: capabilities are false on a bridge without the optional calls', async () => {
  const fake = createFakeRoom();
  const art = fake.client({ id: 'u1', name: 'Ana' }).artifact;
  const bare = { ready: art.ready, db: art.db, get me() { return art.me; } };
  const room = await Commons.connect({ artifact: bare });
  assert.deepEqual(room.capabilities, { players: false, authorship: false, agents: false, post: false, requests: false });
  await assert.rejects(room.players(), code('UNSUPPORTED'));
  await assert.rejects(room.playerOf(['x']), code('UNSUPPORTED'));
  await assert.rejects(room.ask('g1', 'hi', { key: 'k' }), code('UNSUPPORTED'));
  await assert.rejects(room.post('hi'), code('UNSUPPORTED'));
  assert.deepEqual(await room.agents(), []);
});

test('now: uses the bridge clock, and the device clock while the bridge has no sample', async () => {
  const clock = createClock(5000);
  const fake = createFakeRoom({ clock });
  const room = await Commons.connect({ artifact: fake.client({ id: 'u1', name: 'A' }).artifact });
  assert.equal(room.now(), 5000);
  const cold = await Commons.connect({ artifact: fake.client({ id: 'u2', name: 'B', nanClock: true }).artifact });
  assert.ok(Math.abs(cold.now() - Date.now()) < 1000);
});

/* map */

test('map: set, get, has, entries, size; the other page observes the change', async () => {
  const { a, b, fake, mapDb } = await setup();
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  const seenA = record(ma), seenB = record(mb);
  await ma.set('count', 1);
  await ma.set('title', { text: 'Hello' });
  await settle();
  assert.equal(ma.get('count'), 1);
  assert.equal(mb.get('count'), 1);
  assert.deepEqual(mb.get('title'), { text: 'Hello' });
  assert.equal(mb.has('count'), true);
  assert.equal(mb.has('nope'), false);
  assert.equal(mb.get('nope'), undefined);
  assert.deepEqual(mb.entries().sort(), [['count', 1], ['title', { text: 'Hello' }]]);
  assert.equal(mb.size, 2);
  assert.deepEqual(seenA[0], { origin: 'local', changes: [{ key: 'count', action: 'add', value: 1 }] });
  assert.ok(seenB.every((e) => e.origin === 'remote'));
  assert.deepEqual(seenB.flatMap((e) => e.changes.map((c) => c.key)).sort(), ['count', 'title']);
  assert.equal(fake.rows(mapDb).length, 2);
  assert.equal(typeof ma.databaseId, 'string');
});

test('map: an update reports the old value; a local change is visible before the room answers', async () => {
  const { a, b } = await setup();
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  await ma.set('n', 1); await settle();
  const seenB = record(mb);
  const pending = ma.set('n', 2);
  assert.equal(ma.get('n'), 2);
  await pending; await settle();
  assert.deepEqual(seenB, [{ origin: 'remote', changes: [{ key: 'n', action: 'update', value: 2, oldValue: 1 }] }]);
});

test('map: delete empties the value and keeps the row', async () => {
  const { a, b, fake, mapDb } = await setup();
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  await ma.set('n', 1); await settle();
  const seenB = record(mb);
  await ma.delete('n'); await settle();
  assert.equal(ma.has('n'), false);
  assert.equal(mb.has('n'), false);
  assert.deepEqual(seenB[0].changes, [{ key: 'n', action: 'delete', oldValue: 1 }]);
  assert.equal(fake.rows(mapDb).length, 1);
  await ma.delete('never-set');
  assert.equal(fake.rows(mapDb).length, 1);
  await ma.set('n', 5); await settle();
  assert.equal(mb.get('n'), 5);
  assert.equal(fake.rows(mapDb).length, 1);
});

test('map: a refused write rolls back and tells the observer', async () => {
  const { a, pa } = await setup();
  const m = await a.map('App — State');
  await m.set('n', 1); await settle();
  const seen = record(m);
  pa.failNext = 'no';
  await assert.rejects(m.set('n', 2), (e) => e.code === 'REFUSED' && e.reason === 'no');
  assert.equal(m.get('n'), 1);
  assert.deepEqual(seen.map((e) => [e.origin, e.changes[0].action, e.changes[0].value]), [['local', 'update', 2], ['local', 'update', 1]]);
  pa.failNext = 'no';
  await assert.rejects(m.set('fresh', 1), code('REFUSED'));
  assert.equal(m.has('fresh'), false);
});

test('map: two pages set one key at once: the last write to arrive wins on both', async () => {
  const { a, b } = await setup();
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  await ma.set('n', 0); await settle();
  await Promise.all([ma.set('n', 'from-a'), mb.set('n', 'from-b')]);
  await settle();
  assert.equal(ma.get('n'), 'from-b');
  assert.equal(mb.get('n'), 'from-b');
});

test('map: two pages create one key at once: two rows, and both pages agree on the lowest row id', async () => {
  const { a, b, fake, mapDb } = await setup();
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  await Promise.all([ma.set('k', 'from-a'), mb.set('k', 'from-b')]);
  await settle();
  assert.equal(fake.rows(mapDb).length, 2);
  assert.equal(ma.get('k'), 'from-a');
  assert.equal(mb.get('k'), 'from-a');
  await mb.set('k', 'again'); await settle();
  assert.equal(ma.get('k'), 'again');
  assert.equal(fake.rows(mapDb)[1].cells.Value, '"from-b"');
});

test('map: quick sets on a new key run in order and create one row', async () => {
  const { a, fake, mapDb } = await setup();
  const m = await a.map('App — State');
  await Promise.all([m.set('k', 1), m.set('k', 2), m.set('k', 3)]);
  await settle();
  assert.equal(m.get('k'), 3);
  assert.equal(fake.rows(mapDb).length, 1);
});

test('map: a value written by hand or by an agent as plain text is read as text', async () => {
  const { a, fake, mapDb } = await setup();
  const m = await a.map('App — State');
  const seen = record(m);
  fake.serverAddRow(mapDb, { Key: 'note', Value: 'not json' });
  fake.serverAddRow(mapDb, { Key: '', Value: '1' });
  await settle();
  assert.equal(m.get('note'), 'not json');
  assert.equal(m.size, 1);
  assert.equal(seen[0].origin, 'remote');
});

test('map: argument, size, schema and missing-database errors carry stable codes', async () => {
  const { a, fake } = await setup();
  const m = await a.map('App — State');
  await assert.rejects(m.set('', 1), code('BAD_ARGUMENT'));
  await assert.rejects(m.set('k', undefined), code('BAD_ARGUMENT'));
  await assert.rejects(m.set('k', 'x'.repeat(Commons.LIMITS.writeBytes)), code('TOO_LARGE'));
  assert.equal(m.has('k'), false);
  await assert.rejects(a.map('No such database'), code('DB_MISSING'));
  await assert.rejects(a.map(), code('DB_MISSING'));
  await assert.rejects(a.map('App — Tasks'), code('SCHEMA'));
  fake.addDatabase('No title', [{ id: 'v', name: 'Value', type: 'text' }]);
  await assert.rejects(a.map('No title'), code('SCHEMA'));
  const other = await a.map('App — Tasks', { valueProp: 'Result' });
  await other.set('k', 1);
  assert.equal(other.get('k'), 1);
});

test('map and list: with no name they use the app database', async () => {
  const clock = createClock();
  const fake = createFakeRoom({ clock });
  const id = fake.addDatabase('Anything', MAP_PROPS);
  const page = fake.client({ id: 'u1', name: 'Ana', app: { id: 'a1', name: 'App', database: id } });
  const room = await Commons.connect({ artifact: page.artifact });
  assert.deepEqual(room.app, { id: 'a1', name: 'App', database: id });
  const m = await room.map();
  await m.set('k', 1);
  const l = await room.list();
  assert.equal(l.length, 1);
  assert.equal(l.databaseId, id);
  assert.equal(page.calls.filter((c) => c[0] === 'db.onChange').length, 1);
});

/* list */

test('list: push, toArray, get, length; the other page observes the new row', async () => {
  const { a, b } = await setup();
  const la = await a.list('App — Tasks'), lb = await b.list('App — Tasks');
  const seenA = record(la), seenB = record(lb);
  assert.equal(la.titleProp, 'Task');
  const id = await la.push({ Task: 'Write the note', Status: 'Open' });
  assert.equal(la.get(id).cells.Task, 'Write the note');
  await settle();
  assert.equal(lb.length, 1);
  assert.equal(lb.toArray()[0].id, id);
  assert.deepEqual(lb.get(id).cells['Created by'], ['@u-ana:fake']);
  assert.equal(lb.get('nope'), undefined);
  assert.deepEqual(seenA[0].changes.map((c) => [c.action, c.id]), [['add', id]]);
  assert.equal(seenA[0].origin, 'local');
  assert.deepEqual(seenB.map((e) => [e.origin, e.changes[0].action]), [['remote', 'add']]);
  await assert.rejects(la.push(null), code('BAD_ARGUMENT'));
  await assert.rejects(la.push({ Task: 'x', Status: 'Nope' }), code('REFUSED'));
  await assert.rejects(la.push({ Task: 'x'.repeat(30000) }), code('TOO_LARGE'));
  assert.equal(la.length, 1);
});

test('list: update shows at once, reaches the other page, and rolls back when refused', async () => {
  const { a, b, pa } = await setup({ rows: [{ Task: 'One', Status: 'Open' }] });
  const la = await a.list('App — Tasks'), lb = await b.list('App — Tasks');
  const id = la.toArray()[0].id;
  const seenB = record(lb);
  const pending = la.update(id, { Status: 'Done', Result: 'ok' });
  assert.equal(la.get(id).cells.Status, 'Done');
  await pending; await settle();
  assert.equal(lb.get(id).cells.Result, 'ok');
  assert.equal(seenB[0].changes[0].action, 'update');
  assert.equal(seenB[0].changes[0].oldRow.cells.Status, 'Open');
  const seenA = record(la);
  pa.failNext = 'no';
  await assert.rejects(la.update(id, { Result: 'changed' }), code('REFUSED'));
  assert.equal(la.get(id).cells.Result, 'ok');
  assert.deepEqual(seenA.map((e) => e.changes[0].row.cells.Result), ['changed', 'ok']);
  await assert.rejects(la.update('nope', { Result: 'x' }), code('NOT_FOUND'));
  await assert.rejects(la.update(id, { Missing: 'x' }), code('SCHEMA'));
});

test('list: an update of several columns is not atomic; the error lists what was written', async () => {
  const { a, fake, taskDb } = await setup({ rows: [{ Task: 'One', Status: 'Open' }] });
  const l = await a.list('App — Tasks');
  const id = l.toArray()[0].id;
  await assert.rejects(l.update(id, { Result: 'saved', Status: 'Nope' }), (e) => e.code === 'REFUSED' && e.written.join() === 'Result');
  await settle();
  assert.equal(l.get(id).cells.Result, 'saved');
  assert.equal(l.get(id).cells.Status, 'Open');
  assert.equal(fake.rows(taskDb)[0].cells.Result, 'saved');
});

test('list: remove hides the row with a marker; the row stays in the database', async () => {
  const { a, b, fake, taskDb } = await setup({ rows: [{ Task: 'One', Status: 'Open' }, { Task: 'Two', Status: 'Open' }] });
  const la = await a.list('App — Tasks'), lb = await b.list('App — Tasks');
  const seenB = record(lb);
  const [one, two] = la.toArray();
  await la.remove(one.id); await settle();
  assert.deepEqual(lb.toArray().map((r) => r.id), [two.id]);
  assert.equal(seenB[0].changes[0].action, 'delete');
  assert.equal(fake.rows(taskDb).length, 2);
  const custom = await b.list('App — Tasks', { removed: { prop: 'Status', value: 'Done' } });
  assert.equal(custom.length, 2);
  await custom.remove(two.id);
  assert.deepEqual(custom.toArray().map((r) => r.id), [one.id]);
});

test('list: orderBy sorts by a column, blanks last, row id as the tie-break', async () => {
  const { a } = await setup({ rows: [{ Task: 'c', Order: 3 }, { Task: 'none' }, { Task: 'a', Order: 1 }, { Task: 'a2', Order: 1 }] });
  const plain = await a.list('App — Tasks');
  assert.deepEqual(plain.toArray().map((r) => r.cells.Task), ['c', 'none', 'a', 'a2']);
  const sorted = await a.list('App — Tasks', { orderBy: 'Order' });
  assert.deepEqual(sorted.toArray().map((r) => r.cells.Task), ['a', 'a2', 'c', 'none']);
});

test('list: two pages write at once: different cells both stay, one cell keeps the last write', async () => {
  const { a, b } = await setup({ rows: [{ Task: 'One', Status: 'Open' }] });
  const la = await a.list('App — Tasks'), lb = await b.list('App — Tasks');
  const id = la.toArray()[0].id;
  await Promise.all([la.update(id, { Status: 'Done' }), lb.update(id, { Result: 'from-b' })]);
  await settle();
  for (const l of [la, lb]) assert.deepEqual([l.get(id).cells.Status, l.get(id).cells.Result], ['Done', 'from-b']);
  await Promise.all([la.update(id, { Result: 'a-wins?' }), lb.update(id, { Result: 'b-last' })]);
  await settle();
  assert.equal(la.get(id).cells.Result, 'b-last');
  assert.equal(lb.get(id).cells.Result, 'b-last');
});

test('list: a removed database empties the list and refuses writes with DB_GONE', async () => {
  const { a, fake, taskDb } = await setup({ rows: [{ Task: 'One' }] });
  const l = await a.list('App — Tasks');
  const id = l.toArray()[0].id;
  const seen = record(l);
  fake.removeDatabase(taskDb); await settle();
  assert.equal(l.gone, true);
  assert.equal(l.length, 0);
  assert.equal(seen[0].changes[0].action, 'delete');
  await assert.rejects(l.push({ Task: 'x' }), code('DB_GONE'));
  await assert.rejects(l.update(id, { Result: 'x' }), code('NOT_FOUND'));
});

test('observe: the returned function stops the callbacks; close stops the watch', async () => {
  const { a, b, pb } = await setup();
  const la = await a.list('App — Tasks'), lb = await b.list('App — Tasks');
  const seen = [];
  const stop = lb.observe((c) => seen.push(c));
  await la.push({ Task: 'One' }); await settle();
  stop();
  await la.push({ Task: 'Two' }); await settle();
  assert.equal(seen.length, 1);
  assert.equal(lb.length, 2);
  b.close(); await settle();
  assert.equal(pb.watchers.size, 0);
  await la.push({ Task: 'Three' }); await settle();
  assert.equal(lb.length, 2);
});

/* consent and limits */

test('writes wait for the access question, tell onWaiting, and save once the viewer says yes', async () => {
  const { a, pa, hooks, clock } = await setup({ a: { consent: 'unasked' } });
  const l = await a.list('App — Tasks');
  const waits = [];
  const stop = a.onWaiting((c) => { waits.push(c); if (waits.length === 3) pa.answer(true); });
  const before = clock.now();
  const id = await l.push({ Task: 'One' });
  assert.deepEqual(waits, ['CONSENT_PENDING', 'CONSENT_PENDING', 'CONSENT_PENDING']);
  assert.equal(clock.now() - before, 4500);
  assert.equal(l.get(id).cells.Task, 'One');
  stop();
  assert.equal(typeof hooks.sleep, 'function');
});

test('a "no" gives CONSENT_DENIED; an unanswered question gives CONSENT_PENDING after the retry window', async () => {
  const denied = await setup({ a: { consent: 'denied' } });
  const l = await denied.a.list('App — Tasks');
  await assert.rejects(l.push({ Task: 'One' }), code('CONSENT_DENIED'));
  const silent = await setup({ a: { consent: 'unasked' } });
  const l2 = await silent.a.list('App — Tasks');
  const before = silent.clock.now();
  await assert.rejects(l2.push({ Task: 'One' }), code('CONSENT_PENDING'));
  assert.equal(silent.clock.now() - before, 120000);
});

test('a rate-limited write retries until the minute passes; without retry it is RATE_LIMITED', async () => {
  const { a, fake, pa, clock } = await setup();
  const l = await a.list('App — Tasks');
  const waits = [];
  a.onWaiting((c) => waits.push(c));
  for (let i = 0; i < 31; i += 1) await l.push({ Task: 'T' + i });
  assert.equal(l.length, 31);
  assert.ok(waits.length > 0 && waits.every((c) => c === 'RATE_LIMITED'));
  const strict = await Commons.connect({ artifact: fake.client({ id: 'u-x', name: 'X' }).artifact, now: clock.now, retryFor: 0 });
  const lx = await strict.list('App — Tasks');
  for (let i = 0; i < 30; i += 1) await lx.push({ Task: 'X' + i });
  await assert.rejects(lx.push({ Task: 'one too many' }), code('RATE_LIMITED'));
  assert.equal(pa.writes.length <= 30, true);
});

test('a refused read is reported with its code', async () => {
  const { a, pa } = await setup();
  for (let i = 0; i < 20; i += 1) await pa.artifact.db.read('nope').catch(() => {});
  await assert.rejects(a.list('App — Tasks'), code('RATE_LIMITED'));
});

/* players */

test('players: people and agents come back in one shape; playerOf resolves the stamped author', async () => {
  const { a, b } = await setup();
  assert.deepEqual(a.me(), { id: 'u-ana', name: 'Ana', kind: 'person' });
  assert.deepEqual(b.me(), { id: 'u-bot', name: 'Bot', kind: 'agent' });
  assert.deepEqual(await a.players(), [{ id: 'u-ana', name: 'Ana', kind: 'person' }, { id: 'u-bot', name: 'Bot', kind: 'agent' }]);
  const lb = await b.list('App — Tasks');
  const id = await lb.push({ Task: 'By the agent' });
  await settle();
  const la = await a.list('App — Tasks');
  assert.equal(await a.playerOf(la.get(id).cells['Created by']), 'u-bot');
  assert.equal(await a.playerOf(['@stranger:fake']), null);
});

test('players: before the viewer allows the page they reject with CONSENT_REQUIRED', async () => {
  const { a } = await setup({ a: { consent: 'unasked' } });
  await assert.rejects(a.players(), code('CONSENT_REQUIRED'));
  await assert.rejects(a.playerOf(['@u-bot:fake']), code('CONSENT_REQUIRED'));
  assert.deepEqual(await a.agents(), []);
});

/* claims */

async function board() {
  const s = await setup({ rows: [{ Task: 'One', Status: 'Open' }, { Task: 'Two', Status: 'Open' }] });
  s.la = await s.a.list('App — Tasks');
  s.lb = await s.b.list('App — Tasks');
  [s.one, s.two] = s.la.toArray().map((r) => r.id);
  return s;
}

test('claim: a free row is claimed, the other page sees who holds it, and may not take it', async () => {
  const { la, lb, one, pb, clock } = await board();
  assert.equal(la.claimOf(one), null);
  const mine = await la.claim(one, { ttl: 30000 });
  assert.equal(mine.ok, true);
  assert.equal(mine.confirmed, true);
  assert.deepEqual(mine.holder, { id: 'u-ana', name: 'Ana', kind: 'person', until: clock.now() + 30000, mine: true });
  await settle();
  assert.deepEqual(lb.claimOf(one), { id: 'u-ana', name: 'Ana', kind: 'person', until: clock.now() + 30000, mine: false });
  const writes = pb.calls.filter((c) => c[0] === 'db.write').length;
  const theirs = await lb.claim(one);
  assert.equal(theirs.ok, false);
  assert.equal(theirs.holder.id, 'u-ana');
  assert.equal(pb.calls.filter((c) => c[0] === 'db.write').length, writes);
});

test('claim race: two claim at once; the last write wins and the loser is told by its own claim', async () => {
  const { la, lb, one } = await board();
  const [ra, rb] = await Promise.all([la.claim(one), lb.claim(one)]);
  await settle();
  assert.equal(ra.ok, false);
  assert.equal(ra.holder.id, 'u-bot');
  assert.equal(ra.holder.kind, 'agent');
  assert.equal(rb.ok, true);
  assert.equal(la.claimOf(one).id, 'u-bot');
  assert.equal(lb.claimOf(one).mine, true);
});

test('claim race: a claim that lands after the winner confirmed is reported through onLost', async () => {
  const { la, lb, one, pb } = await board();
  pb.pause();
  const lost = [];
  const ra = await la.claim(one, { onLost: (holder) => lost.push(holder) });
  assert.equal(ra.ok, true);
  const rb = await lb.claim(one);
  assert.equal(rb.ok, true);
  await settle();
  assert.equal(lost.length, 1);
  assert.equal(lost[0].id, 'u-bot');
  assert.equal(la.claimOf(one).mine, false);
  pb.resume(); await settle();
  assert.equal(lost.length, 1);
});

test('claim: it expires by the room clock, then anyone may claim; no onLost after expiry', async () => {
  const { la, lb, one, clock } = await board();
  const lost = [];
  await la.claim(one, { ttl: 10000, onLost: (h) => lost.push(h) });
  await settle();
  clock.advance(9999);
  assert.equal(lb.claimOf(one).id, 'u-ana');
  clock.advance(1);
  assert.equal(lb.claimOf(one), null);
  assert.equal(la.claimOf(one), null);
  const rb = await lb.claim(one);
  await settle();
  assert.equal(rb.ok, true);
  assert.equal(la.claimOf(one).id, 'u-bot');
  assert.deepEqual(lost, []);
});

test('claim: claiming a row you hold renews it without a false onLost', async () => {
  const { la, lb, one, clock } = await board();
  const lost = [];
  await la.claim(one, { ttl: 10000, onLost: (h) => lost.push(h) });
  clock.advance(5000);
  const again = await la.claim(one, { ttl: 10000, onLost: (h) => lost.push(h) });
  await settle();
  assert.equal(again.ok, true);
  assert.equal(lb.claimOf(one).until, clock.now() + 10000);
  assert.deepEqual(lost, []);
});

test('release: frees a claim you hold; false when you do not hold it', async () => {
  const { la, lb, one, two, fake, taskDb } = await board();
  await la.claim(one); await settle();
  assert.equal(await lb.release(one), false);
  assert.equal(await lb.release(two), false);
  assert.equal(await la.release(one), true);
  await settle();
  assert.equal(lb.claimOf(one), null);
  assert.equal(fake.rows(taskDb)[0].cells.Claim, '');
  assert.equal((await lb.claim(one)).ok, true);
});

test('claim: an agent that writes the claim cell through Actions is a holder like any other', async () => {
  const { la, one, fake, taskDb, clock } = await board();
  fake.serverWrite(taskDb, one, 'Claim', JSON.stringify({ by: 'agent-7', name: 'Runner', kind: 'agent', until: clock.now() + 60000 }));
  await settle();
  assert.deepEqual(la.claimOf(one), { id: 'agent-7', name: 'Runner', kind: 'agent', until: clock.now() + 60000, mine: false });
  assert.equal((await la.claim(one)).ok, false);
  fake.serverWrite(taskDb, one, 'Claim', 'garbage');
  await settle();
  assert.equal(la.claimOf(one), null);
});

test('claim: when the read-back fails the answer is marked unconfirmed', async () => {
  const { la, one, pa } = await board();
  for (let i = 0; i < 20; i += 1) await pa.artifact.db.read('nope').catch(() => {});
  const r = await la.claim(one);
  assert.equal(r.ok, true);
  assert.equal(r.confirmed, false);
  assert.equal(r.holder.mine, true);
});

test('claim: bad ttl, unknown row, missing claim column and a refused write are errors', async () => {
  const { la, a, one, pa } = await board();
  await assert.rejects(la.claim(one, { ttl: 0 }), code('BAD_ARGUMENT'));
  await assert.rejects(la.claim(one, { ttl: Infinity }), code('BAD_ARGUMENT'));
  await assert.rejects(la.claim('nope'), code('NOT_FOUND'));
  const other = await a.list('App — Tasks', { claimProp: 'Lock' });
  await assert.rejects(other.claim(one), code('SCHEMA'));
  pa.failNext = 'no';
  await assert.rejects(la.claim(one), code('REFUSED'));
  assert.equal(la.claimOf(one), null);
});

/* asking an agent */

test('agents and ask: the viewer confirms the exact text; the page gets a receipt only', async () => {
  const { a, pa } = await setup();
  assert.deepEqual(await a.agents(), [{ id: 'g1', name: 'Helper', kind: 'agent', inRoom: true }, { id: 'g2', name: 'Away', kind: 'agent', inRoom: false }]);
  const r = await a.ask('g1', 'Summarise row 3', { key: 'sum-3', actionId: 'summarise', row: { database: 'd1', rowId: 'r3' } });
  assert.deepEqual(Object.keys(r), ['eventId']);
  assert.deepEqual(pa.dialogs, ['Summarise row 3']);
  assert.deepEqual(pa.sent[0], { agentId: 'g1', text: 'Summarise row 3', key: 'sum-3', actionId: 'summarise', row: { database: 'd1', rowId: 'r3' } });
  const again = await a.ask('g1', 'Summarise row 3', { key: 'sum-3' });
  assert.equal(again.eventId, r.eventId);
  assert.equal(pa.dialogs.length, 1);
});

test('ask: refusals map to stable codes', async () => {
  const { a, pa } = await setup();
  await assert.rejects(a.ask('g1', 'x', {}), code('BAD_ARGUMENT'));
  await assert.rejects(a.ask('g1', 'x', { key: 'has space' }), code('BAD_ARGUMENT'));
  await assert.rejects(a.ask('g1', 'x', { key: 'k', actionId: 'bad id' }), code('BAD_ARGUMENT'));
  await assert.rejects(a.ask('someone-elses', 'x', { key: 'k1' }), code('AGENT_UNAVAILABLE'));
  pa.dialog = 'no';
  await assert.rejects(a.ask('g1', 'x', { key: 'k2' }), code('DECLINED'));
  pa.dialog = 'yes';
  for (let i = 0; i < 10; i += 1) await a.ask('g1', 'x', { key: 'bulk-' + i });
  await assert.rejects(a.ask('g1', 'x', { key: 'k3' }), code('RATE_LIMITED'));
  const asking = await setup({ a: { consent: 'unasked' } });
  await assert.rejects(asking.a.ask('g1', 'x', { key: 'k' }), code('CONSENT_REQUIRED'));
  for (const [reason, c] of [['another request is in progress', 'BUSY'], ['request outcome unknown', 'OUTCOME_UNKNOWN']]) {
    pa.artifact.agent.sendTask = async () => ({ ok: false, reason });
    await assert.rejects(a.ask('g1', 'x', { key: 'k9' }), code(c));
  }
});

test('post and mention: posts as the viewer after a yes; bad mentions and a no are refused', async () => {
  const { a, pa, fake } = await setup();
  assert.equal(a.mention('u-bot'), '<@u-bot>');
  const r = await a.post(a.mention('u-bot') + ' please take task One', { key: 'p1' });
  assert.equal(typeof r.eventId, 'string');
  assert.deepEqual(fake.posts.map((p) => [p.by, p.text]), [['u-ana', '<@u-bot> please take task One']]);
  assert.deepEqual(pa.calls.filter((c) => c[0] === 'room.post')[0][1], { text: '<@u-bot> please take task One', key: 'p1' });
  await assert.rejects(a.post('<@nobody> hi'), code('BAD_MENTION'));
  await assert.rejects(a.post('@room hi'), code('BAD_MENTION'));
  pa.dialog = 'no';
  await assert.rejects(a.post('hi'), code('DECLINED'));
  pa.dialog = 'yes';
  for (let i = 0; i < 9; i += 1) await a.post('n' + i);
  await assert.rejects(a.post('one too many'), code('RATE_LIMITED'));
});

/* experimental */

test('experimental requests: absent on today\'s bridge, present only when all three calls exist', async () => {
  const { a, pa, hooks } = await setup();
  assert.equal(a.experimental.requests, null);
  pa.artifact.agent.requestable = async () => [];
  const partial = await Commons.connect(Object.assign({ artifact: pa.artifact }, hooks));
  assert.equal(partial.experimental.requests, null);
  assert.equal(partial.capabilities.requests, false);
});

test('experimental requests: agents, send, duplicate, watch and refusal codes', async () => {
  const { pa, pb, fake, hooks, taskDb } = await setup({ rows: [{ Task: 'One' }] });
  const agents = [{ id: 'm1', name: 'Board agent', actions: ['claim'] }];
  pa.enableRequests(agents); pb.enableRequests(agents);
  const a = await Commons.connect(Object.assign({ artifact: pa.artifact }, hooks));
  const b = await Commons.connect(Object.assign({ artifact: pb.artifact }, hooks));
  assert.equal(a.capabilities.requests, true);
  const rq = a.experimental.requests;
  assert.deepEqual(await rq.agents(), agents);
  const lists = [];
  const first = await rq.watch({ database: taskDb }, (list) => lists.push(list));
  assert.deepEqual(first, []);
  const row = { database: taskDb, rowId: fake.rows(taskDb)[0].id };
  const sent = await rq.send({ agentId: 'm1', action: 'claim', row, text: 'Claim "One"', key: 'claim-1' });
  assert.equal(sent.duplicate, false);
  const dup = await b.experimental.requests.send({ agentId: 'm1', action: 'claim', row, text: 'Claim "One"', key: 'claim-1' });
  assert.deepEqual(dup, { requestId: sent.requestId, duplicate: true });
  fake.answerRequest(sent.requestId, 'done', 'Claimed');
  const last = lists[lists.length - 1];
  assert.deepEqual([last[0].state, last[0].outcome, last[0].presser], ['done', 'Claimed', 'u-ana']);
  await assert.rejects(rq.send({ agentId: 'm1', action: 'delete', row, text: 'x', key: 'k' }), (e) => e.code === 'REQUEST_REFUSED' && e.reason === 'action_not_allowed');
  pa.dialog = 'no';
  await assert.rejects(rq.send({ agentId: 'm1', action: 'claim', row, text: 'x', key: 'k' }), code('DECLINED'));
  pa.dialog = 'yes'; pa.consent = 'asking';
  await assert.rejects(rq.send({ agentId: 'm1', action: 'claim', row, text: 'x', key: 'k' }), code('CONSENT_REQUIRED'));
  pa.consent = 'granted';
  pa.artifact.agent.request = async () => ({ ok: false, reason: 'rate_page' });
  await assert.rejects(rq.send({}), code('RATE_LIMITED'));
});

test('experimental requests: the list reaches the page as the bridge gave it, outcome untouched', async () => {
  const { pa, fake, hooks, taskDb } = await setup({ rows: [{ Task: 'One' }] });
  pa.enableRequests([{ id: 'm1', name: 'Board agent', actions: ['claim'] }]);
  const rq = (await Commons.connect(Object.assign({ artifact: pa.artifact }, hooks))).experimental.requests;
  const lists = [];
  await rq.watch({ database: taskDb }, (list) => lists.push(list));
  const row = { database: taskDb, rowId: fake.rows(taskDb)[0].id };
  const sent = await rq.send({ agentId: 'm1', action: 'claim', row, text: 'Claim "One"', key: 'claim-1' });
  const markup = '<img src=x onerror="alert(1)"> &amp; <b>done</b>';
  fake.answerRequest(sent.requestId, 'done', markup);
  assert.equal(lists[lists.length - 1][0].outcome, markup);

  const given = [{ requestId: 'q1', state: 'working' }];
  const bare = { agent: { requestable: async () => [], request: async () => ({ ok: true, requestId: 'q1' }), requests: async () => given } };
  const seen = [];
  const adapter = (await Commons.connect({ artifact: Object.assign({ db: {}, ready: Promise.resolve() }, bare) })).experimental.requests;
  assert.equal(await adapter.watch({}, (list) => seen.push(list)), given);
  assert.equal(seen[0], given);
  assert.deepEqual(given, [{ requestId: 'q1', state: 'working' }]);
});

test('the removal marker and the reason matching each live in one place in the library', () => {
  const src = require('node:fs').readFileSync(require.resolve('../commons-sdk.js'), 'utf8');
  assert.equal(src.match(/'Removed'/g).length, 1);
  assert.equal(src.match(/\bremoval\(/g).length, 1);
  assert.equal(src.match(/\bREASONS\b/g).length, 3);
  assert.equal(src.match(/\.test\(/g).length, 3);
});

test('the module exports its limits, its error class and a version', () => {
  assert.equal(Commons.LIMITS.writeBytes, 20000);
  assert.equal(Commons.LIMITS.writesPerMinute, 30);
  assert.equal(Commons.LIMITS.readsPerMinute, 20);
  assert.ok(new Commons.CommonsError('X', 'm') instanceof Error);
  assert.match(Commons.version, /^\d+\.\d+\.\d+$/);
});
