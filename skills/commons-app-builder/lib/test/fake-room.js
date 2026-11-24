'use strict';
/* A fake room: one shared store, any number of clients, each with the `window.artifact` members the
 * library uses. Reason texts the guide does not quote (size, bad value, read rate) are the fake's own. */

const copy = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const ACTION_RE = /^[A-Za-z0-9._-]{1,64}$/;

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
  const clock = (options && options.clock) || createClock();
  const dbs = new Map();
  const clients = [];
  const posts = [];
  const requests = [];                 // experimental named-agent requests, oldest first
  const requestSubs = [];
  let ids = 0, scheduled = false;
  const dirty = new Set();
  const nextId = (p) => p + String(ids += 1).padStart(4, '0');

  const snapshot = (db) => copy({ id: db.id, name: db.name, props: db.props, rows: db.rows });

  function deliver() {
    scheduled = false;
    const now = Array.from(dirty);
    dirty.clear();
    for (const c of clients) {
      for (const id of now) {
        if (!c.watchers.has(id)) continue;
        if (c.paused) c.missed.add(id);
        else c.watchers.get(id)(dbs.has(id) ? snapshot(dbs.get(id)) : null);
      }
    }
  }

  // Changes are coalesced: one snapshot per database per turn, to every watcher, the writer too.
  function changed(id) {
    dirty.add(id);
    if (!scheduled) { scheduled = true; setImmediate(deliver); }
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

  function addRow(db, cells, account) {
    const row = { id: nextId('r'), cells: {} };
    for (const [k, v] of Object.entries(cells)) row.cells[propName(db, k)] = copy(v);
    const stamp = db.props.find((p) => p.type === 'created_by');
    if (stamp && account) row.cells[stamp.name] = [account];
    db.rows.push(row);
    changed(db.id);
    return row.id;
  }

  const room = {
    clock, posts, requests,

    /** Adds a database the way an agent would through Actions. Returns its id. */
    addDatabase(name, props, rows) {
      const db = { id: nextId('d'), name, props: copy(props), rows: [] };
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

    /** The agent's side of an experimental request. */
    answerRequest(requestId, state, outcome) {
      const r = requests.find((x) => x.requestId === requestId);
      r.state = state; r.outcome = outcome || ''; r.updatedAt = clock.now();
      for (const sub of requestSubs) sub();
    },

    /** A page opened by one viewer. opts: {id, name, kind, consent: 'granted'|'unasked'|'denied',
     *  app, agents: [{id, name, kind, userId?}], inRoom: false to leave the viewer out of members}. */
    client(opts) {
      const o = Object.assign({ kind: 'human', consent: 'granted', agents: [] }, opts);
      const c = {
        me: { id: o.id, name: o.name, kind: o.kind }, account: '@' + o.id + ':fake',
        consent: o.consent, watchers: new Map(), paused: false, missed: new Set(),
        writes: [], reads: [], asks: [], postTimes: [], calls: [], dialogs: [], sent: [], keys: new Map(),
        dialog: 'yes', failNext: null, inRoom: o.inRoom !== false,
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

      const artifact = {
        ready: new Promise((ok) => setImmediate(() => { isReady = true; ok(); })),
        get me() { return isReady ? copy(c.me) : null; },
        now: () => (o.nanClock ? NaN : clock.now()),
        db: {
          list: async () => { call('db.list'); return Array.from(dbs.values()).map((d) => ({ id: d.id, name: d.name })); },
          read: async (id) => {
            call('db.read', id);
            if (!within(c.reads, 20)) throw new Error('rate limit: too many reads');
            if (!dbs.has(id)) throw new Error('database not found');
            return snapshot(dbs.get(id));
          },
          write: async (id, rowId, prop, value) => {
            call('db.write', id, rowId, prop, value);
            const db = dbs.get(id), row = db && db.rows.find((r) => r.id === rowId);
            if (!row) return { ok: false, reason: 'row not found' };
            const reason = writeGate(value) || fits(db, prop, value);
            if (reason) return { ok: false, reason };
            row.cells[propName(db, prop)] = copy(value);
            changed(id);
            return { ok: true };
          },
          createRow: async (id, cells) => {
            call('db.createRow', id, cells);
            const db = dbs.get(id);
            if (!db) return { ok: false, reason: 'database not found' };
            let reason = writeGate(cells);
            for (const [k, v] of Object.entries(cells)) reason = reason || fits(db, k, v);
            if (reason) return { ok: false, reason };
            return { ok: true, rowId: addRow(db, cells, c.account) };
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

module.exports = { createFakeRoom, createClock, settle };
