'use strict';
/* Seeded interleavings: two viewers plus a second tab of the first run random operations, some
 * issued back to back, while the test releases write answers and snapshots. Same seed, same run. */
const test = require('node:test');
const assert = require('node:assert/strict');
const Commons = require('../commons-sdk.js');
const { createFakeRoom, createClock, NO_ANSWER } = require('./fake-room.js');

const MAP = [{ id: 'k', name: 'Key', type: 'title' }, { id: 'v', name: 'Value', type: 'text' }];
const TASKS = [
  { id: 't', name: 'Task', type: 'title' }, { id: 'c', name: 'Claim', type: 'text' },
  { id: 'r', name: 'Result', type: 'text' }, { id: 'x', name: 'Removed', type: 'text' },
];
// Claims, releases and back-to-back bursts are drawn most often: they have the most orderings to get wrong.
const MIX = ['inc', 'inc', 'update', 'set', 'del', 'push', 'edit', 'hide', 'claim', 'claim', 'release', 'mapBurst', 'mapBurst', 'claimBurst', 'claimBurst', 'overlap', 'overlap', 'overlap'];
const tick = () => new Promise((ok) => setImmediate(ok));
const inc = (n) => (n || 0) + 1;
const blank = (v) => v === undefined || v === null || v === '';
// The view that holds nothing the bridge has not answered for.
const sure = (t) => t.sure || t.rows;

function prng(seed) {
  let a = (seed + 1) * 0x9e3779b1;
  return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

/* mode: "ordered" (a late snapshot shows the room as it is when it arrives), "scrambled" (a late
 * snapshot is stale, and they come in any order), or "plain" (ordered, with no conditional writes). */
async function run(seed, mode) {
  const exact = mode !== 'plain', ordered = mode !== 'scrambled';
  const rnd = prng(seed), pick = (list) => list[Math.floor(rnd() * list.length)];
  const clock = createClock();
  const fake = createFakeRoom({ clock, hold: ordered ? 'sync' : true, versions: exact, conditional: exact });
  const mapDb = fake.addDatabase('State', MAP, [{ Key: 'n', Value: '0' }]);
  const taskDb = fake.addDatabase('Tasks', TASKS, [{ Task: 'one' }, { Task: 'two' }]);
  const counterRow = fake.rows(mapDb)[0].id;
  const tally = { exact: 0, unknown: 0, room: 0, released: 0, bursts: 0, thrown: 0 };
  const where = 'seed ' + seed + ' (' + mode + ')';
  let unique = 100;
  const fresh = () => (unique += 1);

  // Where each value sits in the room's own order of writes. Values are unique per cell, so a
  // page that shows an earlier index after a later one has gone back in time.
  const order = new Map();
  let read = 0;
  const indexOf = (rowId, prop, value) => {
    for (; read < fake.log.length; read += 1) {
      const w = fake.log[read];
      for (const [k, v] of w.cells ? Object.entries(w.cells) : [[w.prop, w.value]]) order.set(w.rowId + '|' + k + '|' + JSON.stringify(v), read);
    }
    return order.get(rowId + '|' + prop + '|' + JSON.stringify(value));
  };

  const pages = [];
  for (const id of ['ana', 'bot', 'ana']) {
    const c = fake.client({ id, name: id });
    const room = await Commons.connect({ artifact: c.artifact, now: clock.now, sleep: async (ms) => { clock.advance(ms); }, report: () => { tally.thrown += 1; } });
    pages.push({ c, map: await room.map('State'), list: await room.list('Tasks'), last: {}, shown: new Map() });
  }
  // The page's own functions break now and then. That must change nothing below.
  const flaky = () => { if (rnd() < 0.1) throw new Error('the page broke'); };
  for (const p of pages) { p.map.observe(flaky); p.list.observe(flaky); }
  const anyRow = (p) => (rnd() < 0.05 ? 'gone' : pick(p.list.toArray().map((r) => r.id)) || 'gone');

  // Read your writes: right after a definite success, before any further snapshot, the page
  // shows that write, or something the room put after it, or its own later intent.
  const reflects = (p, handle, from, what, isMine) => {
    let mine = -1;
    for (let i = fake.log.length - 1; i >= from && mine < 0; i -= 1) if (fake.log[i].tab === p.c.tab && isMine(fake.log[i])) mine = i;
    if (mine < 0) return;
    const w = fake.log[mine], row = handle.t.rows.find((r) => r.id === w.rowId);
    // A stale snapshot that arrives while a plain write is in flight is trusted over that write.
    if (!row || (!ordered && !w.exact)) return;
    // A later call of this page that is still pending is shown on top: that is its own newer intent.
    if ((handle.t.entries || handle.ops).some((e) => (e.status || 'pending') === 'pending' && !e.quiet)) return;
    for (const [prop, value] of w.cells ? Object.entries(w.cells) : [[w.prop, w.value]]) {
      const seen = row.cells[prop];
      if (blank(value) ? blank(seen) : JSON.stringify(seen) === JSON.stringify(value)) continue;
      // An empty cell is no unique value: it is newer only if the room emptied the cell after this write.
      const at = blank(seen) ? fake.log.findIndex((x, i) => i > mine && x.rowId === w.rowId && x.prop === prop && blank(x.value)) : indexOf(w.rowId, prop, seen);
      assert.ok(at === undefined || at > mine, where + ': ' + what + ' succeeded but the page shows ' + JSON.stringify(seen) + ', not ' + JSON.stringify(value));
    }
  };
  // `isMine(result)` gives the test for this call's own write in the room's log, or null for "no success".
  const after = async (p, handle, what, promise, isMine) => {
    const from = fake.log.length, r = await promise, match = isMine(r);
    if (match) reflects(p, handle, from, what, match);
    return r;
  };
  const wrote = (json) => () => (w) => (w.cells ? w.cells.Value : w.prop === 'Value' && w.value) === json;

  const ops = {
    async inc(p) {
      try {
        const r = await p.map.update('n', inc);
        if (r.mode === 'exact') tally.exact += 1;
        if (r.mode === 'exact') assert.ok(p.map.get('n') >= r.value, where + ': the counter shows less than the value just written');
      } catch (e) {
        if (e.code !== 'OUTCOME_UNKNOWN') throw e;
        tally.unknown += 1;
      }
    },
    update(p, key) { const v = fresh(); return after(p, p.map, 'map.update', p.map.update(key || pick(['a', 'b']), () => v), wrote(JSON.stringify(v))); },
    set(p, key) { const v = fresh(); return after(p, p.map, 'map.set', p.map.set(key || pick(['a', 'b']), v), wrote(JSON.stringify(v))); },
    del: (p, key) => p.map.delete(key || pick(['a', 'b'])),
    push: (p) => after(p, p.list, 'list.push', p.list.push({ Task: 'new' }), (id) => (w) => !!w.cells && w.rowId === id),
    edit(p) { const v = 'r' + fresh(); return after(p, p.list, 'list.update', p.list.update(anyRow(p), { Result: v }), () => (w) => w.value === v); },
    hide: (p) => p.list.remove(anyRow(p)),
    async claim(p, row) {
      const id = row || anyRow(p), token = (r) => r.ok && p.list.held.get(id).token;
      const r = await after(p, p.list, 'list.claim', p.list.claim(id, { ttl: 3600000, onLost: flaky }), (x) => x.ok && ((w) => w.rowId === id && String(w.value).includes('"n":"' + token(x) + '"')));
      if (!exact) assert.notEqual(r.certainty, 'room', where);
      if (!r.ok || r.certainty !== 'room') return;
      tally.room += 1;
      assert.ok(fake.log.some((w) => w.tab === p.c.tab && w.exact && w.rowId === id && String(w.value).includes('"n":"' + token(r) + '"')), where + ': a claim called "room" that the room never took');
    },
    async release(p, row) {
      const id = row || anyRow(p);
      if (await after(p, p.list, 'list.release', p.list.release(id), (x) => x === true && ((w) => w.rowId === id && w.prop === 'Claim' && w.value === ''))) tally.released += 1;
    },
    // Back to back, not awaited: later calls are issued while earlier ones are still pending.
    mapBurst(p) {
      const key = pick(['a', 'b']);
      tally.bursts += 1;
      return burst([0, 1, 2].slice(0, 2 + Math.floor(rnd() * 2)).map(() => ops[pick(['set', 'update', 'del', 'set', 'update'])](p, key)));
    },
    claimBurst(p) {
      const id = anyRow(p);
      tally.bursts += 1;
      return burst(pick([['claim', 'release'], ['claim', 'claim'], ['release', 'claim'], ['claim', 'release', 'claim']]).map((name) => ops[name](p, id)));
    },
  };
  // A second call issued a few deliveries after the first, while the first is still pending:
  // time enough for a peer's competing write to land in between.
  ops.overlap = async (p) => {
    const id = anyRow(p), key = pick(['a', 'b']), onRow = rnd() < 0.7;
    const first = onRow ? ops.claim(p, id) : ops.update(p, key);
    first.catch(() => {});
    for (let i = Math.floor(rnd() * 3); i > 0; i -= 1) await tick();
    tally.bursts += 1;
    return burst([first, onRow ? ops[pick(['release', 'release', 'claim'])](p, id) : ops[pick(['set', 'del', 'update'])](p, key)]);
  };
  // A refusal is an outcome, not a failure of the run; a broken invariant is.
  const refusalOnly = (e) => { if (!(e instanceof Commons.CommonsError)) throw e; };
  const burst = async (promises) => { for (const r of await Promise.allSettled(promises)) if (r.status === 'rejected') refusalOnly(r.reason); };

  const script = async (p) => { for (let i = 0; i < 6; i += 1) await Promise.resolve(ops[pick(MIX)](p)).catch(refusalOnly); };
  let done = false, failure = null;
  Promise.all(pages.map(script)).then(() => { done = true; }, (e) => { failure = e; done = true; });

  const deliver = () => { if (fake.pending.length) fake.pending.splice(Math.floor(rnd() * fake.pending.length), 1)[0].run(); };
  const watch = () => {
    for (const p of pages) {
      for (const [name, t, db, props] of [['map', p.map.t, mapDb, ['Value']], ['list', p.list.t, taskDb, ['Claim', 'Result']]]) {
        if (exact) {
          assert.ok(!(t.version < p.last[name]), where + ': a version went back');
          assert.ok(t.version <= fake.versionOf(db), where + ': a version the room never had');
          p.last[name] = t.version;
        }
        if (!ordered) continue;
        for (const row of sure(t)) {
          for (const prop of props) {
            const at = blank(row.cells[prop]) ? undefined : indexOf(row.id, prop, row.cells[prop]), key = row.id + '|' + prop;
            if (at === undefined) continue;
            assert.ok(!(at < p.shown.get(key)), where + ': ' + name + ' went back to an older value of ' + prop);
            p.shown.set(key, at);
          }
        }
      }
    }
  };

  for (let steps = 0; !done; steps += 1) {
    assert.ok(steps < 6000, where + ': the run did not end');
    await tick();
    if (failure) break;
    if (rnd() < 0.06) {
      const db = rnd() < 0.5 ? mapDb : taskDb, victim = pick(fake.rows(db).filter((r) => r.id !== counterRow));
      if (victim) fake.serverDeleteRow(db, victim.id);
    }
    if (exact && rnd() < 0.1) { const p = pick(pages); if (rnd() < 0.5) p.c.loseAnswer = true; else p.c.failNext = NO_ANSWER; }
    deliver();
    watch();
  }
  if (failure) throw failure;

  while (fake.pending.length) fake.pending.shift().run();
  // With versions a page must reach the room's state from what it was sent, with no fresh copy to
  // undo a mistake. Without them it cannot drop a snapshot that arrives out of order, so it gets one.
  if (!exact) for (const p of pages) p.c.refresh();
  await tick();

  const rows = fake.rows(taskDb), entries = new Map();
  for (const r of fake.rows(mapDb)) if (!entries.has(r.cells.Key)) entries.set(r.cells.Key, r.cells.Value);
  const expected = Array.from(entries).filter(([, v]) => !blank(v)).map(([k, v]) => [k, JSON.parse(v)]).sort();
  for (const p of pages) {
    assert.deepEqual(p.list.toArray().map((r) => [r.id, r.cells]), rows.filter((r) => r.cells.Removed !== 'yes').map((r) => [r.id, r.cells]), where + ': the list is not the room');
    assert.deepEqual(p.map.entries().sort(), expected, where + ': the map is not the room');
    for (const [id, held] of p.list.held) {
      const row = rows.find((r) => r.id === id);
      assert.equal(row && JSON.parse(row.cells.Claim).n, held.token, where + ': a page still tracks a claim the room does not hold');
    }
  }
  // Only a conditional write can promise this: a plain write cannot see what it overwrites.
  for (const w of fake.log.filter((x) => x.exact && x.prop === 'Claim')) {
    const before = blank(w.prev) ? null : JSON.parse(w.prev), by = pages.find((p) => p.c.tab === w.tab).c.me.id;
    if (blank(w.value)) assert.ok(before && before.by === by, where + ': a release cleared a claim that was not this viewer\'s');
    else assert.ok(!before || before.by === by || before.until <= w.time, where + ': a claim took a row someone else held');
  }
  const n = JSON.parse(fake.rows(mapDb).find((r) => r.id === counterRow).cells.Value);
  if (exact) assert.ok(n >= tally.exact && n <= tally.exact + tally.unknown, where + ': counter ' + n + ', exact ' + tally.exact + ', unknown ' + tally.unknown);
  return Object.assign({ n }, tally);
}

async function sweep(mode, count) {
  const sum = { exact: 0, unknown: 0, room: 0, released: 0, bursts: 0, thrown: 0 }, failed = [];
  for (let seed = 0; seed < count; seed += 1) {
    try { const r = await run(seed, mode); for (const k of Object.keys(sum)) sum[k] += r[k]; } catch (e) { failed.push(e.message.split('\n')[0]); }
  }
  if (process.env.COMMONS_SDK_TALLY) {
    const kinds = {};
    for (const m of failed) { const k = m.replace(/^seed \d+ \(\w+\): /, '').replace(/ shows .*| \d.*/, ''); kinds[k] = (kinds[k] || 0) + 1; }
    console.log(mode, JSON.stringify(sum), 'failed seeds:', failed.length, JSON.stringify(kinds));
    if (process.env.COMMONS_SDK_TALLY === '2') console.log(failed.slice(0, 6).join('\n'));
  }
  assert.deepEqual(failed.slice(0, 5), [], failed.length + ' of ' + count + ' seeds failed');
  return sum;
}

test('600 seeded interleavings, conditional writes, late snapshots show the room as it is: every invariant holds', async () => {
  const sum = await sweep('ordered', 600);
  // A run that never reaches these outcomes proves nothing about them.
  assert.ok(sum.exact > 300 && sum.unknown > 30 && sum.room > 300 && sum.released > 100 && sum.bursts > 500 && sum.thrown > 500, JSON.stringify(sum));
});

test('200 seeded interleavings, conditional writes, stale snapshots in any order at all', async () => {
  await sweep('scrambled', 200);
});

test('200 seeded interleavings, plain writes only: pages end showing the room and never say "room"', async () => {
  await sweep('plain', 200);
});

test('the same seed gives the same run', async () => {
  assert.deepEqual(await run(7, 'ordered'), await run(7, 'ordered'));
});
