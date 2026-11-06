import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const file = new URL('../skills/claude-observer/plugin/hooks/register.js', import.meta.url);
const source = await readFile(file, 'utf8');
const load = () => import('data:text/javascript;base64,' + Buffer.from(source + `\n//${Math.random()}`).toString('base64'));
const WID = '40659240fd884f63bcd19fa684b451f1';

test('seat comes from the environment, guests have none', async () => {
  const { seatFromEnv } = await load();
  assert.deepEqual(seatFromEnv(WID, '', 'sutando-worker-' + WID), { seat: WID, session: 'sutando-worker-' + WID });
  assert.deepEqual(seatFromEnv(undefined, '1', 'sutando-core'), { seat: 'core', session: 'sutando-core' });
  assert.deepEqual(seatFromEnv(undefined, '1', undefined), { seat: 'core', session: 'sutando-core' });
  assert.equal(seatFromEnv(undefined, '', 'adhoc'), null);
  assert.equal(seatFromEnv('not-hex', '', 'adhoc'), null);
  assert.equal(seatFromEnv(WID, '1', ''), null);
});

test('condition_since is set on entering or changing an abnormal reason and cleared otherwise', async () => {
  const { nextConditionSince } = await load();
  const ok = { condition: 'ok', reason: '-' };
  const auth = { condition: 'bad', reason: 'auth' };
  assert.equal(nextConditionSince(ok, auth, null, 50), 50);
  assert.equal(nextConditionSince(auth, auth, 50, 90), 50);
  assert.equal(nextConditionSince(auth, { condition: 'bad', reason: 'quota' }, 50, 90), 90);
  assert.equal(nextConditionSince(auth, ok, 50, 90), null);
  assert.equal(nextConditionSince({ condition: 'unk', reason: '-' }, { condition: 'unk', reason: '-' }, null, 90), null);
});

test('engineRoot strips the skill plugin path', async () => {
  const { engineRoot } = await load();
  assert.equal(engineRoot('/a/b/skills/claude-observer/plugin'), '/a/b');
  assert.equal(engineRoot('/a/b/skills/claude-observer/plugin/'), '/a/b');
});

test('buildRecord maps band vocabulary to schema 1', async () => {
  const { buildRecord } = await load();
  const base = { observerId: 'id', startedAt: 1, seat: 'core', session: 's', claudeSessionId: undefined, seq: 2, changedAt: 3,
    conditionSince: null, lastSuccessAt: null, heartbeatAt: 4 };
  const cases = [
    [{ phase: 'req', motion: 'mov', condition: 'ok', reason: '-' }, ['requesting', 'moving', 'healthy', null]],
    [{ phase: 'tool', motion: 'idle', condition: 'unk', reason: '-' }, ['tool', 'idle', 'unknown', null]],
    [{ phase: 'fail', motion: 'idle', condition: 'bad', reason: 'auth' }, ['failed', 'idle', 'abnormal', 'needs-login']],
    [{ phase: 'wait', motion: 'idle', condition: 'bad', reason: 'perm' }, ['waiting', 'idle', 'abnormal', 'permission']],
    [{ phase: 'wait', motion: 'idle', condition: 'bad', reason: 'input' }, ['waiting', 'idle', 'abnormal', 'awaiting-input']],
    [{ phase: 'cmp', motion: 'mov', condition: 'ok', reason: '-' }, ['compacting', 'moving', 'healthy', null]],
    [{ phase: 'fail', motion: 'idle', condition: 'bad', reason: 'quota' }, ['failed', 'idle', 'abnormal', 'quota-limit']],
    [{ phase: 'fail', motion: 'idle', condition: 'bad', reason: 'funds' }, ['failed', 'idle', 'abnormal', 'out-of-credits']],
    [{ phase: 'fail', motion: 'idle', condition: 'bad', reason: 'retry' }, ['failed', 'idle', 'abnormal', 'api-error']],
    [{ phase: 'unk', motion: 'unk', condition: 'unk', reason: '-' }, ['unknown', 'unknown', 'unknown', null]],
  ];
  for (const [state, [p, m, c, r]] of cases) {
    const rec = buildRecord({ ...base, ...state });
    assert.deepEqual([rec.phase, rec.motion, rec.condition, rec.reason], [p, m, c, r]);
    assert.equal(rec.schema, 1);
    assert.equal(rec.claude_session_id, null);
    assert.equal(rec.observer, 'claude-observer');
  }
});

function harness(env, root = '/e/skills/claude-observer/plugin', python = '/py/bin/python3\n') {
  const handlers = {};
  const timers = [];
  const runs = [];
  const resolves = [];
  let nowMs = 1_790_000_000_000;
  let release = null;
  const $ = {
    clock: { now: async () => nowMs, after: (ms, fn) => { timers.push({ ms, fn }); return { cancel() {} }; } },
    env: { get: async (name) => env[name] },
    plugin: { root },
    session: { id: async () => 'claude-session-1' },
    ui: { invalidate() {} },
    process: { run: async (argv, init) => {
      if (argv[0] === 'bash') { resolves.push(argv); return { exitCode: python === null ? 1 : 0, stdout: python || '', stderr: '' }; }
      runs.push({ argv, init }); if (release === 'hold') await new Promise((r) => { release = r; }); return { exitCode: 0 }; } },
  };
  return { $, handlers, timers, runs, resolves, tick: (ms) => { nowMs += ms; }, hold: () => { release = 'hold'; }, free: () => release && release !== 'hold' && release(),
    fire: async (name, e = {}) => handlers[name]($, e, async (x) => x),
    flushTimers: async () => { const due = timers.splice(0); for (const t of due) await t.fn(); await new Promise((r) => setTimeout(r, 5)); } };
}

async function boot(env, root, python) {
  const { register } = await load();
  const h = harness(env, root, python);
  register((name, a, b) => { h.handlers[name + (typeof a === 'object' ? ':' + a.component : '')] = b || a; });
  await h.fire('session.start');
  return h;
}

test('a core seat writes debounced records through the engine CLI and heartbeats', async () => {
  const h = await boot({ SUTANDO_CORE_SESSION: '1', SUTANDO_TMUX_SESSION: 'sutando-core', SUTANDO_WORKSPACE_DIR: '/ws' });
  assert.equal(h.timers.filter((t) => t.ms === 250).length, 1);
  assert(h.timers.some((t) => t.ms === 15000));
  await h.fire('turn.start', { turnId: 't1' });
  await h.fire('classic.StopFailure', { error: 'authentication_failed' });
  assert.equal(h.timers.filter((t) => t.ms === 250).length, 1, 'one flush scheduled however many changes');
  await h.flushTimers();
  assert.equal(h.runs.length, 1);
  const { argv, init } = h.runs[0];
  assert.deepEqual(argv, ['/py/bin/python3', '/e/src/runtime_observation.py', 'write', '--workspace', '/ws']);
  assert.deepEqual(h.resolves, [['bash', '-c', '. "$1/scripts/python-binary.sh" && resolve_python "$1"', 'resolve', '/e']]);
  assert.equal(init.timeoutMs, 5000);
  const rec = JSON.parse(init.stdin);
  assert.deepEqual([rec.seat, rec.session, rec.phase, rec.condition, rec.reason, rec.claude_session_id],
    ['core', 'sutando-core', 'failed', 'abnormal', 'needs-login', 'claude-session-1']);
  assert.equal(rec.condition_since, rec.changed_at);
  assert.equal(rec.last_success_at, null);
  const seq = rec.seq;
  h.tick(16000);
  await h.flushTimers();
  const beat = JSON.parse(h.runs.at(-1).init.stdin);
  assert.equal(beat.seq, seq);
  assert(beat.heartbeat_at > rec.heartbeat_at);
});

test('a completed step records last_success_at and clears the condition', async () => {
  const h = await boot({ SUTANDO_INSTANCE_ID: WID, SUTANDO_TMUX_SESSION: 'sutando-worker-' + WID });
  await h.fire('turn.start', { turnId: 't1' });
  await h.fire('classic.StopFailure', { error: 'rate_limit' });
  h.tick(1000);
  await h.handlers['turn.step']({ ...h.$ }, { turnId: 't1' }, async function* () { yield* []; return 1; }).next();
  await h.flushTimers();
  const rec = JSON.parse(h.runs.at(-1).init.stdin);
  assert.equal(rec.seat, WID);
  assert.equal(rec.condition, 'healthy');
  assert.equal(rec.condition_since, null);
  assert(rec.last_success_at > 0);
});

test('subagent turns neither change the record nor leak into the main loop', async () => {
  const h = await boot({ SUTANDO_CORE_SESSION: '1', SUTANDO_TMUX_SESSION: 'sutando-core' });
  await h.fire('turn.start', { turnId: 'main' });
  await h.fire('classic.StopFailure', { error: 'authentication_failed' });
  await h.flushTimers();
  const before = JSON.parse(h.runs.at(-1).init.stdin);
  h.tick(1000);
  await h.fire('turn.start', { turnId: 'sub', agentId: 'agent-1' });
  await h.handlers['turn.step']({ ...h.$ }, { turnId: 'sub', agentId: 'agent-1' }, async function* () { yield* []; return 1; }).next();
  await h.fire('turn.complete', { turnId: 'sub', agentId: 'agent-1', reason: 'answer' });
  await h.flushTimers();
  const after = JSON.parse(h.runs.at(-1).init.stdin);
  for (const k of ['seq', 'phase', 'condition', 'reason', 'condition_since', 'last_success_at']) assert.deepEqual(after[k], before[k], k);
  await h.handlers['turn.step']({ ...h.$ }, { turnId: 'sub' }, async function* () { yield* []; return 1; }).next();
  await h.handlers['turn.step']({ ...h.$ }, { turnId: 'main', agentId: 'agent-1' }, async function* () { yield* []; return 1; }).next();
  await h.flushTimers();
  assert.equal(JSON.parse(h.runs.at(-1).init.stdin).last_success_at, null, 'no subagent step counts as a main-loop success');
});

test('an unclassified failure is api-error, never abnormal without a reason', async () => {
  const h = await boot({ SUTANDO_CORE_SESSION: '1' });
  await h.fire('turn.start', { turnId: 't1' });
  await h.handlers['turn.step']({ ...h.$ }, { turnId: 't1' }, async function* () { yield* []; return 1; }).next();
  await h.fire('turn.complete', { turnId: 't1', reason: 'error' });
  await h.flushTimers();
  const rec = JSON.parse(h.runs.at(-1).init.stdin);
  assert.deepEqual([rec.condition, rec.reason, rec.phase], ['abnormal', 'api-error', 'failed']);
  await h.fire('classic.StopFailure', { error: 'model_not_found' });
  await h.flushTimers();
  assert.equal(JSON.parse(h.runs.at(-1).init.stdin).reason, 'api-error');
});

test('a tool running answers a permission wait; subagent compaction is ignored', async () => {
  const h = await boot({ SUTANDO_CORE_SESSION: '1' });
  await h.fire('turn.start', { turnId: 't1' });
  await h.fire('classic.PermissionRequest', {});
  await h.flushTimers();
  assert.equal(JSON.parse(h.runs.at(-1).init.stdin).reason, 'permission');
  await h.handlers['tool.call']({ ...h.$ }, { tool: 'Bash' }, async () => {
    await h.flushTimers();
    const during = JSON.parse(h.runs.at(-1).init.stdin);
    assert.deepEqual([during.phase, during.condition, during.reason], ['tool', 'healthy', null]);
    return {};
  });
  const n = h.runs.length;
  await h.fire('classic.PreCompact', { agent_id: 'sub' });
  await h.flushTimers();
  assert.notEqual(JSON.parse(h.runs.at(-1).init.stdin).phase, 'compacting');
  assert(h.runs.length >= n);
});

test('a change during a flush schedules another, never two at once', async () => {
  const h = await boot({ SUTANDO_CORE_SESSION: '1' });
  h.hold();
  await h.fire('turn.start', { turnId: 't1' });
  const due = h.timers.splice(0).filter((t) => t.ms === 250);
  due[0].fn();
  await new Promise((r) => setTimeout(r, 5));
  await h.fire('classic.StopFailure', { error: 'overloaded' });
  await h.flushTimers();
  assert.equal(h.runs.length, 1, 'the timer that fires mid-flush starts nothing');
  h.free();
  await new Promise((r) => setTimeout(r, 5));
  await h.flushTimers();
  assert.equal(h.runs.length, 2);
  assert.equal(JSON.parse(h.runs[1].init.stdin).condition, 'abnormal');
});

test('guest sessions and failing writes never write or break events', async () => {
  const guest = await boot({ SUTANDO_TMUX_SESSION: 'adhoc' });
  await guest.fire('turn.start', { turnId: 't1' });
  await guest.flushTimers();
  assert.equal(guest.runs.length, 0);
  assert.equal(guest.timers.length, 0);
  const h = await boot({ SUTANDO_CORE_SESSION: '1' });
  h.$.process.run = async () => { throw new Error('no python'); };
  await h.fire('turn.start', { turnId: 't1' });
  await h.flushTimers();
});

test('no resolvable python means no write, and the session carries on', async () => {
  const h = await boot({ SUTANDO_CORE_SESSION: '1' }, undefined, null);
  await h.fire('turn.start', { turnId: 't1' });
  await h.flushTimers();
  assert.equal(h.runs.length, 0);
  assert(h.resolves.length >= 1);
});

test('the band is one label line and yields to an occupied slot', async () => {
  const h = await boot({ SUTANDO_CORE_SESSION: '1' });
  const render = h.handlers['ui.render:AbovePrompt'];
  const props = { bodyColumns: 80, maxRows: 4, hasSurvey: false };
  await h.fire('turn.start', { turnId: 't1' });
  assert.deepEqual(await render(h.$, { props }, async () => null), { type: 'Text', children: ['Sutando: thinking'] });
  await h.fire('classic.StopFailure', { error: 'authentication_failed' });
  assert.deepEqual(await render(h.$, { props: { ...props, bodyColumns: 12 } }, async () => null), { type: 'Text', children: ['Sutando: nee'] });
  const taken = { type: 'Text', children: ['engine'] };
  assert.equal(await render(h.$, { props }, async () => taken), taken);
  assert.equal(await render(h.$, { props: { ...props, hasSurvey: true } }, async () => null), null);
  assert.equal(await render(h.$, { props: { ...props, maxRows: 0 } }, async () => null), null);
});
