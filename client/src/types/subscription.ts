/**
 * Shape of `skills/subscription-scanner/state/subscriptions.json` — the
 * agent-maintained subscription tracker surfaced at the /subscriptions
 * route. Mirrors the schema documented in the skill's SKILL.md.
 */

export type SubscriptionStatus = 'active' | 'cancelled' | 'uncertain';
export type SubscriptionFrequency = 'monthly' | 'annual' | 'other';

export interface Subscription {
	vendor: string;
	category?: string;
	amount: number;
	currency: string;
	frequency: SubscriptionFrequency;
	account?: string;
	last_charged?: string | null;
	next_charge?: string | null;
	status: SubscriptionStatus;
	source_sender?: string;
	notes?: string;
}

export interface AmountChange {
	vendor: string;
	from: number;
	to: number;
}

export interface ScanHistoryEntry {
	date: string;
	active_count: number;
	added: string[];
	removed: string[];
	amount_changed: AmountChange[];
}

export interface SubscriptionsData {
	last_scan: string | null;
	subscriptions: Subscription[];
	scan_history: ScanHistoryEntry[];
}
