/*! commons-sdk 0.1.0: a small client library for Room Commons apps. No dependencies. */
(function (root, factory) {
  const api = factory();
  // The global is always set: a page's other inlined script may define a `module` of its own.
  if (root) root.Commons = api;
  if (typeof module === 'object' && module && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const LIMITS = Object.freeze({ writeBytes: 20000, writesPerMinute: 30, readsPerMinute: 20, players: 200, mentions: 20 });
  const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  const ACTION_RE = /^[A-Za-z0-9._-]{1,64}$/;

  /** Every failure the library reports: `code` is stable, `reason` is the bridge's own text.
   *  The README lists the codes. */
  class CommonsError extends Error {
    constructor(code, message, reason) {
      super(message);
      this.name = 'CommonsError';
      this.code = code;
      if (reason !== undefined) this.reason = reason;
    }
  }

  /* Reason text is matched in one place, refusal(): over REASONS, the strings GUIDE.md quotes,
     and at most one more table that a caller names. Callers read `code` only. */
  const REASONS = [
    [/waiting for the viewer/i, 'CONSENT_PENDING'],
    [/not allowed by the viewer/i, 'CONSENT_DENIED'],
    [/page permission required/i, 'CONSENT_REQUIRED'],
    [/rate limit|too many/i, 'RATE_LIMITED'],
    [/^declined$/i, 'DECLINED'],
    [/agent not owned or unavailable/i, 'AGENT_UNAVAILABLE'],
    [/another request is in progress/i, 'BUSY'],
    [/request outcome unknown/i, 'OUTCOME_UNKNOWN'],
    [/mention is not a joined member|cannot mention a group/i, 'BAD_MENTION'],
  ];

  /* A conditional write's reasons, as the host's page API document words them. */
  const CONDITIONAL = [
    [/version changed/i, 'VERSION_CHANGED'],
    [/conditional writes are not available|invalid request/i, 'CONDITIONAL_UNAVAILABLE'],
    [/no answer from the room/i, 'NO_ANSWER'],
    [/^offline$/i, 'OFFLINE'],
    [/waiting for approval/i, 'APPROVAL_PENDING'],
  ];

  function refusal(reason, fallback, extra) {
    const text = String(reason == null ? '' : reason);
    const hit = (extra || []).concat(REASONS).find(([re]) => re.test(text));
    return new CommonsError(hit ? hit[1] : fallback || 'REFUSED', text || 'The room refused the call.', text);
  }

  const bad = (message) => new CommonsError('BAD_ARGUMENT', message);
  const bytes = (text) => (typeof TextEncoder === 'function' ? new TextEncoder().encode(text).length : unescape(encodeURIComponent(text)).length);
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const empty = (v) => v === undefined || v === null || v === '';
  let seq = 0;
  const nonce = () => (seq += 1).toString(36) + Math.random().toString(36).slice(2, 10);

  function checkSize(value) {
    const size = bytes(JSON.stringify(value === undefined ? null : value));
    if (size > LIMITS.writeBytes) throw new CommonsError('TOO_LARGE', 'A write is ' + size + ' bytes; the limit is ' + LIMITS.writeBytes + '.');
  }

  function decode(raw) {
    if (typeof raw !== 'string') return raw;
    try { return JSON.parse(raw); } catch (_e) { return raw; }
  }

  /* The one place a list row is "removed": a marker cell, because a page cannot delete rows. */
  const removal = (opts) => {
    const m = Object.assign({ prop: 'Removed', value: 'yes' }, opts);
    return { cells: () => ({ [m.prop]: m.value }), hides: (cells) => cells[m.prop] === m.value };
  };

  /* The one place a map key is "deleted": its value cell is emptied, and the row stays. */
  const cleared = { json: '', is: (v) => v === undefined || v === null || v === '' };

  const toPlayer = (m) => {
    const p = { id: m.id, name: m.name, kind: m.kind === 'agent' ? 'agent' : 'person' };
    if (m.avatar) p.avatar = m.avatar;
    return p;
  };

  /* One database. Its whole state is `base` (the newest snapshot) and `entries` (this page's own
     writes, in the order they were issued). Every view of it is computed from those two by build(). */
  class Table {
    constructor(room, id) {
      this.room = room; this.id = id;
      this.props = [];
      this.base = { rows: [], version: null };
      this.entries = [];
      this.rows = []; this.sure = [];
      this.version = null; this.gen = 0; this.local = 0;
      this.gone = false; this.plain = false;
      this.listeners = new Set();
      this.tail = Promise.resolve();
    }

    async open() {
      const db = this.room.art.db;
      // Subscribe, then read: no edit is missed in between.
      this.stop = db.onChange(this.id, (snap) => this.ingest(snap));
      try { this.ingest(await this.read()); } catch (e) { this.close(); throw e; }
    }

    close() {
      if (typeof this.stop === 'function') this.stop();
      this.stop = null;
      this.listeners.clear();
    }

    async read() {
      try { return await this.room.art.db.read(this.id); } catch (e) { throw refusal(e && e.message); }
    }

    ingest(snap) {
      const version = snap && typeof snap.version === 'number' ? snap.version : null;
      // The page's copy only moves forward: a snapshot older than the last one is dropped.
      if (version !== null && this.base.version !== null && version < this.base.version) return;
      this.gen += 1;
      this.gone = !snap;
      if (snap) this.props = snap.props || [];
      this.base = { version, rows: snap ? (snap.rows || []).map((r) => ({ id: r.id, cells: Object.assign({}, r.cells) })) : [] };
      this.refresh('remote');
    }

    /* The one rule for dropping an entry. Pending: never. Acknowledged at a version: once the snapshot
       is that new. A plain write: at the first snapshot after its answer, or with the row's creator. */
    prune() {
      const v = this.base.version, made = new Set();
      this.entries = this.entries.filter((e) => {
        let keep = e.status === 'pending';
        if (e.status === 'acked') keep = v !== null && (e.version !== null ? v < e.version : v <= e.at);
        if (e.status === 'plain') keep = e.gen === this.gen || (e.kind === 'cell' && made.has(e.rowId));
        if (keep && e.kind === 'create' && e.rowId && !this.base.rows.some((r) => r.id === e.rowId)) made.add(e.rowId);
        return keep;
      });
    }

    /* The one function that computes a view: the snapshot, then the entries in order. `sure` leaves
       out everything the bridge has not answered for; the page's display leaves out only quiet ones. */
    build(sure) {
      const rows = this.base.rows.map((r) => ({ id: r.id, cells: Object.assign({}, r.cells) }));
      for (const e of this.entries) {
        if (e.kind === 'none' || (e.status === 'pending' && (sure || e.quiet))) continue;
        const row = e.kind === 'upsert' ? e.find(rows) : rows.find((r) => r.id === (e.rowId || e.local));
        if (row && e.kind !== 'create') row.cells[e.prop] = e.value;
        else if (!row && e.kind !== 'cell') rows.push({ id: e.rowId || e.local, cells: Object.assign({}, e.cells) });
      }
      return rows;
    }

    /* Recomputes both views and the version the next conditional write builds on, then tells the handles. */
    refresh(origin) {
      this.prune();
      this.rows = this.build(false);
      this.sure = this.build(true);
      const v = this.base.version, known = (m, e) => (e.status === 'acked' ? Math.max(m, e.version === null ? e.at : e.version) : m);
      this.version = v === null ? null : this.entries.reduce(known, v);
      for (const fn of Array.from(this.listeners)) fn(origin);
    }

    /* A write joins the list when it is issued, so the list keeps the order of the page's calls. */
    add(entry) {
      const e = Object.assign({ status: 'pending', local: '~' + (this.local += 1) }, entry);
      this.entries.push(e);
      this.refresh('local');
      return e;
    }

    /* A write that was refused, declined or left unknown: gone at once, which is the rollback. */
    drop(e) {
      const i = e.status === 'pending' ? this.entries.indexOf(e) : -1;
      if (i < 0) return;
      this.entries.splice(i, 1);
      this.refresh('local');
    }

    /* The bridge said yes. `mark` is the version the write was sent at (conditional), or the snapshot
       count at its answer, -1 when a snapshot came while it was in flight and is trusted over it (plain). */
    settle(e, r, status, mark) {
      e.status = status;
      if (e.kind === 'create') e.rowId = r.rowId;
      if (status === 'plain') e.gen = mark;
      else Object.assign(e, { at: mark, version: typeof r.version === 'number' ? r.version : null });
      this.refresh('local');
      return status === 'plain' ? 'local' : 'room';
    }

    need(name) {
      if (!this.props.some((p) => p.name === name)) throw new CommonsError('SCHEMA', 'The database has no column named "' + name + '".');
    }

    title() {
      const p = this.props.find((x) => x.type === 'title');
      if (!p) throw new CommonsError('SCHEMA', 'The database has no title column.');
      return p.name;
    }

    row(id) { return this.rows.find((r) => r.id === id); }

    /* A cell as the bridge has answered for it: what a write may be decided on. */
    cell(id, prop) {
      const row = this.sure.find((r) => r.id === id);
      return row && row.cells[prop];
    }

    /* Jobs run one at a time, in call order. */
    enqueue(job) {
      const run = this.tail.then(job);
      this.tail = run.catch(() => {});
      return run;
    }

    check() { if (this.gone) throw new CommonsError('DB_GONE', 'The database was removed.'); }

    /* A bridge call for run(): the options are passed only when the write is conditional. */
    call(name, ...args) { return (o) => this.room.art.db[name](this.id, ...args, ...(o ? [o] : [])); }

    /* One conditional attempt at the version this page holds: the bridge's answer when it landed,
       else what is known. The host never queues it, so a lost answer leaves the outcome unknown. */
    async cas(call) {
      const at = this.version;
      try { return await this.room.send(() => call({ ifVersion: at }), CONDITIONAL); } catch (e) {
        if (e.code === 'VERSION_CHANGED') return { changed: true, version: e.version };
        if (e.code === 'NO_ANSWER') return { unknown: true, reason: e.reason };
        if (e.code !== 'CONDITIONAL_UNAVAILABLE') throw e;
        this.plain = true;
        return { plain: true };
      }
    }

    /* Carries one entry out, when its turn in the queue comes. `step()` decides from the sure view and
       gives the bridge call, or null to decline. Resolves "room", "local", null, "contended" or "unknown". */
    async run(e, step, exact) {
      let unsure = false;
      try {
        for (let tries = 0; tries < 5; tries += 1) {
          this.check();
          const call = step();
          if (!call) return null;
          this.refresh('local');
          if (!exact || this.version === null || this.plain) {
            // Counted from the attempt the bridge accepts: an earlier one may have waited on the viewer.
            let gen;
            const r = await this.room.send(() => { gen = this.gen; return call(); });
            return this.settle(e, r, 'plain', gen === this.gen ? gen : -1);
          }
          const at = this.version, r = await this.cas(call);
          if (r.plain) continue;
          if (r.ok) return this.settle(e, r, 'acked', at);
          unsure = unsure || !!r.unknown;
          // Read inside the entry's own turn, so no later write of this page is in flight meanwhile.
          try { this.ingest(await this.read()); } catch (err) {
            if (!unsure) throw err;
            if (e.proof) return 'unknown';
          }
          // Only an entry whose value is its own proof (a claim token) may look for it and go on.
          if (r.unknown && !e.proof) throw new CommonsError('OUTCOME_UNKNOWN', 'No answer from the room: the change may or may not have been saved.', r.reason);
          if (r.unknown && e.proof()) return 'room';
          if (typeof r.version === 'number' && !(this.version >= r.version)) await this.room.sleep(this.room.opts.retryEvery);
        }
        return unsure ? 'unknown' : 'contended';
      } finally { this.drop(e); }
    }
  }

  /* Shared by SharedMap and SharedList: keep a projection of the table's view, diff it, tell observers. */
  class Handle {
    constructor(table) {
      this.t = table;
      this.observers = new Set();
      this.off = null;
    }

    start() {
      this.last = this.project();
      const fn = (origin) => this.refresh(origin);
      this.t.listeners.add(fn);
      this.off = () => this.t.listeners.delete(fn);
    }

    refresh(origin) {
      const next = this.project();
      const changes = this.diff(this.last, next);
      this.last = next;
      if (changes.length) for (const cb of Array.from(this.observers)) cb(changes, origin);
      this.after(origin);
    }

    after() {}

    /** Calls `cb(changes, origin)` after each change; origin is "local" (this page, or its rollback)
     *  or "remote" (a room snapshot). Returns a function that stops it. */
    observe(cb) {
      this.observers.add(cb);
      return () => this.observers.delete(cb);
    }

    /** The bridge id of the database behind this handle. */
    get databaseId() { return this.t.id; }

    /** True once the database behind this handle was removed from the room. */
    get gone() { return this.t.gone; }
  }

  /** A key → value map stored as rows: the title column is the key, a text column holds JSON.
   *  Last write to reach the room wins, per key. Not a CRDT. */
  class SharedMap extends Handle {
    constructor(table, opts) {
      super(table);
      this.keyProp = table.title();
      this.valueProp = (opts && opts.valueProp) || 'Value';
      table.need(this.valueProp);
      this.start();
    }

    /* Two rows with one key (two pages created it at once): the lowest row id is the key's row. */
    rowIn(rows, key) {
      let hit;
      for (const r of rows) if (r.cells[this.keyProp] === key && (!hit || r.id < hit.id)) hit = r;
      return hit;
    }

    project() {
      const rows = new Map();
      for (const r of this.t.rows) {
        const k = r.cells[this.keyProp];
        if (typeof k !== 'string' || k === '') continue;
        const cur = rows.get(k);
        if (!cur || r.id < cur.id) rows.set(k, r);
      }
      const out = new Map();
      for (const [k, r] of rows) if (!cleared.is(r.cells[this.valueProp])) out.set(k, decode(r.cells[this.valueProp]));
      return out;
    }

    diff(a, b) {
      const changes = [];
      for (const [key, value] of b) {
        if (!a.has(key)) changes.push({ key, action: 'add', value });
        else if (!same(a.get(key), value)) changes.push({ key, action: 'update', value, oldValue: a.get(key) });
      }
      for (const [key, oldValue] of a) if (!b.has(key)) changes.push({ key, action: 'delete', oldValue });
      return changes;
    }

    /** @param {string} key @returns {*} the value, or undefined */
    get(key) { return this.last.get(key); }

    /** @param {string} key @returns {boolean} */
    has(key) { return this.last.has(key); }

    /** @returns {Array<[string, *]>} every key and value */
    entries() { return Array.from(this.last.entries()); }

    /** @returns {number} how many keys hold a value */
    get size() { return this.last.size; }

    /** Sets a key to any JSON value. Shown at once; rolled back if the room refuses.
     *  @returns {Promise<void>} rejects with a CommonsError when refused */
    async set(key, value) {
      const json = JSON.stringify(value);
      if (json === undefined) throw bad('A map value must be JSON.');
      return this.put(key, json);
    }

    /** Deletes a key by emptying its value cell. The row stays: a page cannot delete rows. */
    async delete(key) { return this.put(key, cleared.json); }

    /** Read-modify-write of one key: `fn(current)` gives the new value and may run more than once. Not shown
     *  before the room answers. @returns {Promise<{value: *, mode: 'exact'|'best-effort'}>} see the README */
    async update(key, fn) {
      this.checkKey(key);
      const t = this.t, prop = this.valueProp, e = t.add({ kind: 'none', quiet: true });
      let value;
      const step = () => {
        const row = this.rowIn(t.sure, key), cell = row && row.cells[prop];
        value = fn(cleared.is(cell) ? undefined : decode(cell));
        const json = JSON.stringify(value);
        if (json === undefined) throw bad('A map value must be JSON.');
        const cells = { [this.keyProp]: key, [prop]: json };
        checkSize(cells);
        Object.assign(e, row ? { kind: 'cell', rowId: row.id, prop, value: json } : { kind: 'create', rowId: null, cells });
        return row ? t.call('write', row.id, prop, json) : t.call('createRow', cells);
      };
      // No proof is given to run(): a value cannot show that this write landed, a peer can write the same one.
      const mode = await t.enqueue(() => t.run(e, step, true)).catch((err) => {
        if (err.code === 'OUTCOME_UNKNOWN') this.unknown(err, key);
        throw err;
      });
      if (mode !== 'room' && mode !== 'local') throw new CommonsError('CONTENDED', 'The database kept changing. Try again.');
      return { value, mode: mode === 'room' ? 'exact' : 'best-effort' };
    }

    /* Adds what this page can see now to an unknown outcome. Its own copy may be behind the room. */
    unknown(e, key) {
      const row = this.rowIn(this.t.sure, key), cell = row && row.cells[this.valueProp];
      e.value = cleared.is(cell) ? undefined : decode(cell);
      if (this.t.version !== null) e.version = this.t.version;
    }

    checkKey(key) { if (typeof key !== 'string' || key === '') throw bad('A map key is a non-empty string.'); }

    async put(key, json) {
      this.checkKey(key);
      const t = this.t, prop = this.valueProp, cells = { [this.keyProp]: key, [prop]: json };
      checkSize(cells);
      // Shown at once by key; which row it writes, or whether it creates one, is decided at its turn.
      const e = t.add({ kind: 'upsert', find: (rows) => this.rowIn(rows, key), prop, value: json, cells });
      const step = () => {
        const row = this.rowIn(t.sure, key);
        if (!row && cleared.is(json)) return null;
        Object.assign(e, row ? { kind: 'cell', rowId: row.id } : { kind: 'create' });
        return row ? t.call('write', row.id, prop, json) : t.call('createRow', cells);
      };
      await t.enqueue(() => t.run(e, step, false));
    }
  }

  /** The rows of one database as a list of `{id, cells}`.
   *  Last write to reach the room wins, per cell (one row, one column). Not a CRDT. */
  class SharedList extends Handle {
    constructor(table, opts) {
      super(table);
      const o = opts || {};
      this.orderBy = o.orderBy || null;
      this.removed = removal(o.removed);
      this.claimProp = o.claimProp || 'Claim';
      this.held = new Map();
      this.busy = new Map();
      this.start();
    }

    /** The name of the database's title column. */
    get titleProp() { return this.t.title(); }

    project() {
      const rows = [];
      for (const r of this.t.rows) if (!this.removed.hides(r.cells)) rows.push(Object.freeze({ id: r.id, cells: Object.freeze(Object.assign({}, r.cells)) }));
      const by = this.orderBy;
      if (by) {
        rows.sort((x, y) => {
          const a = x.cells[by], b = y.cells[by];
          if (empty(a) !== empty(b)) return empty(a) ? 1 : -1;
          return a < b ? -1 : a > b ? 1 : x.id < y.id ? -1 : x.id > y.id ? 1 : 0;
        });
      }
      return rows;
    }

    diff(a, b) {
      const before = new Map(a.map((r) => [r.id, r])), now = new Map(b.map((r) => [r.id, r]));
      const changes = [];
      for (const row of b) {
        const oldRow = before.get(row.id);
        if (!oldRow) changes.push({ id: row.id, action: 'add', row });
        else if (!same(oldRow.cells, row.cells)) changes.push({ id: row.id, action: 'update', row, oldRow });
      }
      for (const oldRow of a) if (!now.has(oldRow.id)) changes.push({ id: oldRow.id, action: 'delete', oldRow });
      return changes;
    }

    after(origin) {
      if (origin !== 'remote') return;
      for (const id of Array.from(this.held.keys())) this.settle(id);
    }

    /* A claim this page held that now shows another token was taken over. Not judged while this
       page's own write to that cell is queued or in flight. */
    settle(id) {
      const mine = this.held.get(id);
      if (!mine || this.busy.get(id)) return;
      const raw = this.parse(this.t.cell(id, this.claimProp));
      if (raw && raw.n === mine.token) return;
      this.held.delete(id);
      if (mine.until > this.t.room.now() && mine.onLost) mine.onLost(this.holder(raw));
    }

    /* Runs a claim-cell entry in the queue with loss tracking paused. A refusal leaves the earlier claim tracked. */
    async guarded(id, e, step) {
      const count = (by) => this.busy.set(id, (this.busy.get(id) || 0) + by);
      count(1);
      try { return await this.t.enqueue(() => this.t.run(e, step, true)); } catch (err) {
        count(-1);
        this.settle(id);
        count(1);
        throw err;
      } finally { count(-1); }
    }

    /** @returns {Array<{id: string, cells: object}>} the rows, in order */
    toArray() { return this.last.slice(); }

    /** @param {string} id @returns {{id: string, cells: object}|undefined} */
    get(id) { return this.last.find((r) => r.id === id); }

    /** @returns {number} how many rows */
    get length() { return this.last.length; }

    /** Adds a row from `{column: value}` in one call: all fit, or nothing is created. The row
     *  appears once the room confirms it. @returns {Promise<string>} the new row's id */
    async push(cells) {
      if (!cells || typeof cells !== 'object') throw bad('push takes an object of column values.');
      checkSize(cells);
      const t = this.t, e = t.add({ kind: 'create', cells, quiet: true });
      await t.enqueue(() => t.run(e, () => t.call('createRow', cells), false));
      return e.rowId;
    }

    /** Changes cells of one row. Shown at once; rolled back if the room refuses. One write per
     *  column, so not atomic: on a refusal the error's `written` lists the columns already saved. */
    async update(id, cells) {
      const t = this.t;
      if (!t.row(id)) throw new CommonsError('NOT_FOUND', 'No row with id "' + id + '".');
      const pairs = Object.entries(cells || {});
      for (const [prop, value] of pairs) { t.need(prop); checkSize(value); }
      const entries = pairs.map(([prop, value]) => t.add({ kind: 'cell', rowId: id, prop, value })), written = [];
      await t.enqueue(async () => {
        try {
          for (const e of entries) { await t.run(e, () => t.call('write', id, e.prop, e.value), false); written.push(e.prop); }
        } catch (err) {
          for (const e of entries) t.drop(e);
          err.written = written;
          throw err;
        }
      });
    }

    /** Hides a row from the list by writing the removal marker. @returns {Promise<void>} */
    async remove(id) { return this.update(id, this.removed.cells()); }

    parse(cell) {
      const raw = decode(cell);
      return raw && typeof raw === 'object' && typeof raw.by === 'string' && Number.isFinite(raw.until) ? raw : null;
    }

    /* A live claim as callers see it; null for none or one that ran out. */
    holder(raw) {
      if (!raw || raw.until <= this.t.room.now()) return null;
      const me = this.t.room.art.me;
      return { id: raw.by, name: String(raw.name || ''), kind: raw.kind === 'agent' ? 'agent' : 'person', until: raw.until, mine: !!me && raw.by === me.id };
    }

    /** Who holds a row now: `{id, name, kind, until, mine}`, or null when free or expired.
     *  The holder is what the claimer wrote about itself: the room does not attest it. */
    claimOf(id) {
      const row = this.get(id);
      return this.holder(row && this.parse(row.cells[this.claimProp]));
    }

    /** Claims a row for `opts.ttl` ms (default 60000). Resolves `{ok, holder, certainty, reason?}`: "room" when
     *  the room took a conditional write, else "local". A later loss calls `opts.onLost(holder)`. See the README. */
    async claim(id, opts) {
      const o = opts || {}, ttl = o.ttl === undefined ? 60000 : o.ttl, t = this.t, prop = this.claimProp;
      if (!(ttl > 0) || !Number.isFinite(ttl)) throw bad('ttl is a positive number of milliseconds.');
      t.need(prop);
      if (!this.get(id)) throw new CommonsError('NOT_FOUND', 'No row with id "' + id + '".');
      const room = t.room, me = room.me(), tokens = new Set();
      // Any token this call wrote is proof: an attempt whose answer was lost may be the one that stayed.
      const mine = () => { const raw = this.parse(t.cell(id, prop)); return raw && raw.by === me.id && tokens.has(raw.n) ? raw : null; };
      // Stamped anew for every attempt: a write can wait on the viewer for longer than the ttl.
      const stamp = () => {
        const n = nonce();
        tokens.add(n);
        return JSON.stringify({ by: me.id, name: me.name, kind: me.kind, until: room.now() + ttl, n });
      };
      const e = t.add({ kind: 'cell', rowId: id, prop, value: stamp(), proof: () => !!mine() });
      const step = () => {
        const raw = this.parse(t.cell(id, prop));
        if (raw && raw.by !== me.id && raw.until > room.now()) return null;
        return (opt) => t.call('write', id, prop, (e.value = stamp()))(opt);
      };
      const mode = await this.guarded(id, e, step), won = mode !== null && mine();
      if (won && won.until > room.now()) {
        this.held.set(id, { token: won.n, until: won.until, onLost: o.onLost });
        return { ok: true, holder: this.holder(won), certainty: mode === 'local' ? 'local' : 'room' };
      }
      this.settle(id);
      const holder = this.holder(this.parse(t.cell(id, prop)));
      const reason = won ? 'expired' : holder && !holder.mine ? 'held' : mode === 'unknown' ? 'unknown' : 'contended';
      return { ok: false, reason, holder, certainty: 'local' };
    }

    /** Gives up the viewer's claim on a row by emptying the claim cell, and only while the cell still holds
     *  the exact claim found when its turn came. @returns {Promise<boolean>} false when it was not, or is no longer, there */
    async release(id) {
      const t = this.t, prop = this.claimProp, me = t.room.me();
      let pinned;
      const e = t.add({ kind: 'cell', rowId: id, prop, value: '', proof: () => cleared.is(t.cell(id, prop)) });
      // Authorised and pinned from the same view, at its turn: a claim of this page still pending counts for nothing.
      const step = () => {
        const cell = t.cell(id, prop), raw = this.parse(cell);
        if (pinned === undefined) pinned = raw && raw.by === me.id && raw.until > t.room.now() ? cell : null;
        return pinned !== null && cell === pinned ? t.call('write', id, prop, '') : null;
      };
      const mode = await this.guarded(id, e, step);
      if (pinned && cleared.is(t.cell(id, prop))) { this.held.delete(id); return true; }
      if (mode === 'unknown') throw new CommonsError('OUTCOME_UNKNOWN', 'No answer from the room: the claim may or may not have been released.');
      this.settle(id);
      return false;
    }
  }

  /* EXPERIMENTAL: the named-agent request calls are designed and not built, and may change.
     Lists pass through untouched: `state` and `outcome` are the bridge's own, as plain text. */
  function requestsAdapter(art) {
    const a = art.agent;
    // That design's own reason codes. Nothing built documents them, so they stay out of REASONS.
    const codes = [[/^consent_required$/i, 'CONSENT_REQUIRED'], [/^rate(_page|_agent)?$/i, 'RATE_LIMITED']];
    if (!a || typeof a.requestable !== 'function' || typeof a.request !== 'function' || typeof a.requests !== 'function') return null;
    return {
      /** @returns {Promise<Array<{id: string, name: string, actions: string[]}>>} */
      agents: async () => (await a.requestable()) || [],
      /** @returns {Promise<{requestId: string, duplicate: boolean}>} */
      send: async (request) => {
        const r = await a.request(request);
        if (!r || !r.ok) throw refusal(r && r.reason, 'REQUEST_REFUSED', codes);
        return { requestId: r.requestId, duplicate: !!r.duplicate };
      },
      /** Calls `cb(wholeList)` now and on every change, with the bridge's list as given. */
      watch: async (scope, cb) => {
        const first = await a.requests(scope, { onChange: cb });
        cb(first);
        return first;
      },
    };
  }

  /** A connected room. Get one from `Commons.connect()`. */
  class Room {
    constructor(art, opts) {
      this.art = art;
      this.opts = Object.assign({ retryEvery: 1500, retryFor: 120000 }, opts);
      this.sleep = this.opts.sleep || ((ms) => new Promise((ok) => setTimeout(ok, ms)));
      this.tables = new Map();
      this.waiting = new Set();
      /** The app record `{id, name, database}`, or null when the page is opened on its own. */
      this.app = art.app || null;
      /** EXPERIMENTAL adapters. `requests` is null unless the bridge has the named-agent calls. */
      this.experimental = { requests: requestsAdapter(art) };
      /** What this bridge offers. Check before showing a control. */
      this.capabilities = Object.freeze({
        players: typeof art.members === 'function',
        authorship: typeof art.personId === 'function',
        agents: !!art.agent && typeof art.agent.list === 'function' && typeof art.agent.sendTask === 'function',
        post: !!art.room && typeof art.room.post === 'function',
        requests: !!this.experimental.requests,
      });
    }

    /** @returns {number} room time in ms: the bridge's estimate, or this device's clock */
    now() {
      if (this.opts.now) return this.opts.now();
      const t = typeof this.art.now === 'function' ? this.art.now() : NaN;
      return Number.isFinite(t) ? t : Date.now();
    }

    /** @returns {{id: string, name: string, kind: 'person'|'agent'}} the viewer, as the host says */
    me() { return toPlayer(this.art.me); }

    /** The room's joined members, people and agents alike (at most 200). Not "who is viewing".
     *  @returns {Promise<Array<{id: string, name: string, kind: 'person'|'agent', avatar?: string}>>} */
    async players() {
      if (!this.capabilities.players) throw new CommonsError('UNSUPPORTED', 'This bridge does not list members.');
      try { return (await this.art.members()).map(toPlayer); } catch (e) { throw refusal(e && e.message); }
    }

    /** The player a "created by" or person cell names: the attested author of a row.
     *  @returns {Promise<string|null>} a player id */
    async playerOf(cell) {
      if (!this.capabilities.authorship) throw new CommonsError('UNSUPPORTED', 'This bridge does not resolve authors.');
      try { return await this.art.personId(cell); } catch (e) { throw refusal(e && e.message); }
    }

    /** Calls `cb(code)` each time a write waits and will retry: "CONSENT_PENDING" or "RATE_LIMITED".
     *  Returns a function that stops it. */
    onWaiting(cb) {
      this.waiting.add(cb);
      return () => this.waiting.delete(cb);
    }

    /* The guide's write loop: retry while the viewer has not answered or the page is rate-limited. */
    async send(call, extra) {
      const until = this.now() + this.opts.retryFor;
      for (;;) {
        const r = await call();
        if (r && r.ok) return r;
        const err = refusal(r && r.reason, undefined, extra);
        if (r && typeof r.version === 'number') err.version = r.version;
        if ((err.code !== 'CONSENT_PENDING' && err.code !== 'RATE_LIMITED') || this.now() >= until) throw err;
        for (const cb of Array.from(this.waiting)) cb(err.code);
        await this.sleep(this.opts.retryEvery);
      }
    }

    async table(name) {
      const list = await this.art.db.list();
      const id = name ? (list.find((d) => d.name === name) || {}).id : this.app && this.app.database;
      if (!id) throw new CommonsError('DB_MISSING', name ? 'This room has no database named "' + name + '".' : 'This page is not opened as an app: name the database.');
      if (!this.tables.has(id)) {
        const t = new Table(this, id);
        this.tables.set(id, t.open().then(() => t));
        this.tables.get(id).catch(() => this.tables.delete(id));
      }
      return this.tables.get(id);
    }

    /** A shared map on a database, found by exact name (omit it to use the app's own database).
     *  `opts.valueProp` names the value column, default "Value". @returns {Promise<SharedMap>} */
    async map(name, opts) { return new SharedMap(await this.table(name), opts); }

    /** A shared list of a database's rows, found by exact name (omit it to use the app's own).
     *  `opts`: `{orderBy?, removed?: {prop, value}, claimProp?}`. @returns {Promise<SharedList>} */
    async list(name, opts) { return new SharedList(await this.table(name), opts); }

    /** The viewer's own agents; empty until the viewer allows the page.
     *  @returns {Promise<Array<{id: string, name: string, kind: 'agent', inRoom: boolean}>>} */
    async agents() {
      if (!this.capabilities.agents) return [];
      return ((await this.art.agent.list()) || []).map((a) => ({ id: a.id, name: a.name, kind: 'agent', inRoom: !!a.userId }));
    }

    /** Asks one of the viewer's own agents, after the viewer confirms `text`. `opts`: `{key, actionId?, row?}`;
     *  the same key never asks twice. @returns {Promise<{eventId: string}>} a dispatch receipt, nothing more */
    async ask(agentId, text, opts) {
      const o = opts || {};
      if (!this.capabilities.agents) throw new CommonsError('UNSUPPORTED', 'This bridge cannot ask an agent.');
      if (!KEY_RE.test(o.key || '')) throw bad('ask needs a key of letters, digits, ".", "_" or "-" (1 to 64).');
      if (o.actionId !== undefined && !ACTION_RE.test(o.actionId)) throw bad('actionId is letters, digits, ".", "_" or "-" (1 to 64).');
      const request = { agentId, text, key: o.key };
      if (o.actionId !== undefined) request.actionId = o.actionId;
      if (o.row) request.row = o.row;
      const r = await this.art.agent.sendTask(request);
      if (!r || !r.ok) throw refusal(r && r.reason);
      return { eventId: r.eventId };
    }

    /** Posts in the room as the viewer, after they confirm the exact text; `mention(id)` calls a player.
     *  The page gets no answer of its own. @returns {Promise<{eventId: string}>} */
    async post(text, opts) {
      if (!this.capabilities.post) throw new CommonsError('UNSUPPORTED', 'This bridge cannot post in the room.');
      const message = { text };
      if (opts && opts.key !== undefined) message.key = opts.key;
      const r = await this.art.room.post(message);
      if (!r || !r.posted) throw refusal(r && r.reason);
      return { eventId: r.eventId };
    }

    /** @param {string} playerId an id from `players()` @returns {string} mention text for `post` */
    mention(playerId) { return '<@' + playerId + '>'; }

    /** Stops every watch this room handle started. */
    close() {
      for (const p of this.tables.values()) p.then((t) => t.close(), () => {});
      this.tables.clear();
    }
  }

  /** Connects to the room this page is open in; rejects with code NO_BRIDGE outside a room.
   *  `opts` are test hooks and retry timing: `{artifact?, now?, sleep?, retryEvery?, retryFor?}`. */
  async function connect(opts) {
    const o = opts || {};
    const host = typeof window !== 'undefined' ? window : globalThis;
    const art = o.artifact || host.artifact;
    if (!art || !art.db) throw new CommonsError('NO_BRIDGE', 'Open this page in a room to use it.');
    await art.ready;
    return new Room(art, o);
  }

  return { connect, CommonsError, LIMITS, version: '0.1.0' };
});
