import type { Subscription } from '@/types/subscription';

/**
 * Rough FX factors to USD. Monthly burn is an estimate — exact rates
 * aren't worth a network call. Extend if the owner's currencies drift.
 */
const USD_PER: Record<string, number> = { USD: 1, EUR: 1.08, GBP: 1.27 };

/** Monthly-equivalent USD cost of one subscription. */
export function monthlyUsd(sub: Subscription): number {
	const fx = USD_PER[(sub.currency ?? 'USD').toUpperCase()] ?? 1;
	const usd = (sub.amount || 0) * fx;
	// annual → /12; monthly + other are already treated as monthly-equivalent.
	return sub.frequency === 'annual' ? usd / 12 : usd;
}

/** Total monthly burn across all active subscriptions, in USD. */
export function monthlyBurnUsd(subs: readonly Subscription[]): number {
	return subs
		.filter((s) => s.status === 'active')
		.reduce((sum, s) => sum + monthlyUsd(s), 0);
}
