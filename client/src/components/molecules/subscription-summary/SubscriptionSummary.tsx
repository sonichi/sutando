import { APP_COPY } from '@/const-values/app-copy';
import type { Subscription } from '@/types/subscription';
import { monthlyBurnUsd } from '@/utils/subscription-burn';

interface SummaryCard {
	label: string;
	value: string;
	tone: string;
}

export interface SubscriptionSummaryProps {
	subscriptions: readonly Subscription[];
}

/**
 * Four at-a-glance cards above the subscription table: active count,
 * estimated monthly burn (USD), uncertain count, recently-cancelled count.
 */
export default function SubscriptionSummary({ subscriptions }: SubscriptionSummaryProps) {
	const active = subscriptions.filter((s) => s.status === 'active').length;
	const uncertain = subscriptions.filter((s) => s.status === 'uncertain').length;
	const cancelled = subscriptions.filter((s) => s.status === 'cancelled').length;
	const burn = monthlyBurnUsd(subscriptions);

	// Full literal class strings — Tailwind scans source for verbatim
	// utilities, so the tone must not be assembled by concatenation.
	const cards: SummaryCard[] = [
		{ label: APP_COPY.subsCardActive, value: String(active), tone: 'text-[color:var(--color-text)]' },
		{ label: APP_COPY.subsCardBurn, value: `$${burn.toFixed(0)}`, tone: 'text-[color:var(--color-accent)]' },
		{ label: APP_COPY.subsCardUncertain, value: String(uncertain), tone: 'text-[color:var(--color-warning)]' },
		{ label: APP_COPY.subsCardCancelled, value: String(cancelled), tone: 'text-[color:var(--color-danger)]' },
	];

	return (
		<div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
			{cards.map((card) => (
				<div
					key={card.label}
					className="rounded-xl border border-neutral-800 bg-[color:var(--color-surface)] px-4 py-3"
				>
					<div className={`text-2xl font-semibold ${card.tone}`}>{card.value}</div>
					<div className="mt-0.5 text-xs text-[color:var(--color-text-mute)]">{card.label}</div>
				</div>
			))}
		</div>
	);
}
