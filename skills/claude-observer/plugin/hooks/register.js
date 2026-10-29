// Main-loop observations drawn as a one-line band and, for core and pool seats, published as a runtime observation record.
let phase = 'unk';
let motion = 'unk';
let condition = 'unk';
let reason = '-';
const mainTurns = new Set();

const OBSERVER = 'claude-observer';
const VERSION = '0.3.0';
const FLUSH_DELAY_MS = 250;
const HEARTBEAT_MS = 15000;
const WRITE_TIMEOUT_MS = 5000;
const PHASE_NAME = {req: 'requesting', tool: 'tool', idle: 'idle', fail: 'failed', wait: 'waiting', cmp: 'compacting', unk: 'unknown'};
const MOTION_NAME = {mov: 'moving', idle: 'idle', unk: 'unknown'};
const CONDITION_NAME = {ok: 'healthy', bad: 'abnormal', unk: 'unknown'};
const REASON_NAME = {auth: 'needs-login', quota: 'quota-limit', funds: 'out-of-credits', retry: 'api-error', perm: 'permission', input: 'awaiting-input'};

let recSeq = 0;
let changedAt = 0;
let conditionSince = null;
let lastSuccessAt = null;
let seat = null;
let observerId = '';
let startedAt = 0;
let claudeSessionId = null;
let engineDir = '';
let python = '';
let workspaceDir = '';
let initPromise = null;
let dirty = false;
let timerPending = false;
let inFlight = false;

const AUTH_ERRORS = new Set(['authentication_failed', 'oauth_org_not_allowed', 'account_on_hold', 'verification_required', 'cloud_credential_error']);
const ERROR_REASON = {billing_error: 'funds', rate_limit: 'quota', overloaded: 'retry', server_error: 'retry'};
const REASON_LABEL = {auth: 'needs login', quota: 'usage limit reached', funds: 'billing problem', retry: 'API unavailable', perm: 'waiting for permission', input: 'waiting for input'};
const PHASE_LABEL = {req: 'thinking', tool: 'using a tool', idle: 'idle', cmp: 'compacting', unk: 'starting'};

export function errorReason(error) {
  return AUTH_ERRORS.has(error) ? 'auth' : (ERROR_REASON[error] || '-');
}

// Worker seat from its instance id, else the core; any other session (guest, ad-hoc) has none.
export function seatFromEnv(instanceId, coreSession, tmuxSession) {
  const session = String(tmuxSession || '').slice(0, 80);
  if (/^[0-9a-f]{32}$/.test(instanceId || '')) return session ? {seat: instanceId, session} : null;
  if (coreSession === '1') return {seat: 'core', session: session || 'sutando-core'};
  return null;
}

// Start of the current abnormal condition: set on entering it or changing its reason, cleared otherwise.
export function nextConditionSince(before, after, since, nowSec) {
  if (after.condition !== 'bad') return null;
  if (before.condition === 'bad' && before.reason === after.reason && since !== null) return since;
  return nowSec;
}

// The plugin dir is <engine>/skills/claude-observer/plugin.
export function engineRoot(pluginRoot) {
  return String(pluginRoot).replace(/\/+$/, '').split('/').slice(0, -3).join('/');
}

export function buildRecord(s) {
  return {
    schema: 1,
    observer: OBSERVER,
    observer_version: VERSION,
    observer_id: s.observerId,
    observer_started_at: s.startedAt,
    seat: s.seat,
    session: s.session,
    claude_session_id: s.claudeSessionId || null,
    seq: s.seq,
    changed_at: s.changedAt,
    condition_since: s.conditionSince,
    last_success_at: s.lastSuccessAt,
    heartbeat_at: s.heartbeatAt,
    phase: PHASE_NAME[s.phase] ?? 'unknown',
    motion: MOTION_NAME[s.motion] ?? 'unknown',
    condition: CONDITION_NAME[s.condition] ?? 'unknown',
    reason: s.condition === 'bad' ? (REASON_NAME[s.reason] ?? 'api-error') : null,
  };
}

export function label(p, c, r) {
  if (c === 'bad') return 'Sutando: ' + (REASON_LABEL[r] || 'request failed');
  return 'Sutando: ' + (PHASE_LABEL[p] || p);
}

// Best effort: observation writes never block or alter an event.
async function flush($) {
  timerPending = false;
  if (inFlight || !seat) return;
  inFlight = true;
  dirty = false;
  try {
    python ||= await resolvePython($);
    if (!python) return;
    const record = buildRecord({observerId, startedAt, seat: seat.seat, session: seat.session, claudeSessionId, seq: recSeq,
      changedAt, conditionSince, lastSuccessAt, heartbeatAt: (await $.clock.now()) / 1000, phase, motion, condition, reason});
    const argv = [python, engineDir + '/src/runtime_observation.py', 'write'];
    if (workspaceDir) argv.push('--workspace', workspaceDir);
    await $.process.run(argv, {stdin: JSON.stringify(record), timeoutMs: WRITE_TIMEOUT_MS});
  } catch {
    // The band and the session carry on without the record.
  } finally {
    inFlight = false;
    if (dirty) schedule($);
  }
}

// The engine's own interpreter policy: a bare python3 can land on the macOS developer-tools stub.
async function resolvePython($) {
  const out = await $.process.run(['bash', '-c', '. "$1/scripts/python-binary.sh" && resolve_python "$1"', 'resolve', engineDir],
    {timeoutMs: WRITE_TIMEOUT_MS});
  return out.exitCode === 0 ? out.stdout.trim() : '';
}

function schedule($) {
  if (!seat || timerPending) return;
  timerPending = true;
  $.clock.after(FLUSH_DELAY_MS, () => { flush($); });
}

function beat($) {
  $.clock.after(HEARTBEAT_MS, () => { dirty = true; schedule($); beat($); });
}

function ensureInit($) {
  initPromise ??= (async () => {
    const instanceId = await $.env.get('SUTANDO_INSTANCE_ID');
    const coreSession = await $.env.get('SUTANDO_CORE_SESSION');
    const tmuxSession = await $.env.get('SUTANDO_TMUX_SESSION');
    seat = seatFromEnv(instanceId, coreSession, tmuxSession);
    if (!seat) return;
    workspaceDir = (await $.env.get('SUTANDO_WORKSPACE_DIR')) || '';
    engineDir = engineRoot($.plugin.root);
    observerId = Math.random().toString(16).slice(2, 12) + Math.random().toString(16).slice(2, 12);
    startedAt = (await $.clock.now()) / 1000;
    changedAt = startedAt;
    try { claudeSessionId = (await $.session.id()) || null; } catch { claudeSessionId = null; }
    dirty = true;
    schedule($);
    beat($);
  })().catch(() => { seat = null; });
  return initPromise;
}

// Only a change stamps evidence time; redraws never do.
async function observe($, change) {
  await ensureInit($);
  const [p, m, c, r] = [change.phase ?? phase, change.motion ?? motion, change.condition ?? condition, change.reason ?? reason];
  if (p === phase && m === motion && c === condition && r === reason) return;
  const nowMs = await $.clock.now();
  conditionSince = nextConditionSince({condition, reason}, {condition: c, reason: r}, conditionSince, nowMs / 1000);
  [phase, motion, condition, reason] = [p, m, c, r];
  recSeq += 1;
  changedAt = nowMs / 1000;
  dirty = true;
  schedule($);
  $.ui.invalidate('ui.render');
}

async function markSuccess($) {
  await ensureInit($);
  lastSuccessAt = (await $.clock.now()) / 1000;
  recSeq += 1;
  dirty = true;
  schedule($);
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await ensureInit($);
    return next(e);
  });
  on('turn.start', async ($, e, next) => {
    // Subagent runs raise no turn.start today; refuse one anyway so it can never pass as the main loop.
    if (e.agentId) return next(e);
    mainTurns.add(e.turnId);
    await observe($, {phase: 'req', motion: 'mov'});
    return next(e);
  });
  on('turn.step', async function* ($, e, next) {
    if (e.agentId || !mainTurns.has(e.turnId)) return yield* next(e);
    await observe($, {phase: 'req', motion: 'mov'});
    const result = yield* next(e);
    // A completed request is positive recovery from any earlier failure.
    await markSuccess($);
    await observe($, {condition: 'ok', reason: '-'});
    return result;
  });
  on('tool.call', async ($, e, next) => {
    if (e.agentId) return next(e);
    const answered = condition === 'bad' && (reason === 'perm' || reason === 'input');
    await observe($, answered ? {phase: 'tool', motion: 'mov', condition: 'ok', reason: '-'} : {phase: 'tool', motion: 'mov'});
    try {
      return await next(e);
    } finally {
      await observe($, {phase: 'req'});
    }
  });
  on('turn.complete', async ($, e, next) => {
    if (e.agentId || !mainTurns.delete(e.turnId)) return next(e);
    if (e.reason === 'error') await observe($, {phase: 'fail', motion: 'idle', condition: 'bad'});
    else if (e.reason === 'answer') await observe($, {phase: 'idle', motion: 'idle', condition: 'ok', reason: '-'});
    else await observe($, {phase: 'idle', motion: 'idle'});
    return next(e);
  });
  on('classic.StopFailure', async ($, e, next) => {
    if (!e.agent_id) await observe($, {phase: 'fail', motion: 'idle', condition: 'bad', reason: errorReason(e.error)});
    return next(e);
  });
  on('classic.PermissionRequest', async ($, e, next) => {
    if (!e.agent_id) await observe($, {phase: 'wait', motion: 'idle', condition: 'bad', reason: 'perm'});
    return next(e);
  });
  on('classic.Notification', async ($, e, next) => {
    if (!e.agent_id && e.notification_type === 'elicitation_dialog') await observe($, {phase: 'wait', motion: 'idle', condition: 'bad', reason: 'input'});
    if (!e.agent_id && e.notification_type === 'auth_success' && reason === 'auth') await observe($, {condition: 'ok', reason: '-'});
    return next(e);
  });
  on('classic.PreCompact', async ($, e, next) => {
    if (!e.agent_id) await observe($, {phase: 'cmp', motion: 'mov'});
    return next(e);
  });
  on('classic.PostCompact', async ($, e, next) => {
    if (!e.agent_id) await observe($, {phase: 'idle', motion: 'idle'});
    return next(e);
  });
  on('ui.render', {component: 'AbovePrompt'}, async ($, e, next) => {
    const original = await next(e);
    if (e.props.hasSurvey) return original;
    if (original?.children?.length || original?.type === 'Text') return original;
    if (Math.floor(e.props.maxRows) < 1) return original;
    return {type: 'Text', children: [label(phase, condition, reason).slice(0, Math.floor(e.props.bodyColumns))]};
  });
}
