/**
 * Claude Code id adoption (pure). The interactive core is a process we don't
 * control, so we ADOPT its correlation ids rather than minting our own — that's
 * how the three sources (OTel, hooks, .jsonl) agree on one trace per CC session
 * without coordinating.
 *
 *   session.id  → trace_id   ("cc-sess:<id>")      one CC session = one trace
 *   OTel spanId → span_id    ("sp_<id>")           pass-through, namespaced
 *   request_id  → usage_id   ("cc:tok:<id>[:type]")the idempotency key
 *   org.id      → tenant_id / actor.tenant_id
 *   user.id     → actor.user_id (channel=claude-code, tier=owner)
 */

import type { Actor } from '../events.js';
import type { ResourceAttrs } from './cc-records.js';

export function traceIdFromSession(sessionId?: string): string {
	return 'cc-sess:' + (sessionId && sessionId.length > 0 ? sessionId : 'unknown');
}

export function spanIdFromCcSpan(ccSpanId?: string): string | undefined {
	return ccSpanId ? 'sp_' + ccSpanId : undefined;
}

/** `cc:tok:<request_id>` when a request_id is known (the cross-source idempotency
 *  key); otherwise a coarser `cc:tok:<session>:<ts-bucket>` fallback. An optional
 *  `typeSuffix` distinguishes per-type metric datapoints of the same request so
 *  they don't clobber when metrics are the sole source. */
export function usageIdFromRequest(
	requestId: string | undefined,
	fallback: { sessionId?: string; tsBucket?: number },
	typeSuffix?: string,
): string {
	const base = requestId
		? 'cc:tok:' + requestId
		: 'cc:tok:' + (fallback.sessionId ?? 'unknown') + ':' + (fallback.tsBucket ?? 0);
	return typeSuffix ? base + ':' + typeSuffix : base;
}

export function tenantFromResource(res?: ResourceAttrs): string | null {
	const org = res?.['organization.id'];
	return typeof org === 'string' && org.length > 0 ? org : null;
}

export function actorFromResourceAttrs(res?: ResourceAttrs): Actor {
	const userId =
		(typeof res?.['user.id'] === 'string' && (res['user.id'] as string)) ||
		(typeof res?.['user.account_uuid'] === 'string' && (res['user.account_uuid'] as string)) ||
		'core';
	const actor: Actor = {
		user_id: userId,
		channel: 'claude-code',
		access_tier: 'owner',
		tenant_id: tenantFromResource(res),
	};
	return actor;
}
