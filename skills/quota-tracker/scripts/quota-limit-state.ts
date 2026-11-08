/**
 * The account's limit state from one quota record: `allowed`, `overage` (a window
 * is rejected but extra usage is serving the request) or `rejected`.
 *
 * Its own module, free of imports, so the credential proxy AND the fallback
 * policy decide with the SAME rule: the policy cannot import the proxy (the
 * proxy imports the policy). Before this, the policy read a raw `rejected`
 * status and asked for a runtime switch on a request that overage had served
 * (#5275 review).
 */

/** The two fields of a rejection-ledger entry this rule reads. */
interface LedgerEntry { status?: unknown; ts?: unknown }

export type LimitState = 'allowed' | 'overage' | 'rejected';

/** Same rule as `classify_limit_state` in src/quota_availability.py; both run
 *  tests/fixtures/quota-limit-state.parity.json. `overage` is served, not paused. */
export function limitState(record: unknown): LimitState {
	const rec = (record && typeof record === 'object' && !Array.isArray(record)) ? record as Record<string, unknown> : {};
	const h = (rec.headers && typeof rec.headers === 'object' && !Array.isArray(rec.headers)) ? rec.headers as Record<string, unknown> : {};
	const prefix = 'anthropic-ratelimit-unified-';
	const limited = h[`${prefix}status`] === 'rejected' || Object.entries(h).some(([k, v]) => {
		if (!k.startsWith(prefix) || !k.endsWith('-status')) return false;
		const w = k.slice(prefix.length, -'-status'.length);
		return w !== '' && w !== 'overage' && v === 'rejected';
	});
	if (!limited) return 'allowed';
	const overage = h[`${prefix}overage-status`];
	if (overage !== 'allowed' && overage !== 'allowed_warning') return 'rejected';
	const checked = typeof rec.last_checked === 'string' ? Date.parse(rec.last_checked) : NaN;
	const ledger = Array.isArray(rec.recent_rejections) ? rec.recent_rejections : [];
	for (const r of ledger) {
		if (!r || typeof r !== 'object' || (r as LedgerEntry).status !== 429) continue;
		const at = typeof (r as LedgerEntry).ts === 'string' ? Date.parse((r as LedgerEntry).ts) : NaN;
		// An unordered 429 (either stamp unreadable) is not known to be older.
		if (Number.isNaN(checked) || Number.isNaN(at) || at >= checked) return 'rejected';
	}
	return 'overage';
}
