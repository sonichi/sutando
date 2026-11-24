'use strict';
/* A fake room: one shared store, any number of clients, each with the `window.artifact` members the
 * library uses. Reason texts the guide does not quote (size, bad value, read rate) are the fake's own. */

const copy = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const ACTION_RE = /^[A-Za-z0-9._-]{1,64}$/;
const NO_ANSWER = 'no answer from the room; read again before deciding';

/** A clock tests move by hand. */
function createClock(start) {
  let t = start === undefined ? 1700000000000 : start;
  return { now: () => t, advance: (ms) => { t += ms; } };
}

/** Lets pending promise jobs and coalesced snapshots run. */
async function settle(turns) {
  for (let i = 0; i < (turns || 6); i += 1) await new Promise((ok) => setImmediate(ok));
}

function createFakeRoom(options) {
  const opt = options || {};
  const clock = opt.clock || createClock();
  const dbs = new Map();
  const clients = [];
  const posts = [];
  const requests = [];                 // experimental named-agent requests, oldest first
  const requestSubs = [];
  let ids = 0, scheduled = false;
  const dirty = new Set();
  const nextId = (p) => p + String(ids += 1).padStart(4, '0');

  // `versions` and `conditional` are the two host switches; both are off unless a test turns them on.
  const snapshot = (db) => {
    const snap = copy({ id: db.id, name: db.name, props: db.props, rows: db.rows });
    if (opt.versions) snap.version = db.version;
    return snap;
  };

  function deliver() {
    scheduled = false;
    const now = Array.from(dirty);
    dirty.clear();
    for (const c of clients) {
      for (const id of now) {
        if (c.manual || !c.watchers.has(id)) continue;
        // hold: "sync" hands over the room as it is when the page gets the snapshot, not as it was.
        const now = () => (dbs.has(id) ? snapshot(dbs.get(id)) : null), snap = opt.hold === 'sync' ? null : now();
        if (c.paused) c.missed.add(id);
        else later('snapshot', c.tab, () => { if (c.watchers.has(id)) c.watchers.get(id)(opt.hold === 'sync' ? now() : snap); });
      }
    }
  }

  // With `hold`, every snapshot and every write answer waits in `pending` until the test runs it.
  const pending = [], log = [];
  const later = (kind, tab, run) => { if (opt.hold) pending.push({ kind, tab, run }); else run(); };
  const answered = (result, tab, exact) => (opt.hold ? new Promise((ok) => later(exact ? 'exact-answer' : 'answer', tab, () => ok(result))) : result);

  // Changes are coalesced: one snapshot per database per turn, to every watcher, the writer too.
  function changed(id) {
    if (dbs.has(id)) dbs.get(id).version += 1;
    dirty.add(id);
    if (opt.hold) deliver();
    else if (!scheduled) { scheduled = true; setImmediate(deliver); }
  }

  function fits(db, name, value) {
    const prop = db.props.find((p) => p.name === name || p.id === name);
    if (!prop) return 'unknown property: ' + name;
    if (prop.type === 'created_by') return 'property is read-only: ' + name;
    if (value === null || value === '') return null;
    const optionNames = (prop.options || []).map((o) => o.name);
    if ((prop.type === 'select' || prop.type === 'status') && !optionNames.includes(value)) return 'value does not fit: ' + name;
    if (prop.type === 'number' && typeof value !== 'number') return 'value does not fit: ' + name;
    if ((prop.type === 'text' || prop.type === 'title') && typeof value !== 'string') return 'value does not fit: ' + name;
    return null;
  }

  const propName = (db, name) => db.props.find((p) => p.name === name || p.id === name).name;

  function addRow(db, cells, account, rowId) {
    const row = { id: rowId || nextId('r'), cells: {} };
    for (const [k, v] of Object.entries(cells)) row.cells[propName(db, k)] = copy(v);
    const stamp = db.props.find((p) => p.type === 'created_by');
    if (stamp && account) row.cells[stamp.name] = [account];
    db.rows.push(row);
    changed(db.id);
    return row.id;
  }

  const room = {
    clock, posts, requests, pending, log,

    versionOf(id) { return dbs.get(id).version; },

    /** Adds a database the way an agent would through Actions. Returns its id. */
    addDatabase(name, props, rows) {
      const db = { id: nextId('d'), name, props: copy(props), rows: [], version: 0 };
      dbs.set(db.id, db);
      for (const cells of rows || []) addRow(db, cells, null);
      return db.id;
    },

    removeDatabase(id) { dbs.delete(id); changed(id); },

    rows(id) { return copy(dbs.get(id).rows); },

    /** A write that does not come from a page: an agent using Actions. No consent, no page limits. */
    serverWrite(id, rowId, prop, value) {
      const db = dbs.get(id), row = db.rows.find((r) => r.id === rowId);
      row.cells[propName(db, prop)] = copy(value);
      changed(id);
    },

    serverAddRow(id, cells, account) { return addRow(dbs.get(id), cells, account || null); },

    /** An agent deleting a row through Actions: something a page cannot do. */
    serverDeleteRow(id, rowId) {
      const db = dbs.get(id);
      db.rows = db.rows.filter((r) => r.id !== rowId);
      changed(id);
    },

    /** The agent's side of an experimental request. */
    answerRequest(requestId, state, outcome) {
      const r = requests.find((x) => x.requestId === requestId);
      r.state = state; r.outcome = outcome || ''; r.updatedAt = clock.now();
      for (const sub of requestSubs) sub();
    },

    /** A page opened by one viewer. opts: {id, name, kind, consent: 'granted'|'unasked'|'denied', app, agents,
     *  inRoom: false, sync: 'manual' (plain writes stay in this page's own copy until `sync()`)}. */
    client(opts) {
      const o = Object.assign({ kind: 'human', consent: 'granted', agents: [] }, opts);
      const c = {
        me: { id: o.id, name: o.name, kind: o.kind }, account: '@' + o.id + ':fake',
        consent: o.consent, watchers: new Map(), paused: false, missed: new Set(),
        writes: [], reads: [], asks: [], postTimes: [], calls: [], dialogs: [], sent: [], keys: new Map(),
        dialog: 'yes', failNext: null, inRoom: o.inRoom !== false, tab: clients.length,
        manual: o.sync === 'manual', local: new Map(), queue: [], loseAnswer: false,
      };
      clients.push(c);
      let isReady = false;
      const call = (name, ...args) => c.calls.push([name, ...copy(args)]);
      const within = (list, max) => {
        while (list.length && list[0] <= clock.now() - 60000) list.shift();
        if (list.length >= max) return false;
        list.push(clock.now());
        return true;
      };
      // The access question: asked on the first call that needs it, answered by the test.
      const allowed = () => {
        if (c.consent === 'unasked') c.consent = 'asking';
        return c.consent === 'granted';
      };
      const writeGate = (value) => {
        if (!allowed()) return c.consent === 'denied' ? 'not allowed by the viewer' : 'waiting for the viewer’s answer';
        if (c.failNext) { const reason = c.failNext; c.failNext = null; return reason; }
        if (Buffer.byteLength(JSON.stringify(value === undefined ? null : value)) > 20000) return 'write too large';
        if (!within(c.writes, 30)) return 'rate limit: too many writes';
        return null;
      };
      const joined = () => clients.filter((x) => x.inRoom);
      // A manual page reads and plainly writes its own copy; any other page sees the room at once.
      const pull = () => { c.local.clear(); for (const [id, db] of dbs) c.local.set(id, copy(db)); };
      const view = (id) => (c.manual ? c.local.get(id) : dbs.get(id));
      const tell = (id) => { if (c.watchers.has(id)) c.watchers.get(id)(view(id) ? snapshot(view(id)) : null); };
      const exactly = (opts) => !!opts && 'ifVersion' in opts;
      const unfit = (opts) => {
        if (!opt.conditional) return 'conditional writes are not available here';
        return Number.isInteger(opts.ifVersion) && opts.ifVersion >= 0 ? null : 'invalid request';
      };
      const moved = (db, opts) => (db.version === opts.ifVersion ? null : 'version changed');
      const refused = (db, reason) => (reason === 'version changed' ? { ok: false, reason, version: db.version } : { ok: false, reason });
      // A conditional write that landed can still lose its answer on the way back.
      const answer = (db, extra) => {
        if (c.loseAnswer) { c.loseAnswer = false; return { ok: false, reason: NO_ANSWER }; }
        return Object.assign({ ok: true }, opt.ackVersion === false ? null : { version: db.version }, extra);
      };
      if (c.manual) pull();

      const artifact = {
        ready: new Promise((ok) => setImmediate(() => { isReady = true; ok(); })),
        get me() { return isReady ? copy(c.me) : null; },
        now: () => (o.nanClock ? NaN : clock.now()),
        db: {
          list: async () => { call('db.list'); return Array.from(dbs.values()).map((d) => ({ id: d.id, name: d.name })); },
          read: async (id) => {
            call('db.read', id);
            if (!within(c.reads, 20)) throw new Error('rate limit: too many reads');
            if (!view(id)) throw new Error('database not found');
            return snapshot(view(id));
          },
          write: async (id, rowId, prop, value, opts) => {
            call('db.write', id, rowId, prop, value, ...(opts ? [opts] : []));
            const exact = exactly(opts), db = exact ? dbs.get(id) : view(id);
            const row = db && db.rows.find((r) => r.id === rowId);
            if (!row) return { ok: false, reason: 'row not found' };
            const reason = writeGate(value) || fits(db, prop, value) || (exact && (unfit(opts) || moved(db, opts)));
            if (reason) return answered(refused(db, reason), c.tab, exact);
            const prev = row.cells[propName(db, prop)];
            row.cells[propName(db, prop)] = copy(value);
            if (c.manual && !exact) {
              c.queue.push({ id, rowId, prop, value: copy(value) });
              setImmediate(() => tell(id));
              return { ok: true };
            }
            changed(id);
            log.push({ tab: c.tab, db: id, exact, rowId, prop: propName(db, prop), value: copy(value), prev, version: db.version, time: clock.now() });
            return answered(exact ? answer(db) : { ok: true }, c.tab, exact);
          },
          createRow: async (id, cells, opts) => {
            call('db.createRow', id, cells, ...(opts ? [opts] : []));
            const exact = exactly(opts), db = exact ? dbs.get(id) : view(id);
            if (!db) return { ok: false, reason: 'database not found' };
            let reason = writeGate(cells);
            for (const [k, v] of Object.entries(cells)) reason = reason || fits(db, k, v);
            reason = reason || (exact && (unfit(opts) || moved(db, opts)));
            if (reason) return answered(refused(db, reason), c.tab, exact);
            if (c.manual && !exact) {
              const row = { id: nextId('r'), cells: {} };
              for (const [k, v] of Object.entries(cells)) row.cells[propName(db, k)] = copy(v);
              db.rows.push(row);
              c.queue.push({ id, rowId: row.id, cells: copy(cells) });
              setImmediate(() => tell(id));
              return { ok: true, rowId: row.id };
            }
            const rowId = addRow(dbs.get(id), cells, c.account);
            log.push({ tab: c.tab, db: id, exact, rowId, cells: copy(cells), version: db.version, time: clock.now() });
            return answered(exact ? answer(db, { rowId }) : { ok: true, rowId }, c.tab, exact);
          },
          onChange: (id, fn) => {
            call('db.onChange', id);
            if (c.watchers.has(id)) throw new Error('one watch per database');
            c.watchers.set(id, fn);
            return () => c.watchers.delete(id);
          },
        },
        members: async () => {
          call('members');
          if (!allowed()) throw new Error('page permission required');
          return joined().map((x) => ({ id: x.me.id, name: x.me.name, avatar: null, kind: x.me.kind }));
        },
        personId: async (cell) => {
          call('personId', cell);
          if (!allowed()) throw new Error('page permission required');
          const hit = Array.isArray(cell) && joined().find((x) => x.account === cell[0]);
          return hit ? hit.me.id : null;
        },
        agent: {
          list: async () => { call('agent.list'); return c.consent === 'granted' ? copy(o.agents) : []; },
          sendTask: async (req) => {
            call('agent.sendTask', req);
            if (!allowed()) return { ok: false, reason: 'page permission required' };
            if (!KEY_RE.test(req.key || '') || (req.actionId !== undefined && !ACTION_RE.test(req.actionId))) return { ok: false, reason: 'invalid request' };
            if (c.keys.has(req.key)) return copy(c.keys.get(req.key));
            if (!o.agents.some((a) => a.id === req.agentId)) return { ok: false, reason: 'agent not owned or unavailable' };
            c.dialogs.push(req.text);
            if (c.dialog !== 'yes') return { ok: false, reason: 'declined' };
            if (!within(c.asks, 10)) return { ok: false, reason: 'too many agent requests' };
            const result = { ok: true, eventId: nextId('$task') };
            c.keys.set(req.key, result);
            c.sent.push(copy(req));
            return copy(result);
          },
        },
        room: {
          post: async (msg) => {
            call('room.post', msg);
            if (/@room\b/.test(msg.text)) return { posted: false, reason: 'a page cannot mention a group' };
            const named = Array.from(msg.text.matchAll(/<@([^>]+)>/g)).map((m) => m[1]);
            if (named.some((id) => !joined().some((x) => x.me.id === id))) return { posted: false, reason: 'mention is not a joined member' };
            c.dialogs.push(msg.text);
            if (c.dialog !== 'yes') return { posted: false, reason: 'declined' };
            if (!within(c.postTimes, 10)) return { posted: false, reason: 'too many room posts' };
            const eventId = nextId('$post');
            posts.push({ by: c.me.id, text: msg.text, eventId });
            return { posted: true, eventId };
          },
        },
      };
      if (o.app) artifact.app = copy(o.app);

      return Object.assign(c, {
        artifact,
        /** The viewer answers the access question. */
        answer(yes) { c.consent = yes ? 'granted' : 'denied'; },
        /** A manual page meets the room: its queued writes land in order, then it takes the room's state. */
        sync() {
          for (const op of c.queue.splice(0)) {
            const db = dbs.get(op.id), row = db && db.rows.find((r) => r.id === op.rowId);
            if (db && op.cells) addRow(db, op.cells, c.account, op.rowId);
            else if (row) { row.cells[propName(db, op.prop)] = copy(op.value); changed(op.id); }
          }
          const watched = Array.from(c.watchers.keys());
          pull();
          for (const id of watched) tell(id);
        },
        /** Hands this page the room as it is now, for every database it watches. */
        refresh() { for (const id of Array.from(c.watchers.keys())) c.watchers.get(id)(dbs.has(id) ? snapshot(dbs.get(id)) : null); },
        /** Hold this page's snapshots back, as a slow connection would. */
        pause() { c.paused = true; },
        resume() {
          c.paused = false;
          for (const id of c.missed) if (c.watchers.has(id)) c.watchers.get(id)(dbs.has(id) ? snapshot(dbs.get(id)) : null);
          c.missed.clear();
        },
        /** EXPERIMENTAL: add the named-agent request calls, as designed and not built. */
        enableRequests(agents) {
          const listFor = (scope) => copy(requests.filter((r) => (scope.row ? r.row.rowId === scope.row.rowId : r.row.database === scope.database)));
          artifact.agent.requestable = async () => copy(agents);
          artifact.agent.request = async (req) => {
            call('agent.request', req);
            if (c.consent !== 'granted') return { ok: false, reason: 'consent_required' };
            const agent = agents.find((a) => a.id === req.agentId);
            if (!agent) return { ok: false, reason: 'not_requestable' };
            if (!agent.actions.includes(req.action)) return { ok: false, reason: 'action_not_allowed' };
            const open = requests.find((r) => r.row.rowId === req.row.rowId && r.action === req.action && (r.state === 'sent' || r.state === 'working'));
            if (open) return { ok: true, requestId: open.requestId, duplicate: true };
            if (c.dialog !== 'yes') return { ok: false, reason: 'declined' };
            if (!within(c.asks, 10)) return { ok: false, reason: 'rate' };
            const r = { requestId: nextId('q'), action: req.action, state: 'sent', outcome: '', presser: c.me.id, row: copy(req.row), updatedAt: clock.now(), expiresAt: clock.now() + 600000 };
            requests.push(r);
            for (const sub of requestSubs) sub();
            return { ok: true, requestId: r.requestId };
          };
          artifact.agent.requests = async (scope, opts) => {
            if (opts && opts.onChange) requestSubs.push(() => opts.onChange(listFor(scope)));
            return listFor(scope);
          };
        },
      });
    },
  };
  return room;
}

module.exports = { createFakeRoom, createClock, settle, NO_ANSWER };
