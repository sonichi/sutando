#!/usr/bin/env node
// Read-only observer of the Codex core: publishes its runtime observation record from the rollout
// file the core's process holds open. Args: --engine --tmux-socket --session [--workspace].
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFile } from 'node:child_process';
import { pathToFileURL } from 'node:url';

export const OBSERVER = 'codex-observer';
export const VERSION = '0.1.0';
const TICK_MS = 1000;
const DISCOVER_EVERY = 10;
const FLUSH_DELAY_MS = 250;
const HEARTBEAT_MS = 15000;
const RUN_TIMEOUT_MS = 5000;
export const TAIL_BYTES = 256 * 1024;
const TOOL_CALLS = new Set(['function_call', 'custom_tool_call', 'local_shell_call', 'web_search_call', 'mcp_tool_call']);
const TOOL_OUTPUTS = new Set(['function_call_output', 'custom_tool_call_output', 'local_shell_call_output', 'mcp_tool_call_output']);

export function newState() {
  return {phase: 'unknown', motion: 'unknown', condition: 'unknown', reason: null, seq: 0, changedAt: 0,
    conditionSince: null, lastSuccessAt: null, turnSucceeded: false};
}

export function errorReason(error) {
  const info = error?.codex_error_info;
  if (info === 'unauthorized') return 'needs-login';
  if (info === 'usage_limit_exceeded' || info === 'session_budget_exceeded') return 'quota-limit';
  return 'api-error';
}

// Evidence a model request completed: an item the model produced. token_count is not: Codex
// writes one from the session's running totals before returning a usage-limit failure.
function modelOutput(p) {
  return TOOL_CALLS.has(p.type) || p.type === 'reasoning' || (p.type === 'message' && p.role === 'assistant');
}

function epoch(ts, fallback) {
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? ms / 1000 : fallback;
}

// One rollout line -> change, or null. A failure is the error on the turn's completion; it
// stands until a model output proves a later request completed.
export function lineChange(state, line) {
  const p = line?.payload || {};
  if (line?.type === 'event_msg') {
    if (p.type === 'task_started') {
      return {phase: 'requesting', motion: 'moving', turnSucceeded: false, ...(state.condition === 'abnormal' ? {} : {condition: 'unknown'})};
    }
    if (p.type === 'task_complete' && p.error) {
      return {phase: 'failed', motion: 'idle', condition: 'abnormal', reason: errorReason(p.error)};
    }
    if (p.type === 'task_complete') return {phase: 'idle', motion: 'idle'};
    if (p.type === 'turn_aborted') return {phase: 'idle', motion: 'idle'};
    return null;
  }
  if (line?.type !== 'response_item') return null;
  if (modelOutput(p)) {
    const phase = state.motion === 'moving' && TOOL_CALLS.has(p.type) ? {phase: 'tool'} : {};
    return {success: true, condition: 'healthy', reason: null, turnSucceeded: true, ...phase};
  }
  if (state.motion === 'moving' && TOOL_OUTPUTS.has(p.type)) return {phase: 'requesting'};
  return null;
}

// Applies a change at `at` (epoch s); true when the record changed.
export function apply(state, change, at) {
  if (!change) return false;
  let changed = false;
  if (change.success) { state.lastSuccessAt = at; changed = true; }
  if ('turnSucceeded' in change) state.turnSucceeded = change.turnSucceeded;
  const was = {condition: state.condition, reason: state.reason};
  for (const key of ['phase', 'motion', 'condition', 'reason']) {
    if (change[key] !== undefined && change[key] !== state[key]) { state[key] = change[key]; state.changedAt = at; changed = true; }
  }
  if (state.condition !== 'abnormal') {
    state.reason = null;
    state.conditionSince = null;
  } else if (was.condition !== 'abnormal' || was.reason !== state.reason) {
    state.conditionSince = at;
  }
  if (changed) state.seq += 1;
  return changed;
}

export function buildRecord(state, ident, now) {
  return {
    schema: 1, observer: OBSERVER, observer_version: VERSION, observer_id: ident.observerId,
    observer_started_at: ident.startedAt, seat: 'core', session: ident.session, claude_session_id: null,
    seq: state.seq, changed_at: state.changedAt || ident.startedAt, condition_since: state.conditionSince,
    last_success_at: state.lastSuccessAt, heartbeat_at: now, phase: state.phase, motion: state.motion,
    condition: state.condition, reason: state.condition === 'abnormal' ? (state.reason || 'api-error') : null,
  };
}

// The core's own rollout among those its processes hold open: the one top-level thread. Subagents'
// rollouts name a parent; an unreadable one could be top-level, so it is as ambiguous as a second.
export function pickRollout(metas) {
  if (metas.some((m) => !m)) return null;
  const top = metas.filter((m) => !m.parent_thread_id && !m.agent_path);
  return top.length === 1 ? top[0].path : null;
}

export function descendants(rootPid, psText) {
  const children = new Map();
  for (const row of psText.split('\n')) {
    const [pid, ppid] = row.trim().split(/\s+/).map(Number);
    if (pid && ppid) children.set(ppid, [...(children.get(ppid) || []), pid]);
  }
  const out = [rootPid];
  for (let i = 0; i < out.length; i++) out.push(...(children.get(out[i]) || []));
  return out;
}

export function rolloutPaths(lsofText) {
  return [...new Set(lsofText.split('\n').filter((l) => l.startsWith('n') && /\/rollout-[^/]*\.jsonl$/.test(l))
    .map((l) => l.slice(1)))];
}

export class Observer {
  constructor(opts, deps) {
    this.opts = opts;
    this.deps = deps;
    this.state = newState();
    this.target = null;
    this.offset = 0;
    this.rest = '';
    this.ticks = 0;
    this.gone = 0;
    this.misses = 0;
    this.ident = {observerId: crypto.randomBytes(8).toString('hex'), startedAt: deps.now(), session: opts.session};
    this.dirty = false;
    this.timer = null;
    this.inFlight = false;
    this.python = '';
    this.metas = new Map();
  }

  // false once the core session is gone, so the caller can stop. One failed probe proves
  // nothing: a session or a rollout counts as gone on the second miss in a row.
  async discover() {
    const panePid = await this.deps.panePid();
    if (!panePid) {
      if (++this.gone < 2) return true;
      this.select(null);
      return false;
    }
    this.gone = 0;
    const paths = await this.deps.openRollouts(panePid);
    // Only the open files' classification is kept; an unreadable session_meta is retried.
    const kept = new Map();
    for (const p of paths) {
      let kind = this.metas.get(p);
      if (!kind) {
        const meta = await this.deps.readMeta(p);
        if (meta) kind = {parent_thread_id: meta.parent_thread_id || null, agent_path: meta.agent_path || null};
      }
      if (kind) kept.set(p, kind);
    }
    this.metas = kept;
    const file = pickRollout(paths.map((p) => (kept.has(p) ? {...kept.get(p), path: p} : null)));
    if (file === null && this.target && ++this.misses < 2) return true;
    this.misses = 0;
    this.select(file);
    return true;
  }

  select(file) {
    if (file === this.target) return;
    this.target = file;
    // The writer refuses a lower seq from the same observer_id, so seq never goes back.
    this.state = {...newState(), seq: this.state.seq};
    this.rest = '';
    if (!file) return;
    const {text, size} = this.deps.readTail(file, TAIL_BYTES);
    this.offset = size;
    // A tail read can start mid-line; that fragment fails to parse and is skipped.
    this.feed(text);
    this.markDirty();
  }

  follow() {
    if (!this.target) return;
    const chunk = this.deps.readFrom(this.target, this.offset);
    if (!chunk) return;
    this.offset += chunk.bytes;
    this.feed(chunk.text);
  }

  feed(text) {
    const lines = (this.rest + text).split('\n');
    this.rest = lines.pop();
    let changed = false;
    for (const raw of lines) {
      let line;
      try { line = JSON.parse(raw); } catch { continue; }
      changed = apply(this.state, lineChange(this.state, line), epoch(line.timestamp, this.deps.now())) || changed;
    }
    if (changed) this.markDirty();
  }

  async tick() {
    if (this.ticks++ % DISCOVER_EVERY === 0 && !(await this.discover())) return false;
    this.follow();
    return true;
  }

  markDirty() {
    this.dirty = true;
    if (this.timer || !this.target) return;
    this.timer = this.deps.after(FLUSH_DELAY_MS, () => { this.timer = null; this.flush(); });
  }

  heartbeat() {
    if (this.target) this.markDirty();
  }

  async flush() {
    if (this.inFlight || !this.target || !this.dirty) return;
    this.inFlight = true;
    this.dirty = false;
    try {
      this.python ||= await this.deps.resolvePython();
      if (!this.python) return;
      await this.deps.write(this.python, buildRecord(this.state, this.ident, this.deps.now()));
    } catch {
      // The record is best effort; its lease lapses into "no opinion".
    } finally {
      this.inFlight = false;
      if (this.dirty) this.markDirty();
    }
  }
}

export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, '');
    if (!key || argv[i + 1] === undefined) throw new Error(`bad argument ${argv[i]}`);
    out[key] = argv[i + 1];
  }
  for (const key of ['engine', 'tmux-socket', 'session']) if (!out[key]) throw new Error(`missing --${key}`);
  return {engine: out.engine, tmuxSocket: out['tmux-socket'], session: out.session, workspace: out.workspace || ''};
}

// partialOk: exit 1 with output is a result (lsof exits 1 when one listed pid has gone).
// A timeout, signal or overflow is never one: its output may be cut short.
function run(file, args, input, partialOk = false) {
  return new Promise((resolve) => {
    const child = execFile(file, args, {timeout: RUN_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024}, (err, stdout) => {
      const partial = partialOk && err && err.code === 1 && !err.killed && !err.signal && stdout;
      resolve(err && !partial ? null : stdout);
    });
    if (input !== undefined) child.stdin.end(input);
  });
}

export async function lsofRollouts(pids) {
  return rolloutPaths((await run('lsof', ['-a', '-p', pids.join(','), '-Fn'], undefined, true)) || '');
}

// One fd, one size snapshot: the returned end is exactly where the bytes read stop, so a write
// landing during the read is picked up by the next follow, never skipped or read twice.
function readRange(io, file, startOf) {
  const fd = io.openSync(file, 'r');
  try {
    const size = io.fstatSync(fd).size;
    const start = Math.min(startOf(size), size);
    const buf = Buffer.alloc(size - start);
    const n = buf.length ? io.readSync(fd, buf, 0, buf.length, start) : 0;
    return {buf: buf.subarray(0, n), end: start + n};
  } finally {
    io.closeSync(fd);
  }
}

// session_meta carries the base instructions, so the first line can run to hundreds of KB.
function firstLine(file, limit = 4 * 1024 * 1024) {
  const fd = fs.openSync(file, 'r');
  try {
    const parts = [];
    const chunk = Buffer.alloc(256 * 1024);
    for (let pos = 0; pos < limit;) {
      const n = fs.readSync(fd, chunk, 0, chunk.length, pos);
      if (!n) break;
      const nl = chunk.subarray(0, n).indexOf(10);
      parts.push(Buffer.from(chunk.subarray(0, nl < 0 ? n : nl)));
      if (nl >= 0) break;
      pos += n;
    }
    return Buffer.concat(parts).toString('utf8');
  } finally {
    fs.closeSync(fd);
  }
}

export function fileDeps(opts, io = fs) {
  return {
    now: () => Date.now() / 1000,
    after: (ms, fn) => setTimeout(fn, ms),
    panePid: async () => Number(((await run('tmux', ['-S', opts.tmuxSocket, 'list-panes', '-t', `=${opts.session}`,
      '-F', '#{pane_pid}'])) || '').split('\n')[0]) || 0,
    openRollouts: async (panePid) => {
      return lsofRollouts(descendants(panePid, (await run('ps', ['-A', '-o', 'pid=,ppid='])) || ''));
    },
    readMeta: async (file) => {
      try {
        const first = JSON.parse(firstLine(file));
        return first.type === 'session_meta' ? first.payload : null;
      } catch { return null; }
    },
    readTail: (file, bytes) => {
      const {buf, end} = readRange(io, file, (size) => Math.max(0, size - bytes));
      return {text: buf.toString('utf8'), size: end};
    },
    readFrom: (file, offset) => {
      const {buf} = readRange(io, file, () => offset);
      return buf.length ? {text: buf.toString('utf8'), bytes: buf.length} : null;
    },
    resolvePython: async () => ((await run('bash', ['-c', '. "$1/scripts/python-binary.sh" && resolve_python "$1"', 'resolve', opts.engine])) || '').trim(),
    write: (python, record) => {
      const args = [path.join(opts.engine, 'src', 'runtime_observation.py'), 'write'];
      if (opts.workspace) args.push('--workspace', opts.workspace);
      return run(python, args, JSON.stringify(record));
    },
  };
}

// Runs ticks until the core is gone; a tick that throws is one bad read, not the end of the core.
export async function runLoop(observer, sleep) {
  for (;;) {
    let alive = true;
    try { alive = await observer.tick(); } catch { /* keep going */ }
    if (!alive) return;
    await sleep(TICK_MS);
  }
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const observer = new Observer(opts, fileDeps(opts));
  setInterval(() => observer.heartbeat(), HEARTBEAT_MS);
  await runLoop(observer, (ms) => new Promise((r) => setTimeout(r, ms)));
  process.exit(0);
}

// import.meta.url is the resolved path; argv[1] keeps a symlinked spelling such as /tmp.
export function isEntrypoint(argv1, moduleUrl) {
  try {
    return Boolean(argv1) && moduleUrl === pathToFileURL(fs.realpathSync(argv1)).href;
  } catch {
    return false;
  }
}

if (isEntrypoint(process.argv[1], import.meta.url)) main();
