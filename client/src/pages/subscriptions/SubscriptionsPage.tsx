import { useMemo } from 'react';
import PageHeader from '@/components/atoms/page-header';
import SubscriptionSummary from '@/components/molecules/subscription-summary';
import SubscriptionTable from '@/components/organisms/subscription-table';
import { APP_COPY } from '@/const-values/app-copy';
import { APP_ROUTES } from '@/const-values/app-routes';
import { useSubscriptions } from '@/hooks/useSubscriptions';

/**
 * Subscriptions page — surfaces the agent-maintained paid-subscription
 * tracker (subscription-scanner skill). Summary cards + sortable table,
 * with a localhost-gated "Scan now" button that queues an out-of-cycle
 * scan. Ported from the legacy web-client /paidsubscriptions page (#651).
 */
export default function SubscriptionsPage() {
	const { state, data, error, scanning, scan } = useSubscriptions();

	const addedVendors = useMemo(() => {
		const last = data?.scan_history?.[data.scan_history.length - 1];
		return new Set<string>(last?.added ?? []);
	}, [data]);

	const subscriptions = data?.subscriptions ?? [];
	const hasData = subscriptions.length > 0;
	const lastScan = data?.last_scan
		? new Date(data.last_scan).toLocaleString()
		: APP_COPY.subsNeverScanned;

	return (
		<section className="flex h-full flex-col">
			<PageHeader title={APP_ROUTES.subscriptions.label} hint={APP_ROUTES.subscriptions.hint} />
			<div className="flex-1 space-y-5 overflow-y-auto px-6 py-6">
				<div className="flex flex-wrap items-center justify-between gap-3">
					<p className="text-xs text-[color:var(--color-text-mute)]">
						{APP_COPY.subsLastScan}: {lastScan}
					</p>
					<button
						type="button"
						onClick={() => void scan()}
						disabled={scanning}
						className="rounded-md border border-neutral-700 px-3 py-1.5 text-sm text-[color:var(--color-text)] hover:border-[color:var(--color-accent)] disabled:opacity-50"
					>
						{scanning ? APP_COPY.subsScanning : APP_COPY.subsScanNow}
					</button>
				</div>

				{error ? (
					<p className="text-sm text-[color:var(--color-danger)]">
						{APP_COPY.subsLoadFailed} {error}
					</p>
				) : null}

				{state === 'loading' ? (
					<p className="text-sm text-[color:var(--color-text-mute)]">{APP_COPY.subsLoading}</p>
				) : hasData ? (
					<>
						<SubscriptionSummary subscriptions={subscriptions} />
						<SubscriptionTable subscriptions={subscriptions} addedVendors={addedVendors} />
					</>
				) : (
					<p className="text-sm text-[color:var(--color-text-mute)]">{APP_COPY.subsEmpty}</p>
				)}
			</div>
		</section>
	);
}
