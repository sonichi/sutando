import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  Observer, TAIL_BYTES, apply, buildRecord, descendants, fileDeps, isEntrypoint, lineChange, lsofRollouts, newState, parseArgs,
  pickRollout, rolloutPaths, runLoop,
} from '../src/agent/codex/cli/codex-observer.mjs';

const ev = (type, extra = {}, ts = '2026-10-06T02:57:50.000Z') => ({timestamp: ts, type: 'event_msg', payload: {type, ...extra}});
const item = (type, ts = '2026-10-06T02:57:51.000Z', extra = {}) => ({timestamp: ts, type: 'response_item', payload: {type, ...extra}});
const said = (ts) => item('message', ts, {role: 'assistant'});
const quota = {message: "You've hit your usage limit.", codex_error_info: 'usage_limit_exceeded'};
const REPO = fileURLToPath(new URL('..', import.meta.url));
// The repository's interpreter policy, never a bare python3 (the macOS developer-tools stub).
const PY = execFileSync('bash', ['-c', '. "$1/scripts/python-binary.sh" && resolve_python "$1"', 'resolve', REPO]).toString().trim();
const writeRecord = (ws, rec) => execFileSync(PY, [REPO + 'src/runtime_observation.py', 'write', '--workspace', ws], {input: JSON.stringify(rec)});
const run = (lines, state = newState()) => {
  for (const line of lines) apply(state, lineChange(state, line), Date.parse(line.timestamp) / 1000);
  return state;
};

test('a turn reads moving, a model output healthy, its end idle', () => {
  const s = run([ev('task_started')]);
  assert.deepEqual([s.phase, s.motion, s.condition], ['requesting', 'moving', 'unknown']);
  run([item('function_call', '2026-10-06T02:57:53.000Z')], s);
  assert.deepEqual([s.phase, s.condition], ['tool', 'healthy']);
  assert.equal(s.lastSuccessAt, Date.parse('2026-10-06T02:57:53.000Z') / 1000);
  run([item('function_call_output'), said(), ev('task_complete')], s);
  assert.deepEqual([s.phase, s.motion, s.condition], ['idle', 'idle', 'healthy']);
});

test('a token_count is not a success: a turn with no model output stays unknown', () => {
  const s = run([ev('task_started'), ev('token_count', {info: {}}), item('message', undefined, {role: 'user'}), ev('task_complete')]);
  assert.deepEqual([s.phase, s.motion, s.condition, s.lastSuccessAt], ['idle', 'idle', 'unknown', null]);
  assert.equal(buildRecord(s, {observerId: 'o', startedAt: 1, session: 's'}, 2).reason, null);
});

test('a usage-limit failure after a success is abnormal quota-limit and keeps the earlier success time', () => {
  const s = run([ev('task_started'), said('2026-10-06T02:57:51.000Z'), ev('task_complete'),
    ev('task_started', {}, '2026-10-06T03:00:00.000Z'), ev('token_count', {info: {total_token_usage: {}}}, '2026-10-06T03:00:01.000Z'),
    ev('task_complete', {error: quota}, '2026-10-06T03:00:01.000Z')]);
  const r = buildRecord(s, {observerId: 'o', startedAt: 1, session: 's'}, 2);
  assert.deepEqual([r.phase, r.motion, r.condition, r.reason], ['failed', 'idle', 'abnormal', 'quota-limit']);
  assert.equal(r.last_success_at, Date.parse('2026-10-06T02:57:51.000Z') / 1000);
  assert.equal(r.condition_since, Date.parse('2026-10-06T03:00:01.000Z') / 1000);
  run([ev('task_started', {}, '2026-10-06T03:05:00.000Z')], s);
  assert.equal(s.condition, 'abnormal', 'a new turn alone proves nothing');
  run([said('2026-10-06T03:05:02.000Z')], s);
  assert.deepEqual([s.condition, s.reason, s.conditionSince], ['healthy', null, null]);
});

test('completion errors map to the record reasons', () => {
  const reasonOf = (info) => buildRecord(run([ev('task_started'), ev('task_complete', {error: {codex_error_info: info}})]),
    {observerId: 'o', startedAt: 1, session: 's'}, 2).reason;
  assert.equal(reasonOf('unauthorized'), 'needs-login');
  assert.equal(reasonOf('session_budget_exceeded'), 'quota-limit');
  assert.equal(reasonOf('server_overloaded'), 'api-error');
  assert.equal(reasonOf({http_connection_failed: {http_status_code: 502}}), 'api-error');
});

test('an aborted turn goes idle; items outside a turn never move the phase', () => {
  const s = run([ev('task_started'), ev('turn_aborted')]);
  assert.deepEqual([s.phase, s.motion], ['idle', 'idle']);
  run([item('function_call'), item('function_call_output'), ev('agent_message'), {type: 'turn_context'}], s);
  assert.deepEqual([s.phase, s.motion], ['idle', 'idle']);
});

test('the record is schema 1 for the core seat', () => {
  const s = run([ev('task_started')]);
  const r = buildRecord(s, {observerId: 'abc', startedAt: 10, session: 'sutando-core'}, 20);
  assert.deepEqual([r.schema, r.seat, r.session, r.observer, r.claude_session_id, r.heartbeat_at, r.condition_since],
    [1, 'core', 'sutando-core', 'codex-observer', null, 20, null]);
  assert.equal(r.seq, s.seq);
});

test('only the one top-level rollout is the core', () => {
  assert.equal(pickRollout([{path: 'a'}, {path: 'b', parent_thread_id: 'x'}, {path: 'c', agent_path: '/sub'}]), 'a');
  assert.equal(pickRollout([{path: 'a'}, {path: 'b'}]), null);
  assert.equal(pickRollout([{path: 'b', parent_thread_id: 'x'}]), null);
  assert.equal(pickRollout([]), null);
});

test('process tree and lsof parsing', () => {
  assert.deepEqual(descendants(10, ' 10 1\n 11 10\n 12 11\n 13 1\n 14 10\n'), [10, 11, 14, 12]);
  assert.deepEqual(rolloutPaths('p11\nn/x/sessions/2026/10/06/rollout-a.jsonl\nn/x/log.txt\nn/x/sessions/2026/10/06/rollout-a.jsonl\n'),
    ['/x/sessions/2026/10/06/rollout-a.jsonl']);
});

test('arguments are required', () => {
  assert.throws(() => parseArgs(['--engine', '/e']), /missing --tmux-socket/);
  assert.deepEqual(parseArgs(['--engine', '/e', '--tmux-socket', '/s', '--session', 'c']),
    {engine: '/e', tmuxSocket: '/s', session: 'c', workspace: ''});
});

function fake(files, opts = {}) {
  const writes = [];
  const timers = [];
  const deps = {
    now: () => 1_791_000_000,
    after: (ms, fn) => timers.push(fn),
    panePid: async () => (opts.gone ? 0 : 42),
    openRollouts: async () => Object.keys(files),
    readMeta: async (p) => files[p].meta,
    readTail: (p, bytes) => { const t = files[p].text; return {text: t.slice(-bytes), size: Buffer.byteLength(t)}; },
    readFrom: (p, off) => { const t = files[p].text.slice(off); return t ? {text: t, bytes: Buffer.byteLength(t)} : null; },
    resolvePython: async () => (opts.python === undefined ? '/py' : opts.python),
    write: async (py, rec) => { writes.push({py, rec}); },
  };
  return {deps, writes, flush: async () => { for (const fn of timers.splice(0)) fn(); await new Promise((r) => setTimeout(r, 5)); }};
}
const jl = (...lines) => lines.map((l) => JSON.stringify(l)).join('\n') + '\n';

test('the observer follows the core rollout and writes through the resolved python', async () => {
  const files = {'/r/main': {meta: {id: 'm'}, text: jl(ev('task_started'))}, '/r/sub': {meta: {parent_thread_id: 'm'}, text: ''}};
  const f = fake(files);
  const o = new Observer({session: 'sutando-core'}, f.deps);
  assert.equal(await o.tick(), true);
  await f.flush();
  assert.equal(f.writes.at(-1).py, '/py');
  assert.deepEqual([f.writes.at(-1).rec.phase, f.writes.at(-1).rec.motion], ['requesting', 'moving']);
  const done = JSON.stringify(ev('task_complete'));
  files['/r/main'].text += jl(said()) + done.slice(0, 5);
  await o.tick();
  await f.flush();
  assert.equal(f.writes.at(-1).rec.condition, 'healthy');
  assert.equal(f.writes.at(-1).rec.phase, 'requesting', 'the half-written last line is held back');
});

test('a split line is applied once it completes', async () => {
  const line = JSON.stringify(ev('task_complete'));
  const files = {'/r/main': {meta: {}, text: jl(ev('task_started')) + line.slice(0, 10)}};
  const f = fake(files);
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  assert.equal(o.state.phase, 'requesting');
  files['/r/main'].text += line.slice(10) + '\n';
  await o.tick();
  assert.equal(o.state.phase, 'idle');
});

test('a large rollout is read from its tail, skipping the cut first line', async () => {
  const filler = jl(...Array.from({length: 4000}, () => ev('agent_message', {message: 'x'.repeat(80)})));
  const files = {'/r/main': {meta: {}, text: filler + jl(ev('task_started'))}};
  assert(Buffer.byteLength(files['/r/main'].text) > TAIL_BYTES);
  const f = fake(files);
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  assert.equal(o.state.phase, 'requesting');
});

test('no core rollout, two top-level rollouts, or no python mean no write', async () => {
  for (const files of [{}, {'/a': {meta: {}, text: jl(ev('task_started'))}, '/b': {meta: {}, text: ''}}]) {
    const f = fake(files);
    const o = new Observer({session: 's'}, f.deps);
    await o.tick();
    o.heartbeat();
    await f.flush();
    assert.equal(f.writes.length, 0);
  }
  const f = fake({'/a': {meta: {}, text: jl(ev('task_started'))}}, {python: ''});
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  await f.flush();
  assert.equal(f.writes.length, 0);
});

test('a core session gone on two discoveries in a row stops the observer; one miss does not', async () => {
  const f = fake({'/a': {meta: {}, text: ''}}, {gone: true});
  const o = new Observer({session: 's'}, f.deps);
  assert.equal(await o.discover(), true);
  assert.equal(await o.discover(), false);
});

test('one discovery that finds no rollout keeps the target; a second drops it', async () => {
  const files = {'/a': {meta: {}, text: jl(ev('task_started'))}};
  const f = fake(files);
  const o = new Observer({session: 's'}, f.deps);
  await o.discover();
  f.deps.openRollouts = async () => [];
  await o.discover();
  assert.equal(o.target, '/a');
  await o.discover();
  assert.equal(o.target, null);
});

test('reselecting a rollout never lowers seq, so the real writer keeps accepting records', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-ws-'));
  const stored = () => JSON.parse(fs.readFileSync(path.join(ws, 'state/runtime-observations/core.json'), 'utf8'));
  const busy = jl(...Array.from({length: 15}, (_, i) => ev(i % 2 ? 'task_complete' : 'task_started')));
  const files = {'/a': {meta: {}, text: busy}, '/b': {meta: {}, text: jl(ev('task_started'))}};
  const f = fake(files);
  f.deps.now = () => Date.now() / 1000;
  f.deps.write = async (py, rec) => writeRecord(ws, rec);
  f.deps.openRollouts = async () => ['/a'];
  const o = new Observer({session: 'sutando-core'}, f.deps);
  await o.discover();
  await f.flush();
  const before = stored();
  f.deps.openRollouts = async () => ['/b'];
  await o.discover();
  await o.discover();
  await f.flush();
  const after = stored();
  assert(after.seq > before.seq, `seq ${after.seq} after ${before.seq}`);
  assert.deepEqual([after.phase, after.motion], ['requesting', 'moving']);
  fs.rmSync(ws, {recursive: true});
});

test('lsof output still counts when one listed pid has already exited', {skip: !fs.existsSync('/usr/sbin/lsof') && !fs.existsSync('/usr/bin/lsof')}, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-lsof-'));
  const file = fs.realpathSync(dir) + '/rollout-held.jsonl';
  fs.writeFileSync(file, '');
  const holder = spawn(process.execPath, ['-e', `require('fs').openSync(${JSON.stringify(file)}, 'r'); setTimeout(() => {}, 20000)`]);
  const gone = spawn(process.execPath, ['-e', '0']);
  await new Promise((r) => gone.on('exit', r));
  await new Promise((r) => setTimeout(r, 300));
  try {
    assert.deepEqual(await lsofRollouts([holder.pid, gone.pid]), [file]);
  } finally {
    holder.kill();
    fs.rmSync(dir, {recursive: true});
  }
});

test('a failing write never throws out of the observer', async () => {
  const f = fake({'/a': {meta: {}, text: jl(ev('task_started'))}});
  f.deps.write = async () => { throw new Error('no python'); };
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  await f.flush();
  o.heartbeat();
  await f.flush();
});

test('fileDeps reads a long first line and file ranges', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-'));
  const file = path.join(dir, 'rollout-x.jsonl');
  const meta = {type: 'session_meta', payload: {id: 't', base_instructions: 'y'.repeat(600_000)}};
  fs.writeFileSync(file, JSON.stringify(meta) + '\n' + JSON.stringify(ev('task_started')) + '\n');
  const deps = fileDeps({engine: '/e', tmuxSocket: '/s', session: 's'});
  assert.equal((await deps.readMeta(file)).id, 't');
  const {size} = deps.readTail(file, 100);
  assert.equal(size, fs.statSync(file).size);
  assert.equal(deps.readFrom(file, size), null);
  fs.appendFileSync(file, 'abc');
  assert.deepEqual(deps.readFrom(file, size), {text: 'abc', bytes: 3});
  fs.rmSync(dir, {recursive: true});
});

test('/health reads a usage-limit failure after a success as abnormal quota-limit', () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-health-'));
  const now = Date.now() / 1000;
  const at = (dt) => new Date((now + dt) * 1000).toISOString();
  const s = run([ev('task_started', {}, at(-120)), said(at(-118)), ev('task_complete', {}, at(-117)),
    ev('task_started', {}, at(-30)), ev('token_count', {info: {}}, at(-29)), ev('task_complete', {error: quota}, at(-29))]);
  writeRecord(ws, buildRecord(s, {observerId: 'o'.repeat(16), startedAt: now - 300, session: 'sutando-core'}, now - 1));
  // The pane's own quota claim began after the real success: a false later success would supersede it.
  fs.mkdirSync(path.join(ws, 'state/cli-wedge'), {recursive: true});
  fs.writeFileSync(path.join(ws, 'state/cli-wedge/window.jsonl'), [-100, -70, -40].map((dt) => JSON.stringify(
    {ts: now + dt, state: 's', raw_state: 'r', patterns: [], abnormal: ['quota-limit']})).join('\n') + '\n');
  const snap = JSON.parse(execFileSync(PY, [REPO + 'src/health_snapshot.py', '--workspace', ws, '--agent', 'core', '--view', 'full']).toString());
  const core = snap.agents[0];
  assert.deepEqual([core.condition, core.reason], ['abnormal', 'quota-limit']);
  assert.equal(core.sources.cli_wedge.opinion?.reason, 'quota-limit');
  assert.equal(core.sources.cli_wedge.value.superseded_by, undefined, 'the pane quota claim stands');
  fs.rmSync(ws, {recursive: true});
});

test('discovery keeps only open rollouts, only their classification, and retries unreadable metadata', async () => {
  const big = 'x'.repeat(600_000);
  const metas = {'/main': {id: 'm', base_instructions: big}, '/sub': null};
  const f = fake({'/main': {meta: null, text: jl(ev('task_started'))}, '/sub': {meta: null, text: ''}});
  let reads = 0;
  f.deps.readMeta = async (p) => { reads += 1; return metas[p]; };
  f.deps.openRollouts = async () => ['/main', '/sub'];
  const o = new Observer({session: 's'}, f.deps);
  await o.discover();
  assert.equal(o.target, null, 'an unreadable sibling could be a second top-level rollout');
  assert.deepEqual([...o.metas.keys()], ['/main']);
  assert.deepEqual(o.metas.get('/main'), {parent_thread_id: null, agent_path: null, subagent: false});
  metas['/sub'] = {id: 's', parent_thread_id: 'm'};
  await o.discover();
  assert.equal(o.target, '/main', 'repaired as a subagent');
  f.deps.openRollouts = async () => ['/main'];
  await o.discover();
  assert.deepEqual([...o.metas.keys()], ['/main'], 'closed rollouts are pruned');
  assert.equal(reads, 3, 'a cached classification is not re-read; a failed read is');
});

test('an open rollout that stays unreadable drops the target on the second discovery', async () => {
  const metas = {'/old': {id: 'o'}, '/new': null};
  const f = fake({'/old': {meta: null, text: jl(ev('task_started'), ev('task_complete'))}, '/new': {meta: null, text: ''}});
  f.deps.readMeta = async (p) => metas[p];
  f.deps.openRollouts = async () => ['/old'];
  const o = new Observer({session: 's'}, f.deps);
  await o.discover();
  assert.equal(o.target, '/old');
  f.deps.openRollouts = async () => ['/old', '/new'];
  await o.discover();
  assert.equal(o.target, '/old', 'one ambiguous discovery keeps the target');
  await o.discover();
  assert.equal(o.target, null, 'a second one drops it');
  const before = f.writes.length;
  o.heartbeat();
  await f.flush();
  assert.equal(f.writes.length, before, 'no target, no renewed record');
});

test('an unreadable rollout repaired as a second top-level thread leaves no target', async () => {
  const metas = {'/a': {id: 'a'}, '/b': null};
  const f = fake({'/a': {meta: null, text: ''}, '/b': {meta: null, text: ''}});
  f.deps.readMeta = async (p) => metas[p];
  f.deps.openRollouts = async () => ['/a', '/b'];
  const o = new Observer({session: 's'}, f.deps);
  await o.discover();
  metas['/b'] = {id: 'b'};
  await o.discover();
  assert.equal(o.target, null);
});

test('a write landing during the initial tail read is applied exactly once', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-race-'));
  const file = path.join(dir, 'rollout-race.jsonl');
  const done = JSON.stringify(ev('task_complete', {error: quota})) + '\n';
  fs.writeFileSync(file, jl(ev('task_started')) + done.slice(0, 20));
  // Codex extends the half-written line just after the reader first learns the file's size.
  let appended = false;
  const afterSize = (stat) => { if (!appended) { appended = true; fs.appendFileSync(file, done.slice(20, 40)); } return stat; };
  const io = {...fs, statSync: (...a) => afterSize(fs.statSync(...a)), fstatSync: (...a) => afterSize(fs.fstatSync(...a))};
  const deps = fileDeps({engine: REPO, tmuxSocket: '/s', session: 's'}, io);
  const o = new Observer({session: 's'}, {...fake({}).deps, readTail: deps.readTail, readFrom: deps.readFrom});
  o.select(file);
  fs.appendFileSync(file, done.slice(40));
  o.follow();
  assert.deepEqual([o.state.phase, o.state.condition, o.state.reason], ['failed', 'abnormal', 'quota-limit']);
  assert.equal(o.offset, fs.statSync(file).size);
  fs.rmSync(dir, {recursive: true});
});

test('started through a symlinked path, the observer runs and probes the core session', async () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-link-')));
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin);
  const log = path.join(dir, 'tmux.log');
  fs.writeFileSync(path.join(bin, 'tmux'), `#!/bin/sh\necho "$*" >> ${JSON.stringify(log)}\n`, {mode: 0o755});
  const link = path.join(dir, 'engine-link');
  fs.symlinkSync(path.join(REPO, 'src/agent/codex/cli'), link);
  const child = spawn(process.execPath, [path.join(link, 'codex-observer.mjs'), '--engine', REPO, '--tmux-socket', '/x.sock',
    '--session', 'sutando-core'], {env: {...process.env, PATH: `${bin}:${process.env.PATH}`}, stdio: 'ignore'});
  try {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline && !(fs.existsSync(log) && fs.readFileSync(log, 'utf8').includes('list-panes'))) {
      await new Promise((r) => setTimeout(r, 100));
    }
    assert.match(fs.readFileSync(log, 'utf8'), /-S \/x\.sock list-panes -t =sutando-core/);
  } finally {
    child.kill();
    fs.rmSync(dir, {recursive: true});
  }
});

test('the entrypoint check resolves argv[1] and refuses a missing path', () => {
  const real = fileURLToPath(new URL('../src/agent/codex/cli/codex-observer.mjs', import.meta.url));
  const url = new URL('../src/agent/codex/cli/codex-observer.mjs', import.meta.url).href;
  assert.equal(isEntrypoint(real, url), true);
  assert.equal(isEntrypoint('/no/such/file.mjs', url), false);
  assert.equal(isEntrypoint(undefined, url), false);
});

test('the main loop survives a tick that throws and stops when the core is gone', async () => {
  const outcomes = [() => { throw new Error('bad read'); }, () => true, () => false];
  let ticks = 0;
  let sleeps = 0;
  await runLoop({tick: async () => outcomes[ticks++]()}, async () => { sleeps += 1; });
  assert.deepEqual([ticks, sleeps], [3, 2]);
});

test('fileDeps.write runs the engine writer with the workspace and the record on stdin', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-write-'));
  const py = path.join(dir, 'py');
  const out = path.join(dir, 'argv');
  fs.writeFileSync(py, `#!/bin/sh\nprintf '%s\\n' "$@" > ${JSON.stringify(out)}\ncat >> ${JSON.stringify(out)}\n`, {mode: 0o755});
  const deps = fileDeps({engine: '/eng', tmuxSocket: '/s', session: 's', workspace: '/ws dir'});
  await deps.write(py, {seat: 'core'});
  assert.deepEqual(fs.readFileSync(out, 'utf8').split('\n'),
    ['/eng/src/runtime_observation.py', 'write', '--workspace', '/ws dir', '{"seat":"core"}']);
  fs.rmSync(dir, {recursive: true});
});

const receiptStart = (id = 'turn-a') => [ev('task_started', {turn_id: id}),
  {type: 'turn_context', payload: {turn_id: id}}];
const userInput = (text = 'synthetic task prompt') => item('message', '2026-10-06T02:57:51.000Z',
  {role: 'user', content: [{type: 'input_text', text}]});
const receiptEnd = (id = 'turn-a') => [said('2026-10-06T02:57:52.000Z'),
  ev('task_complete', {turn_id: id}, '2026-10-06T02:57:53.000Z')];
const receiptIdent = {observerId: 'o', startedAt: 1, session: 's', coreIncarnation: '42:Tue Oct  6 02:00:00 2026'};
const receiptRecord = (state) => buildRecord(state, receiptIdent, 1791255480);

test('completion receipt hashes exact accepted input within the matching observed turn', () => {
  const prompt = 'synthetic task prompt\nwith exact whitespace ';
  const state = run([...receiptStart(), userInput(prompt), ...receiptEnd()]);
  assert.deepEqual(receiptRecord(state).completed_task, {
    prompt_sha256: crypto.createHash('sha256').update(prompt).digest('hex'),
    accepted_at: Date.parse('2026-10-06T02:57:51.000Z') / 1000,
    completed_at: Date.parse('2026-10-06T02:57:53.000Z') / 1000,
    core_incarnation: receiptIdent.coreIncarnation,
  });
  assert(!JSON.stringify(state).includes(prompt));
  assert(!JSON.stringify(receiptRecord(state)).includes(prompt));
  assert.equal(buildRecord(state, {observerId: 'o', startedAt: 1, session: 's'}, 1791255480).completed_task, undefined);
});

test('missing, reordered, mismatched and multi-input turn evidence earns no receipt', () => {
  const cases = [
    [userInput(), ...receiptEnd()],
    [ev('task_started', {turn_id: 'turn-a'}), userInput(), ...receiptEnd()],
    [userInput(), ...receiptStart(), ...receiptEnd()],
    [...receiptStart(), userInput(), ...receiptEnd('turn-b')],
    [...receiptStart(), userInput(), userInput('manual steering'), ...receiptEnd()],
    [...receiptStart(), userInput(), ...receiptStart('turn-b'), ...receiptEnd('turn-b')],
    [...receiptStart(), said(), userInput(), ...receiptEnd()],
    [...receiptStart(), userInput(), {type: 'turn_context', payload: {turn_id: 'other'}}, ...receiptEnd()],
    [...receiptStart(), {...userInput(), timestamp: 'bad-date'}, ...receiptEnd()],
    [...receiptStart(), userInput(), ev('task_complete', {turn_id: 'turn-a'})],
    [...receiptStart(), userInput(), said(), ev('task_complete', {turn_id: 'turn-a'}, 'bad-date')],
  ];
  for (const lines of cases) assert.equal(receiptRecord(run(lines)).completed_task, undefined);
});

test('failed or aborted turns clear task correlation and never acknowledge the prior prompt', () => {
  for (const ending of [ev('turn_aborted'), ev('task_complete', {turn_id: 'turn-a', error: quota})]) {
    const state = run([...receiptStart(), userInput(), said(), ending]);
    assert.equal(receiptRecord(state).completed_task, undefined);
    run(receiptEnd(), state);
    assert.equal(receiptRecord(state).completed_task, undefined);
  }
});

test('new/manual input clears the old receipt and advances its publication sequence', () => {
  for (const input of [ev('task_started', {turn_id: 'manual'}), userInput('manual message'), ev('turn_aborted')]) {
    const state = run([...receiptStart(), userInput(), ...receiptEnd()]);
    const seq = state.seq;
    run([input], state);
    assert(state.seq > seq);
    assert.equal(receiptRecord(state).completed_task, undefined);
  }
});

test('observer restart reconstructs only a complete correlated turn from the top-level tail', async () => {
  const text = jl(...receiptStart(), userInput(), ...receiptEnd());
  for (let restart = 0; restart < 2; restart++) {
    const files = {'/main': {meta: {}, text}, '/sub': {meta: {parent_thread_id: 'parent'}, text}};
    const f = fake(files);
    f.deps.coreIncarnation = async () => receiptIdent.coreIncarnation;
    const observer = new Observer({session: 's'}, f.deps);
    await observer.tick();
    await f.flush();
    assert.equal(f.writes.at(-1).rec.completed_task.core_incarnation, receiptIdent.coreIncarnation);
  }
  const f = fake({'/main': {meta: {}, text: jl(userInput(), ...receiptEnd())}});
  f.deps.coreIncarnation = async () => receiptIdent.coreIncarnation;
  const observer = new Observer({session: 's'}, f.deps);
  await observer.tick(); await f.flush();
  assert.equal(f.writes.at(-1).rec.completed_task, undefined);
});

test('ambiguous or subagent-only rollout never produces a completion receipt', async () => {
  const text = jl(...receiptStart(), userInput(), ...receiptEnd());
  for (const files of [{'/sub': {meta: {parent_thread_id: 'parent'}, text}},
    {'/sub': {meta: {source: {subagent: {thread_spawn: {parent_thread_id: 'parent'}}}}, text}},
    {'/one': {meta: {}, text}, '/two': {meta: {}, text}}]) {
    const f = fake(files); f.deps.coreIncarnation = async () => receiptIdent.coreIncarnation;
    const observer = new Observer({session: 's'}, f.deps);
    await observer.tick(); await f.flush();
    assert.equal(f.writes.length, 0);
  }
});

test('receipt survives the production schema writer without the input body', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'observer-receipt-'));
  try {
    const record = receiptRecord(run([...receiptStart(), userInput('private synthetic prompt'), ...receiptEnd()]));
    writeRecord(ws, record);
    const stored = fs.readFileSync(path.join(ws, 'state/runtime-observations/core.json'), 'utf8');
    assert.deepEqual(JSON.parse(stored).completed_task, record.completed_task);
    assert(!stored.includes('private synthetic prompt'));
  } finally { fs.rmSync(ws, {recursive: true, force: true}); }
});

test('core incarnation uses the notifier whitespace contract for single-digit days', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'observer-incarnation-'));
  const oldPath = process.env.PATH;
  try {
    fs.writeFileSync(path.join(dir, 'ps'), '#!/bin/sh\n[ "$LC_ALL" = C ] && [ "$TZ" = UTC ] || exit 1\nprintf " Thu Oct  8 02:00:00 2026 \\n"\n', {mode: 0o755});
    process.env.PATH = `${dir}:${oldPath}`;
    const deps = fileDeps({});
    assert.equal(await deps.coreIncarnation(42), '42:Thu Oct 8 02:00:00 2026');
    fs.writeFileSync(path.join(dir, 'ps'), '#!/bin/sh\nexit 1\n', {mode: 0o755});
    assert.equal(await deps.coreIncarnation(42), '');
  } finally {
    process.env.PATH = oldPath;
    fs.rmSync(dir, {recursive: true, force: true});
  }
});

test('a new core incarnation reselects even the same rollout without refreshing old acceptance time', async () => {
  const text = jl(...receiptStart(), userInput(), ...receiptEnd());
  const files = {'/main': {meta: {}, text}};
  const f = fake(files);
  let incarnation = '42:Tue Oct 6 02:00:00 2026';
  f.deps.coreIncarnation = async () => incarnation;
  const observer = new Observer({session: 's'}, f.deps);
  await observer.discover(); await f.flush();
  const before = f.writes.at(-1).rec.completed_task;
  incarnation = '42:Tue Oct 6 04:00:00 2026';
  await observer.discover(); await f.flush();
  const after = f.writes.at(-1).rec.completed_task;
  assert.equal(after.core_incarnation, incarnation);
  assert.equal(after.accepted_at, before.accepted_at);
  assert.equal(after.completed_at, before.completed_at);
  assert(after.accepted_at < Date.parse('2026-10-06T04:00:00Z') / 1000);
  incarnation = '';
  await observer.discover(); await f.flush();
  assert.equal(f.writes.at(-1).rec.completed_task, undefined);
});

test('record binds current core incarnation even before a completed parent task', () => {
  const record = buildRecord(newState(), {observerId: 'o', startedAt: 1, session: 's', coreIncarnation: '42:start'}, 2);
  assert.equal(record.core_incarnation, '42:start');
  assert.equal(record.completed_task, undefined);
});

test('only a complete matching failed root turn publishes a typed terminal receipt', () => {
  const end = (id = 'turn-a', info = 'server_overloaded') => ev('task_complete',
    {turn_id: id, error: {codex_error_info: info}}, '2026-10-06T02:57:53.000Z');
  const state = run([...receiptStart(), userInput(), end()]);
  const record = receiptRecord(state);
  assert.equal(record.condition, 'abnormal');
  assert.equal(record.completed_task, undefined);
  assert.equal(record.failed_task.error_kind, 'server-overloaded');
  assert.equal(record.failed_task.turn_id, 'turn-a');
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-failed-receipt-'));
  try {
    writeRecord(ws, record);
    const saved = JSON.parse(fs.readFileSync(path.join(ws, 'state/runtime-observations/core.json')));
    assert.deepEqual(saved.failed_task, record.failed_task);
    assert(!JSON.stringify(saved).includes('synthetic task prompt'));
  } finally { fs.rmSync(ws, {recursive: true}); }
  for (const lines of [
    [userInput(), end()],
    [ev('task_started', {turn_id: 'turn-a'}), userInput(), end()],
    [...receiptStart(), userInput(), end('other')],
    [...receiptStart(), userInput(), ev('turn_aborted'), end()],
    [...receiptStart(), userInput(), userInput('extra'), end()],
    [...receiptStart(), userInput(), {...end(), timestamp: 'bad-date'}],
  ]) assert.equal(receiptRecord(run(lines)).failed_task, undefined);
  run([ev('task_started', {turn_id: 'next'})], state);
  assert.equal(receiptRecord(state).failed_task, undefined);
});

const startupContext = () => item('message', '2026-10-06T02:57:50.100Z', {role: 'user', content: [
  {type: 'input_text', text: '# AGENTS.md instructions for synthetic-project\n<INSTRUCTIONS>\nFixture rules.\n</INSTRUCTIONS>'},
  {type: 'input_text', text: '<environment_context>\n<cwd>synthetic-project</cwd>\n</environment_context>'},
]});

test('real CLI first-turn metadata before context never replaces the authoritative prompt', () => {
  const first = [receiptStart()[0], startupContext(), receiptStart()[1], userInput()];
  const succeeded = receiptRecord(run([...first, ...receiptEnd()]));
  assert.equal(succeeded.completed_task.prompt_sha256, crypto.createHash('sha256').update('synthetic task prompt').digest('hex'));
  assert.equal(succeeded.last_completed_at, succeeded.completed_task.completed_at);
  const failed = receiptRecord(run([...first, ev('task_complete', {turn_id: 'turn-a',
    error: {codex_error_info: 'server_overloaded'}}, '2026-10-06T02:57:53.000Z')]));
  assert.equal(failed.failed_task.prompt_sha256, succeeded.completed_task.prompt_sha256);
  assert.equal(failed.failed_task.error_kind, 'server-overloaded');
  assert.equal(failed.completed_task, undefined);
  for (const input of [
    [receiptStart()[0], startupContext(), startupContext(), receiptStart()[1], userInput()],
    [...receiptStart(), startupContext(), userInput()],
    [receiptStart()[0], {...startupContext(), payload: {...startupContext().payload, content: startupContext().payload.content.slice(0, 1)}}, receiptStart()[1], userInput()],
    [receiptStart()[0], startupContext(), userInput()],
    [...first, userInput('actual duplicate')],
  ]) assert.equal(receiptRecord(run([...input, ...receiptEnd()])).completed_task, undefined);
});
