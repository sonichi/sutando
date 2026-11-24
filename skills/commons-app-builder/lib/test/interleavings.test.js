'use strict';
/* Seeded interleavings: two viewers plus a second tab of the first run random operations while the
 * test releases write answers and snapshots in a random order. The same seed gives the same run. */
const test = require('node:test');
const assert = require('node:assert/strict');
const Commons = require('../commons-sdk.js');
const { createFakeRoom, createClock, NO_ANSWER } = require('./fake-room.js');

const MAP = [{ id: 'k', name: 'Key', type: 'title' }, { id: 'v', name: 'Value', type: 'text' }];
const TASKS = [
  { id: 't', name: 'Task', type: 'title' }, { id: 'c', name: 'Claim', type: 'text' },
  { id: 'r', name: 'Result', type: 'text' }, { id: 'x', name: 'Removed', type: 'text' },
];
// Claims and releases are drawn most often: they have the most orderings to get wrong.
const MIX = ['inc', 'inc', 'newKey', 'set', 'del', 'push', 'edit', 'hide', 'claim', 'claim', 'claim', 'release', 'release', 'release'];
const tick = () => new Promise((ok) => setImmediate(ok));
const inc = (n) => (n || 0) + 1;

function prng(seed) {
  let a = (seed + 1) * 0x9e3779b1;
  return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

async function run(seed, exact) {
  const rnd = prng(seed), pick = (list) => list[Math.floor(rnd() * list.length)];
  const clock = createClock();
  const fake = createFakeRoom({ clock, hold: true, versions: exact, conditional: exact });
  const mapDb = fake.addDatabase('State', MAP, [{ Key: 'n', Value: '0' }]);
  const taskDb = fake.addDatabase('Tasks', TASKS, [{ Task: 'one' }, { Task: 'two' }]);
  const counterRow = fake.rows(mapDb)[0].id;
  const tally = { exact: 0, unknown: 0, room: 0, released: 0 };
  const where = 'seed ' + seed + (exact ? ' (conditional)' : ' (plain)');

  const pages = [];
  for (const id of ['ana', 'bot', 'ana']) {
    const c = fake.client({ id, name: id });
    const room = await Commons.connect({ artifact: c.artifact, now: clock.now, sleep: async (ms) => { clock.advance(ms); } });
    pages.push({ c, map: await room.map('State'), list: await room.list('Tasks'), last: {} });
  }
  const mine = (p) => (w) => w.tab === p.c.tab;
  const anyRow = (p) => (rnd() < 0.05 ? 'gone' : pick(p.list.toArray().map((r) => r.id)) || 'gone');

  const ops = {
    async inc(p) {
      try { if ((await p.map.update('n', inc)).mode === 'exact') tally.exact += 1; } catch (e) {
        if (e.code !== 'OUTCOME_UNKNOWN') throw e;
        tally.unknown += 1;
      }
    },
    newKey: (p) => p.map.update(pick(['a', 'b']), () => Math.floor(rnd() * 9)),
    set: (p) => p.map.set(pick(['a', 'b']), Math.floor(rnd() * 9)),
    del: (p) => p.map.delete(pick(['a', 'b'])),
    push: (p) => p.list.push({ Task: 'new' }),
    edit: (p) => p.list.update(anyRow(p), { Result: 'r' + Math.floor(rnd() * 9) }),
    hide: (p) => p.list.remove(anyRow(p)),
    async claim(p) {
      const id = anyRow(p);
      const r = await p.list.claim(id, { ttl: 3600000 });
      if (!exact) assert.notEqual(r.certainty, 'room', where);
      if (!r.ok || r.certainty !== 'room') return;
      tally.room += 1;
      const token = p.list.held.get(id).token;
      const took = fake.log.find((w) => mine(p)(w) && w.exact && w.rowId === id && String(w.value).includes('"n":"' + token + '"'));
      assert.ok(took, where + ': a claim called "room" that the room never took');
      const before = took.prev ? JSON.parse(took.prev) : null;
      assert.ok(!before || before.by === p.c.me.id || before.until <= clock.now(), where + ': a claim took a row someone else held');
    },
    async release(p) {
      const id = anyRow(p), row = p.list.t.row(id), pinned = row && row.cells.Claim, from = fake.log.length;
      try { if (await p.list.release(id)) tally.released += 1; } finally {
        // Only a conditional write can promise this: a plain release cannot see what it overwrites.
        for (const w of fake.log.slice(from).filter(mine(p))) if (exact && w.rowId === id && w.prop === 'Claim' && w.value === '') assert.equal(w.prev, pinned, where + ': a release cleared another claim');
      }
    },
  };

  const script = async (p) => {
    for (let i = 0; i < 6; i += 1) {
      // A refusal is an outcome, not a failure of the run; a broken invariant is.
      try { await ops[pick(MIX)](p); } catch (e) { if (!(e instanceof Commons.CommonsError)) throw e; }
    }
  };
  let done = false, failure = null;
  Promise.all(pages.map(script)).then(() => { done = true; }, (e) => { failure = e; done = true; });

  for (let steps = 0; !done; steps += 1) {
    assert.ok(steps < 4000, where + ': the run did not end');
    await tick();
    if (rnd() < 0.08) {
      const victim = pick(fake.rows(rnd() < 0.5 ? mapDb : taskDb).filter((r) => r.id !== counterRow));
      if (victim) fake.serverDeleteRow(fake.rows(mapDb).some((r) => r.id === victim.id) ? mapDb : taskDb, victim.id);
    }
    if (exact && rnd() < 0.1) { const p = pick(pages); if (rnd() < 0.5) p.c.loseAnswer = true; else p.c.failNext = NO_ANSWER; }
    if (fake.pending.length) fake.pending.splice(Math.floor(rnd() * fake.pending.length), 1)[0].run();
    for (const p of pages) {
      for (const [name, t, db] of [['map', p.map.t, mapDb], ['list', p.list.t, taskDb]]) {
        if (!exact) continue;
        assert.ok(!(t.version < p.last[name]), where + ': a version went back');
        assert.ok(t.version <= fake.versionOf(db), where + ': a version the room never had');
        p.last[name] = t.version;
      }
    }
  }
  if (failure) throw failure;

  while (fake.pending.length) fake.pending.shift().run();
  for (const p of pages) p.c.refresh();
  await tick();

  const rows = fake.rows(taskDb), entries = new Map();
  for (const r of fake.rows(mapDb)) if (!entries.has(r.cells.Key)) entries.set(r.cells.Key, r.cells.Value);
  const expected = Array.from(entries).filter(([, v]) => v !== '' && v !== undefined && v !== null).map(([k, v]) => [k, JSON.parse(v)]).sort();
  for (const p of pages) {
    assert.deepEqual(p.list.toArray().map((r) => [r.id, r.cells]), rows.filter((r) => r.cells.Removed !== 'yes').map((r) => [r.id, r.cells]), where + ': the list is not the room');
    assert.deepEqual(p.map.entries().sort(), expected, where + ': the map is not the room');
    for (const [id, held] of p.list.held) {
      const row = rows.find((r) => r.id === id);
      assert.equal(row && JSON.parse(row.cells.Claim).n, held.token, where + ': a page still tracks a claim the room does not hold');
    }
  }
  const n = JSON.parse(fake.rows(mapDb).find((r) => r.id === counterRow).cells.Value);
  if (exact) assert.ok(n >= tally.exact && n <= tally.exact + tally.unknown, where + ': counter ' + n + ', exact ' + tally.exact + ', unknown ' + tally.unknown);
  return Object.assign({ n }, tally);
}

test('200 seeded interleavings with conditional writes keep every invariant', async () => {
  const sum = { exact: 0, unknown: 0, room: 0, released: 0 };
  for (let seed = 0; seed < 200; seed += 1) { const r = await run(seed, true); for (const k of Object.keys(sum)) sum[k] += r[k]; }
  // A run that never reaches these outcomes proves nothing about them.
  assert.ok(sum.exact > 100 && sum.unknown > 10 && sum.room > 100 && sum.released > 50, JSON.stringify(sum));
  if (process.env.COMMONS_SDK_TALLY) console.log(JSON.stringify(sum));
});

test('100 seeded interleavings with plain writes end with every page showing the room, and never say "room"', async () => {
  for (let seed = 0; seed < 100; seed += 1) await run(seed, false);
});

test('the same seed gives the same run', async () => {
  assert.deepEqual(await run(7, true), await run(7, true));
});
