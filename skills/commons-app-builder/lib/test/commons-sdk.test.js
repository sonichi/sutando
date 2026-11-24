'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Commons = require('../commons-sdk.js');
const { createFakeRoom, createClock, settle, NO_ANSWER } = require('./fake-room.js');

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
  const fake = createFakeRoom(Object.assign({ clock }, o.room));
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

async function board(opts) {
  const s = await setup(Object.assign({ rows: [{ Task: 'One', Status: 'Open' }, { Task: 'Two', Status: 'Open' }] }, opts));
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
  assert.equal(mine.certainty, 'local');
  assert.deepEqual(mine.holder, { id: 'u-ana', name: 'Ana', kind: 'person', until: clock.now() + 30000, mine: true });
  await settle();
  assert.deepEqual(lb.claimOf(one), { id: 'u-ana', name: 'Ana', kind: 'person', until: clock.now() + 30000, mine: false });
  const writes = pb.calls.filter((c) => c[0] === 'db.write').length;
  const theirs = await lb.claim(one);
  assert.deepEqual([theirs.ok, theirs.reason, theirs.holder.id], [false, 'held', 'u-ana']);
  assert.equal(pb.calls.filter((c) => c[0] === 'db.write').length, writes);
});

test('claim race, plain writes: both are told ok as "local"; the last write stays and the other hears onLost', async () => {
  const { la, lb, one } = await board();
  const lost = [];
  const [ra, rb] = await Promise.all([la.claim(one, { onLost: (h) => lost.push(h.id) }), lb.claim(one)]);
  assert.deepEqual([ra.ok, ra.certainty, rb.ok, rb.certainty], [true, 'local', true, 'local']);
  await settle();
  assert.deepEqual(lost, ['u-bot']);
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

test('claim: a plain claim spends no read; the page\'s own copy could not confirm it anyway', async () => {
  const { la, one, pa } = await board();
  const reads = pa.calls.filter((c) => c[0] === 'db.read').length;
  const r = await la.claim(one);
  assert.deepEqual([r.ok, r.certainty, r.holder.mine], [true, 'local', true]);
  assert.equal(pa.calls.filter((c) => c[0] === 'db.read').length, reads);
});

test('claim: the expiry is stamped by the attempt the room accepts, not when claim was called', async () => {
  const { clock, pa } = await setup({ rows: [{ Task: 'One' }], a: { consent: 'unasked' } });
  const start = clock.now();
  const sleep = async (ms) => { clock.advance(ms); if (clock.now() - start >= 63000) pa.answer(true); };
  const list = await (await Commons.connect({ artifact: pa.artifact, now: clock.now, sleep })).list('App — Tasks');
  const r = await list.claim(list.toArray()[0].id);
  assert.ok(clock.now() - start >= 63000);
  assert.deepEqual([r.ok, r.holder && r.holder.until], [true, clock.now() + 60000]);
});

test('claim: a write that outlives its ttl is reported as expired, never as ok', async () => {
  const { la, one, pa, clock } = await board();
  const write = pa.artifact.db.write;
  pa.artifact.db.write = async (...args) => { const r = await write(...args); clock.advance(5001); return r; };
  const r = await la.claim(one, { ttl: 5000 });
  assert.deepEqual([r.ok, r.reason, r.holder], [false, 'expired', null]);
});

test('claim: a refused release and a refused renewal keep the earlier claim tracked for onLost', async () => {
  for (const act of ['release', 'claim']) {
    const { la, one, pa, fake, taskDb, clock } = await board();
    const lost = [];
    assert.equal((await la.claim(one, { onLost: (h) => lost.push(h && h.id) })).ok, true);
    pa.failNext = 'the room said no';
    await assert.rejects(la[act](one), code('REFUSED'));
    assert.equal(la.claimOf(one).mine, true);
    fake.serverWrite(taskDb, one, 'Claim', JSON.stringify({ by: 'runner', name: 'Runner', kind: 'agent', until: clock.now() + 60000 }));
    await settle();
    assert.deepEqual(lost, ['runner'], act);
  }
});

/* claims when a page has its own copy, and conditional writes */

const EXACT = { versions: true, conditional: true };
const MANUAL = { a: { sync: 'manual' }, b: { sync: 'manual' } };
const exactWrites = (page) => page.calls.filter((c) => c[0] === 'db.write' && c[5] && 'ifVersion' in c[5]);

test('claim, own copies and plain writes: both pages get ok as "local", never as "room"; sync settles it through onLost', async () => {
  const { la, lb, one, pa, pb } = await board(MANUAL);
  const lost = [];
  const ra = await la.claim(one, { onLost: (h) => lost.push(h.id) }), rb = await lb.claim(one);
  assert.deepEqual([ra.ok, ra.certainty, rb.ok, rb.certainty], [true, 'local', true, 'local']);
  assert.equal(la.claimOf(one).mine && lb.claimOf(one).mine, true);
  pa.sync(); pb.sync(); pa.sync();
  assert.deepEqual(lost, ['u-bot']);
  assert.equal(la.claimOf(one).id, 'u-bot');
});

test('claim, conditional writes: one of two simultaneous claims is taken by the room; the other is told who holds it', async () => {
  const { la, lb, one, pa, pb } = await board({ room: EXACT });
  const [ra, rb] = await Promise.all([la.claim(one), lb.claim(one)]);
  assert.deepEqual([ra.ok, ra.certainty], [true, 'room']);
  assert.deepEqual([rb.ok, rb.reason, rb.holder.id, rb.certainty], [false, 'held', 'u-ana', 'local']);
  assert.equal(exactWrites(pa).length, 1);
  assert.equal(exactWrites(pb).length, 1);
  assert.equal(pb.calls.filter((c) => c[0] === 'db.write').length, 1);
});

test('claim, conditional writes and own copies: two pages cannot both be told ok; a stale copy raises no false onLost', async () => {
  const { la, lb, one, two, pb } = await board(Object.assign({ room: EXACT }, MANUAL));
  const lost = [];
  const ra = await la.claim(one, { onLost: (h) => lost.push(h) });
  assert.deepEqual([ra.ok, ra.certainty], [true, 'room']);
  const rb = await lb.claim(one);
  assert.deepEqual([rb.ok, rb.reason, rb.certainty], [false, 'contended', 'local']);
  pb.sync();
  assert.deepEqual([(await lb.claim(one)).reason, lb.claimOf(one).id], ['held', 'u-ana']);
  await la.update(two, { Result: 'x' }); await settle();
  assert.equal(la.claimOf(one).mine, true);
  assert.deepEqual(lost, []);
});

test('conditional writes: the version comes from the read and from each snapshot', async () => {
  const { la, one, two, pa, fake, taskDb } = await board({ room: EXACT });
  assert.equal((await la.claim(one)).certainty, 'room');
  fake.serverWrite(taskDb, two, 'Result', 'moved'); await settle();
  assert.equal((await la.claim(two)).certainty, 'room');
  const sent = exactWrites(pa).map((c) => c[5].ifVersion);
  assert.equal(sent.length, 2);
  assert.ok(sent[1] > sent[0]);
  assert.equal(pa.calls.filter((c) => c[0] === 'db.read').length, 1);
});

test('conditional writes: "not available here" and "invalid request" fall back to a plain write, once and for good', async () => {
  const off = await board({ room: { versions: true } });
  assert.deepEqual([(await off.la.claim(off.one)).certainty, (await off.la.claim(off.two)).certainty], ['local', 'local']);
  assert.equal(exactWrites(off.pa).length, 1);
  const odd = await board({ room: EXACT });
  odd.pa.failNext = 'invalid request';
  const r = await odd.la.claim(odd.one);
  assert.deepEqual([r.ok, r.certainty], [true, 'local']);
});

test('conditional writes: without a version the write is plain and no ifVersion is sent', async () => {
  const { la, one, pa } = await board({ room: { conditional: true } });
  assert.equal((await la.claim(one)).certainty, 'local');
  assert.equal(exactWrites(pa).length, 0);
});

test('conditional writes: a lost answer means read again, never refused', async () => {
  const landed = await board({ room: EXACT });
  landed.pa.loseAnswer = true;
  assert.deepEqual([(await landed.la.claim(landed.one)).certainty, exactWrites(landed.pa).length], ['room', 1]);
  const dropped = await board({ room: EXACT });
  dropped.pa.failNext = NO_ANSWER;
  const r = await dropped.la.claim(dropped.one);
  assert.deepEqual([r.ok, r.certainty, exactWrites(dropped.pa).length], [true, 'room', 2]);
  const [first, second] = exactWrites(dropped.pa).map((c) => JSON.parse(c[4]).n);
  assert.notEqual(first, second);
});

test('claim, lost answer while the own copy lags: the token of the attempt that landed is the proof', async () => {
  const { pa, clock, fake, taskDb } = await setup({ rows: [{ Task: 'One' }], room: EXACT, a: { sync: 'manual' } });
  const sleep = async (ms) => { clock.advance(ms); pa.sync(); };
  const list = await (await Commons.connect({ artifact: pa.artifact, now: clock.now, sleep })).list('App — Tasks');
  const one = list.toArray()[0].id;
  pa.loseAnswer = true;
  const r = await list.claim(one);
  assert.deepEqual([r.ok, r.certainty, r.holder.mine], [true, 'room', true]);
  assert.equal(JSON.parse(fake.rows(taskDb)[0].cells.Claim).by, 'u-ana');
});

test('claim, lost answer: an earlier attempt that stayed is recognised by its token even when later tries ran out', async () => {
  const { pa, clock } = await setup({ rows: [{ Task: 'One' }], room: EXACT, a: { sync: 'manual' } });
  let sleeps = 0;
  const sleep = async (ms) => { clock.advance(ms); sleeps += 1; if (sleeps === 4) pa.sync(); };
  const list = await (await Commons.connect({ artifact: pa.artifact, now: clock.now, sleep })).list('App — Tasks');
  pa.loseAnswer = true;
  const r = await list.claim(list.toArray()[0].id);
  assert.deepEqual([r.ok, r.certainty, r.holder.mine, exactWrites(pa).length], [true, 'room', true, 5]);
});

/* Two tabs of one viewer on a board with conditional writes. */
async function tabs() {
  const s = await board({ room: EXACT, b: { id: 'u-ana', name: 'Ana', kind: 'human' } });
  // The other tab acts between this tab's first write and whatever it does next.
  s.between = (act) => {
    const write = s.pa.artifact.db.write;
    let first = true;
    s.pa.artifact.db.write = async (...args) => { const r = await write(...args); if (first) { first = false; await act(); } return r; };
  };
  s.cell = () => s.fake.rows(s.taskDb)[0].cells.Claim;
  return s;
}

test('release, lost answer: a claim renewed from the viewer\'s other tab is not cleared by the retry', async () => {
  const { la, lb, one, pa, between, cell } = await tabs();
  const lost = [];
  await la.claim(one, { onLost: (h) => lost.push(h.mine) }); await settle();
  between(() => lb.claim(one));
  pa.failNext = NO_ANSWER;
  assert.equal(await la.release(one), false);
  assert.equal(JSON.parse(cell()).n, lb.held.get(one).token);
  assert.deepEqual(lost, [true]);
});

test('release issued while this page\'s claim is pending: it waits for the claim, and when a peer won it writes nothing', async () => {
  const { la, lb, one, pa, fake, taskDb } = await board({ room: EXACT });
  const write = pa.artifact.db.write;
  let first = true, release;
  // The peer's claim, and its snapshot, land while this page's claim write is on its way.
  pa.artifact.db.write = async (...args) => {
    if (first) { first = false; await lb.claim(one); await settle(); release = la.release(one); }
    return write(...args);
  };
  const claim = await la.claim(one);
  assert.deepEqual([claim.ok, claim.reason, await release], [false, 'held', false]);
  assert.equal(JSON.parse(fake.rows(taskDb)[0].cells.Claim).by, 'u-bot');
  assert.equal(pa.calls.filter((c) => c[0] === 'db.write' && c[4] === '').length, 0);
});

test('release issued right behind a claim that wins: it releases that claim', async () => {
  const { la, one, fake, taskDb } = await board({ room: EXACT });
  const [claim, released] = await Promise.all([la.claim(one), la.release(one)]);
  assert.deepEqual([claim.ok, released, fake.rows(taskDb)[0].cells.Claim, la.held.has(one)], [true, true, '', false]);
});

test('release, lost answer: an empty cell means released, whoever emptied it', async () => {
  const { la, one, pa, cell } = await tabs();
  await la.claim(one);
  pa.loseAnswer = true;
  assert.deepEqual([await la.release(one), cell()], [true, '']);
});

test('release, lost answer on a copy that never catches up: the outcome is reported as unknown, and the claim stays tracked', async () => {
  const { la, one, pa, fake, taskDb } = await board({ room: EXACT, a: { sync: 'manual' } });
  const lost = [];
  await la.claim(one, { onLost: (h) => lost.push(h) });
  pa.loseAnswer = true;
  await assert.rejects(la.release(one), code('OUTCOME_UNKNOWN'));
  assert.equal(la.held.has(one), true);
  assert.equal(fake.rows(taskDb)[0].cells.Claim, '');
  pa.sync();
  assert.deepEqual([la.claimOf(one), la.held.has(one), lost], [null, false, [null]], 'the sync shows the release went through');
});

test('claim, lost answer and then no read either: the result says "unknown", not a read error', async () => {
  const { la, one, pa } = await board({ room: EXACT });
  pa.loseAnswer = true;
  for (let i = 0; i < 20; i += 1) await pa.artifact.db.read('nope').catch(() => {});
  const r = await la.claim(one);
  assert.deepEqual([r.ok, r.reason, r.certainty], [false, 'unknown', 'local']);
});

test('release with plain writes is pinned to what this page sees, and says nothing about the room', async () => {
  const { la, lb, one } = await board({ b: { id: 'u-ana', name: 'Ana', kind: 'human' } });
  await la.claim(one); await settle();
  await lb.claim(one);
  assert.equal(await la.release(one), true, 'this tab has not seen the other tab\'s renewal, and a plain write cannot check');
});

test('claim, then renew and release at once while the own copy lags: each write builds on the last answer', async () => {
  const { la, one, fake, taskDb } = await board({ room: EXACT, a: { sync: 'manual' } });
  const lost = [];
  const out = [(await la.claim(one, { onLost: (h) => lost.push(h) })).certainty, (await la.claim(one)).certainty, await la.release(one)];
  assert.deepEqual([out, fake.rows(taskDb)[0].cells.Claim, lost], [['room', 'room', true], '', []]);
});

test('conditional writes: "offline" and "waiting for approval" are refusals with their own codes', async () => {
  const { la, one, pa } = await board({ room: EXACT });
  for (const [reason, c] of [['offline', 'OFFLINE'], ['this write is waiting for approval', 'APPROVAL_PENDING']]) {
    pa.failNext = reason;
    await assert.rejects(la.claim(one), code(c));
  }
  assert.equal(la.claimOf(one), null);
});

test('claim: a renewal whose own write shows up before it returns raises no false onLost', async () => {
  const { la, one, pa } = await board({ room: EXACT });
  const lost = [];
  await la.claim(one, { onLost: (h) => lost.push(h) });
  pa.loseAnswer = true;
  const again = await la.claim(one, { onLost: (h) => lost.push(h) });
  await settle();
  assert.deepEqual([again.ok, again.certainty, lost.length], [true, 'room', 0]);
});

test('release, conditional writes: a release does not wipe a claim someone else took meanwhile', async () => {
  const { la, one, pa, fake, taskDb, clock } = await board(Object.assign({ room: EXACT }, { a: { sync: 'manual' } }));
  const lost = [];
  await la.claim(one, { onLost: (h) => lost.push(h && h.id) });
  const theirs = JSON.stringify({ by: 'runner', name: 'Runner', kind: 'agent', until: clock.now() + 60000 });
  fake.serverWrite(taskDb, one, 'Claim', theirs);
  assert.equal(await la.release(one), false);
  assert.equal(fake.rows(taskDb)[0].cells.Claim, theirs);
  pa.sync();
  assert.equal(await la.release(one), false);
  assert.deepEqual(lost, ['runner']);
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

test('the removal marker, map deletion and the reason matching each live in one place in the library', () => {
  const src = require('node:fs').readFileSync(require.resolve('../commons-sdk.js'), 'utf8');
  assert.equal(src.match(/'Removed'/g).length, 1);
  assert.equal(src.match(/\bremoval\(/g).length, 1);
  assert.equal(src.match(/re\.test\(text\)/g).length, 1);
  assert.doesNotMatch(src, /json [!=]== ''/);
  const table = src.slice(src.indexOf('const REASONS = ['), src.indexOf('const CONDITIONAL = ['));
  assert.doesNotMatch(table, /consent_required|rate\(|version changed|no answer/);
});

test('reason codes of the unbuilt request design map only inside the experimental adapter', async () => {
  const { a, pa } = await setup();
  const l = await a.list('App — Tasks');
  for (const reason of ['consent_required', 'rate_page', 'rate']) {
    pa.failNext = reason;
    await assert.rejects(l.push({ Task: 'x' }), (e) => e.code === 'REFUSED' && e.reason === reason);
  }
});

test('a failed first read frees its watch: a retry works, and close leaves no watcher', async () => {
  const { a, pa, taskDb, clock } = await setup({ rows: [{ Task: 'One' }] });
  for (let i = 0; i < 20; i += 1) await pa.artifact.db.read(taskDb);
  await assert.rejects(a.list('App — Tasks'), code('RATE_LIMITED'));
  assert.equal(pa.watchers.size, 0);
  clock.advance(61000);
  assert.equal((await a.list('App — Tasks')).length, 1);
  assert.equal(pa.watchers.size, 1);
  a.close(); await settle();
  assert.equal(pa.watchers.size, 0);
});

test('map.update, conditional writes: two pages count together and lose nothing; a new key gets one row', async () => {
  const { a, b, fake, mapDb } = await setup({ room: EXACT });
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  const inc = (n) => (n || 0) + 1;
  const done = await Promise.all([ma.update('n', inc), mb.update('n', inc), ma.update('n', inc), mb.update('n', inc)]);
  await settle();
  assert.deepEqual(done.map((r) => r.mode), ['exact', 'exact', 'exact', 'exact']);
  assert.deepEqual(done.map((r) => r.value).sort(), [1, 2, 3, 4]);
  assert.deepEqual([ma.get('n'), mb.get('n'), fake.rows(mapDb).length], [4, 4, 1]);
});

test('map.update, plain writes: it says "best-effort", and two pages at once lose a step and make two rows', async () => {
  const { a, b, fake, mapDb } = await setup();
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  const inc = (n) => (n || 0) + 1;
  assert.deepEqual(await ma.update('solo', inc), { value: 1, mode: 'best-effort' });
  await settle();
  assert.deepEqual(await mb.update('solo', inc), { value: 2, mode: 'best-effort' });
  await Promise.all([ma.update('n', inc), mb.update('n', inc)]);
  await settle();
  assert.deepEqual([ma.get('n'), mb.get('n')], [1, 1]);
  assert.equal(fake.rows(mapDb).filter((r) => r.cells.Key === 'n').length, 2);
});

test('map.update: bad keys and values are refused, and a copy that never catches up ends as CONTENDED', async () => {
  const { a, b, pa } = await setup({ room: EXACT, a: { sync: 'manual' } });
  const ma = await a.map('App — State'), mb = await b.map('App — State');
  await assert.rejects(ma.update('', () => 1), code('BAD_ARGUMENT'));
  await assert.rejects(ma.update('k', () => undefined), code('BAD_ARGUMENT'));
  await mb.set('other', 1);
  await assert.rejects(ma.update('k', () => 1), code('CONTENDED'));
  pa.sync();
  assert.deepEqual(await ma.update('k', () => 1), { value: 1, mode: 'exact' });
});

const inc = (n) => (n || 0) + 1;
/* A counter at 1, two pages. The peer's increment lands between this page's write and its next read. */
async function counter(opts) {
  const s = await setup(Object.assign({ room: EXACT }, opts));
  s.fake.serverAddRow(s.mapDb, { Key: 'n', Value: '1' });
  s.pa.sync();
  s.ma = await s.a.map('App — State'); s.mb = await s.b.map('App — State');
  s.room = () => s.fake.rows(s.mapDb)[0].cells.Value;
  s.peerAfterFirstWrite = () => {
    const write = s.pa.artifact.db.write;
    let first = true;
    s.pa.artifact.db.write = async (...args) => { const r = await write(...args); if (first) { first = false; await s.mb.update('n', inc); } return r; };
  };
  return s;
}

test('map.update, lost answer: an update that never landed is not called exact because a peer wrote the same number', async () => {
  const { ma, pa, room, peerAfterFirstWrite } = await counter();
  peerAfterFirstWrite();
  pa.failNext = NO_ANSWER;
  await assert.rejects(ma.update('n', inc), (e) => e.code === 'OUTCOME_UNKNOWN' && e.value === 2 && typeof e.version === 'number' && e.reason === NO_ANSWER);
  assert.equal(room(), '2');
});

test('map.update, lost answer: an update that did land is not applied a second time', async () => {
  const { ma, pa, room, peerAfterFirstWrite } = await counter();
  let runs = 0;
  peerAfterFirstWrite();
  pa.loseAnswer = true;
  await assert.rejects(ma.update('n', (n) => { runs += 1; return inc(n); }), (e) => e.code === 'OUTCOME_UNKNOWN' && e.value === 3);
  assert.deepEqual([room(), runs], ['3', 1]);
});

test('map.update, own copy lagging: two and three updates in a row build on the acknowledged version and value', async () => {
  const { ma, mb, pa, room } = await counter({ a: { sync: 'manual' } });
  const got = [];
  for (let i = 0; i < 3; i += 1) got.push(await ma.update('n', inc));
  assert.deepEqual([got.map((r) => r.value), got.map((r) => r.mode), room(), ma.get('n')], [[2, 3, 4], ['exact', 'exact', 'exact'], '4', 4]);
  await ma.set('other', 1); await settle();
  assert.equal(ma.get('n'), 4, 'a stale snapshot of the own copy does not undo an acknowledged write');
  assert.deepEqual(await ma.update('n', inc), { value: 5, mode: 'exact' });
  await mb.set('n', 10); pa.sync();
  assert.equal(ma.get('n'), 10, 'a newer version from the room wins');
  assert.deepEqual(await ma.update('n', inc), { value: 11, mode: 'exact' });
  assert.equal(room(), '11');
});

test('map.update, an answer without a version: never wrong, but a lagging copy has to sync before the next update', async () => {
  const { ma, pa, room } = await counter({ room: Object.assign({ ackVersion: false }, EXACT), a: { sync: 'manual' } });
  assert.deepEqual(await ma.update('n', inc), { value: 2, mode: 'exact' });
  await assert.rejects(ma.update('n', inc), code('CONTENDED'));
  pa.sync();
  assert.deepEqual([await ma.update('n', inc), room()], [{ value: 3, mode: 'exact' }, '3']);
  const live = await counter({ room: Object.assign({ ackVersion: false }, EXACT) });
  assert.deepEqual([(await live.ma.update('n', inc)).value, (await live.ma.update('n', inc)).value, live.room()], [2, 3, '3']);
});

test('a plain set right after an update that created the key shows at once, and stays', async () => {
  const { a } = await setup({ room: EXACT });
  const m = await a.map('App — State');
  assert.equal((await m.update('k', () => 1)).value, 1);
  await m.set('k', 2);
  assert.equal(m.get('k'), 2, 'before any snapshot: the set sits on top of the acknowledged create');
  await settle();
  assert.equal(m.get('k'), 2);
});

test('a set on a row that only an acknowledged create supplies outlives a snapshot that does not hold the row yet', async () => {
  const { a } = await setup({ room: EXACT });
  const m = await a.map('App — State'), t = m.t, before = { version: t.base.version, props: t.props, rows: [] };
  await m.update('k', () => 1);
  await m.set('k', 2);
  t.ingest(before);
  assert.equal(m.get('k'), 2);
});

test('a snapshot whose rows are ahead of its version does not show a created row twice', async () => {
  const { a, fake, mapDb } = await setup({ room: EXACT });
  const m = await a.map('App — State'), t = m.t, version = t.base.version;
  await m.update('k', () => 1);
  t.ingest({ version, props: t.props, rows: fake.rows(mapDb) });
  assert.deepEqual([t.rows.length, t.sure.length, m.get('k')], [1, 1, 1]);
});

test('set, delete, push, list update and remove stay plain writes where conditional writes exist', async () => {
  const { a, pa } = await setup({ room: EXACT, rows: [{ Task: 'One' }] });
  const m = await a.map('App — State'), l = await a.list('App — Tasks');
  await m.set('k', 1); await m.delete('k');
  const id = await l.push({ Task: 'Two' });
  await l.update(id, { Result: 'x' }); await l.remove(id);
  assert.equal(pa.calls.filter((c) => /^db\.(write|createRow)$/.test(c[0])).length, 5);
  assert.equal(pa.calls.filter((c) => c.some((arg) => arg && typeof arg === 'object' && 'ifVersion' in arg)).length, 0);
});

test('calls issued back to back run in order, and each decides on what the earlier ones left', async () => {
  const { a, fake, mapDb } = await setup({ room: EXACT });
  const m = await a.map('App — State'), seen = [];
  const done = await Promise.all([m.set('k', 5), m.update('k', (n) => { seen.push(n); return n + 1; }), m.delete('k'), m.update('k', (n) => { seen.push(n); return 9; })]);
  assert.deepEqual([seen[0], seen[seen.length - 1], done[1].value, m.get('k'), fake.rows(mapDb).length], [5, undefined, 6, 9, 1]);
});

test('a plain write that waited for the viewer is shown when it is accepted, whatever arrived while it waited', async () => {
  const { pa, fake, taskDb, clock } = await setup({ rows: [{ Task: 'One' }], a: { consent: 'unasked' } });
  const sleep = async (ms) => { clock.advance(ms); await settle(1); };
  const l = await (await Commons.connect({ artifact: pa.artifact, now: clock.now, sleep })).list('App — Tasks'), id = l.toArray()[0].id;
  const saving = l.update(id, { Result: 'mine' });
  await settle();
  fake.serverWrite(taskDb, id, 'Status', 'Done'); await settle();
  pa.answer(true);
  await saving;
  assert.equal(l.get(id).cells.Result, 'mine');
});

test('after an update with an unknown outcome, a set issued right behind it is still shown', async () => {
  const { a, pa } = await setup({ room: EXACT });
  const m = await a.map('App — State');
  await m.set('k', 1); await settle();
  pa.failNext = NO_ANSWER;
  const [lost] = await Promise.allSettled([m.update('k', (n) => n + 1), m.set('k', 5)]);
  assert.deepEqual([lost.reason.code, m.get('k')], ['OUTCOME_UNKNOWN', 5]);
});

/* A create whose answer arrives after the room's next snapshot. `then` runs between the two. */
function lateAnswer(page, then) {
  const createRow = page.artifact.db.createRow;
  page.artifact.db.createRow = async (...args) => { const r = await createRow(...args); await then(r.rowId); await settle(); return r; };
}

test('a create acknowledged after the snapshot that removed the row does not bring the row back', async () => {
  for (const room of [EXACT, Object.assign({ ackVersion: false }, EXACT)]) {
    const { a, pa, fake, mapDb } = await setup({ room });
    const m = await a.map('App — State');
    lateAnswer(pa, (rowId) => fake.serverDeleteRow(mapDb, rowId));
    assert.equal((await m.update('k', () => 1)).mode, 'exact');
    assert.deepEqual([m.has('k'), fake.rows(mapDb).length], [false, 0]);
  }
});

test('a create acknowledged in the normal order, or after a snapshot that holds the row, shows the row once', async () => {
  const { a, pa, fake, mapDb } = await setup({ room: EXACT });
  const m = await a.map('App — State');
  await m.update('early', () => 1);
  assert.equal(m.get('early'), 1);
  lateAnswer(pa, () => {});
  await m.update('late', () => 2);
  assert.deepEqual([m.get('late'), m.t.rows.length, fake.rows(mapDb).length], [2, 2, 2]);
});

test('a plain create answered after the snapshot that removed the row does not bring the row back', async () => {
  const { a, pa, fake, taskDb } = await setup();
  const l = await a.list('App — Tasks');
  lateAnswer(pa, (rowId) => fake.serverDeleteRow(taskDb, rowId));
  await l.push({ Task: 'gone at once' });
  assert.deepEqual([l.length, fake.rows(taskDb).length], [0, 0]);
});

test('a snapshot older than the last one is dropped, even when it is newer than the last acknowledged write', async () => {
  const { a } = await setup({ room: EXACT, rows: [{ Task: 'One' }] });
  const l = await a.list('App — Tasks'), t = l.t, at = t.version;
  const snap = (version, title) => ({ version, props: t.props, rows: [{ id: 'r', cells: { Task: title } }] });
  t.ingest(snap(at + 5, 'newer'));
  t.ingest(snap(at + 3, 'older'));
  assert.deepEqual([t.version, l.toArray().map((r) => r.cells.Task)], [at + 5, ['newer']]);
  t.ingest(snap(at + 5, 'same version, own plain write'));
  assert.deepEqual(l.toArray().map((r) => r.cells.Task), ['same version, own plain write']);
});

test('map: with two rows for one key, delete empties the lowest; if that row is removed the other value returns', async () => {
  const { a, fake, mapDb } = await setup();
  const low = fake.serverAddRow(mapDb, { Key: 'k', Value: '1' });
  fake.serverAddRow(mapDb, { Key: 'k', Value: '2' });
  const m = await a.map('App — State');
  assert.equal(m.get('k'), 1);
  await m.delete('k'); await settle();
  assert.equal(m.has('k'), false);
  assert.deepEqual(fake.rows(mapDb).map((r) => r.cells.Value), ['', '2']);
  fake.serverDeleteRow(mapDb, low); await settle();
  assert.equal(m.get('k'), 2);
});

test('the file defines the global Commons even when another script defined a module', () => {
  const src = require('node:fs').readFileSync(require.resolve('../commons-sdk.js'), 'utf8');
  for (const foreign of [{}, { exports: {} }]) {
    const page = require('node:vm').createContext({ module: foreign });
    require('node:vm').runInContext(src, page);
    assert.equal(typeof page.Commons.connect, 'function');
  }
});

test('the module exports its limits, its error class and a version', () => {
  assert.equal(Commons.LIMITS.writeBytes, 20000);
  assert.equal(Commons.LIMITS.writesPerMinute, 30);
  assert.equal(Commons.LIMITS.readsPerMinute, 20);
  assert.ok(new Commons.CommonsError('X', 'm') instanceof Error);
  assert.match(Commons.version, /^\d+\.\d+\.\d+$/);
});
