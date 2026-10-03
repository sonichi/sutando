import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import type { ObsEvent } from '../../src/observability/events.js';
import type { Sink } from '../../src/observability/sink.js';
import { Collector } from '../../src/observability/collector/collector.js';
import { CoreStateNormalizer, CORE_STATE_SOURCE } from '../../src/observability/core-state-normalizer.js';
import { mapCoreState, mapCoreTurnRefused, refusedTurnKind, coreStateKind, CORE_STATE_OBS_SOURCE } from '../../src/observability/core-state-map.js';

const CTX = { node: 'test-node', receivedAt: 1_700_000_000 };
const raw = (over: Record<string, unknown>) => ({ kind: 'core.state' as const, session: 'sutando-core', to: 'running', ...over });

describe('coreStateKind — precedence', () => {
	const cases: Array<[Record<string, unknown>, string, string]> = [
		[{ from: 'running', to: 'logged-out' }, 'core.auth.login_required', 'denied'],
		[{ from: 'running', to: 'blocked-human', gate: 'login' }, 'core.auth.login_required', 'denied'],
		[{ from: 'logged-out', to: 'idle-ready' }, 'core.auth.recovered', 'ok'],
		[{ from: 'logged-out', to: 'running' }, 'core.auth.recovered', 'ok'],
		[{ from: 'running', to: 'crashed' }, 'core.session.crashed', 'error'],
		[{ from: 'running', to: 'hung' }, 'core.session.hung', 'error'],
		[{ from: 'running', to: 'unobserved' }, 'core.session.unobserved', 'ok'],
		[{ from: 'running', to: 'gateway-down' }, 'core.gateway.down', 'error'],
		[{ from: 'running', to: 'gateway-down', gateway_auth_rejected: true }, 'core.gateway.auth_rejected', 'denied'],
		[{ from: 'gateway-down', to: 'running' }, 'core.gateway.up', 'ok'],
		[{ from: 'running', to: 'blocked-human', gate: 'permission' }, 'core.gate.human', 'ok'],
		[{ from: 'running', to: 'blocked-known', gate: 'press-enter' }, 'core.gate.known', 'ok'],
		[{ from: 'idle-ready', to: 'running' }, 'core.session.running', 'ok'],
		[{ from: 'running', to: 'idle-ready' }, 'core.session.idle', 'ok'],
		[{ from: null, to: 'running' }, 'core.session.running', 'ok'],
		[{ from: 'running', to: 'something-new' }, 'core.state.changed', 'ok'],
	];
	for (const [over, kind, outcome] of cases) {
		it(`${over.from ?? 'null'} → ${over.to}${over.gate ? ` (${over.gate})` : ''}${over.gateway_auth_rejected ? ' auth-rejected' : ''} = ${kind}`, () => {
			assert.deepEqual(coreStateKind(raw(over) as never), { kind, outcome });
		});
	}
	it('a login gate that ends in logged-out stays login_required, not recovered', () => {
		assert.equal(coreStateKind(raw({ from: 'blocked-human', to: 'logged-out' }) as never).kind, 'core.auth.login_required');
	});
	it('login gate → recovered only counts logged-out as the prior state', () => {
		// The gate kind of the PREVIOUS state is not carried, so blocked-human → running is plain running.
		assert.equal(coreStateKind(raw({ from: 'blocked-human', to: 'running' }) as never).kind, 'core.session.running');
	});
});

describe('mapCoreState — event shape', () => {
	it('one transition → one event; trace derived from session; prompt never carried', () => {
		const { events, usage } = mapCoreState(
			raw({ from: 'running', to: 'logged-out', detail: 'core not authenticated (needs /login)', ts: 1_700_000_042.5 }),
			CTX,
		);
		assert.equal(usage.length, 0);
		assert.equal(events.length, 1);
		const ev = events[0];
		assert.equal(ev.schema, 1);
		assert.equal(ev.ts, 1_700_000_042.5);
		assert.equal(ev.trace_id, 'core-sess:sutando-core');
		assert.equal(ev.node, 'test-node');
		assert.equal(ev.source, CORE_STATE_OBS_SOURCE);
		assert.equal(ev.kind, 'core.auth.login_required');
		assert.equal(ev.outcome, 'denied');
		assert.deepEqual(ev.actor, { user_id: 'core', channel: 'core-supervisor', access_tier: 'owner', tenant_id: null });
		assert.deepEqual(ev.data, {
			from: 'running',
			to: 'logged-out',
			detail: 'core not authenticated (needs /login)',
			session: 'sutando-core',
		});
	});
	it('ts falls back to receivedAt; null from is kept as null', () => {
		const ev = mapCoreState(raw({ from: null, to: 'idle-ready' }), CTX).events[0];
		assert.equal(ev.ts, CTX.receivedAt);
		assert.equal((ev.data as Record<string, unknown>).from, null);
	});
});

describe('refused turns — per occurrence', () => {
	it('classifies the refusal line', () => {
		assert.equal(refusedTurnKind('Not logged in · Please run /login'), 'core.auth.turn_refused');
		assert.equal(refusedTurnKind('OAuth access token has expired. Re-authenticate to continue.'), 'core.auth.turn_refused');
		assert.equal(refusedTurnKind("You're out of usage credits. Run /usage-credits to keep using Fable 5.1"), 'core.limit.turn_refused');
		assert.equal(refusedTurnKind('You hit your weekly limit'), 'core.limit.turn_refused');
		assert.equal(refusedTurnKind('something else entirely'), 'core.turn.refused');
	});
	it('maps to one denied event carrying the line, never a prompt', () => {
		const { events, usage } = mapCoreTurnRefused(
			{ kind: 'core.turn_refused', session: 'sutando-core', line: 'Not logged in · Please run /login', state: 'logged-out', turn: '✻ Worked for 0s · done 3:52 PM', ts: 1_700_000_001 },
			CTX,
		);
		assert.equal(usage.length, 0);
		assert.equal(events.length, 1);
		assert.equal(events[0].kind, 'core.auth.turn_refused');
		assert.equal(events[0].outcome, 'denied');
		assert.equal(events[0].trace_id, 'core-sess:sutando-core');
		assert.deepEqual(events[0].data, { line: 'Not logged in · Please run /login', state: 'logged-out', turn: '✻ Worked for 0s · done 3:52 PM', session: 'sutando-core' });
	});
	it('normalizer accepts it and rejects a refusal without a line', () => {
		const n = new CoreStateNormalizer();
		assert.equal(n.normalize({ kind: 'core.turn_refused', session: 's', line: 'Please run /login' }, CTX).events[0].kind, 'core.auth.turn_refused');
		assert.deepEqual(n.normalize({ kind: 'core.turn_refused', session: 's' }, CTX), { events: [], usage: [] });
		assert.deepEqual(n.normalize({ kind: 'core.turn_refused', line: 'x' }, CTX), { events: [], usage: [] });
	});
});

describe('CoreStateNormalizer — decode + collector integration', () => {
	const n = new CoreStateNormalizer();
	it('source name is the ingest path segment', () => {
		assert.equal(n.source, CORE_STATE_SOURCE);
		assert.equal(CORE_STATE_SOURCE, 'core-state');
	});
	it('drops garbage without throwing', () => {
		for (const bad of [null, 42, 'x', {}, { kind: 'core.state' }, { kind: 'core.state', to: 'running' }, { kind: 'voice.session', to: 'x', session: 's' }, raw({ from: 7 }), raw({ ts: 'now' })]) {
			assert.deepEqual(n.normalize(bad, CTX), { events: [], usage: [] });
		}
	});
	it('a POSTed transition lands in the sink as one core.* event', () => {
		const seen: ObsEvent[] = [];
		const sink: Sink = { type: 'test', write: (ev) => void seen.push(ev) };
		const c = new Collector({ sinks: [sink], usageForwarder: null }).register(n);
		const stat = c.ingest('core-state', raw({ from: 'running', to: 'crashed', detail: 'core process/session not found' }));
		assert.equal(stat.ok, true);
		assert.equal(stat.events, 1);
		assert.equal(seen.length, 1);
		assert.equal(seen[0].kind, 'core.session.crashed');
		assert.equal(seen[0].outcome, 'error');
	});
});
