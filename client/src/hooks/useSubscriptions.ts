import { useCallback, useEffect, useState } from 'react';
import { fetchSubscriptions, triggerSubscriptionScan } from '@/lib/api';
import type { SubscriptionsData } from '@/types/subscription';

export type SubscriptionsState = 'loading' | 'ready' | 'error';

export interface UseSubscriptionsResult {
	state: SubscriptionsState;
	data: SubscriptionsData | null;
	error: string | null;
	scanning: boolean;
	scan: () => Promise<void>;
	reload: () => void;
}

const SCAN_POLL_INTERVAL_MS = 6000;
const SCAN_POLL_MAX_TRIES = 20; // ~2 min ceiling

/**
 * Subscription tracker state for the /subscriptions page. Loads the state
 * file on mount; `scan()` queues an out-of-cycle scan and then polls until
 * the file's `last_scan` changes (the scan runs as an async agent task) or
 * the poll budget runs out.
 */
export function useSubscriptions(): UseSubscriptionsResult {
	const [state, setState] = useState<SubscriptionsState>('loading');
	const [data, setData] = useState<SubscriptionsData | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [scanning, setScanning] = useState(false);
	const [reloadKey, setReloadKey] = useState(0);

	useEffect(() => {
		const controller = new AbortController();
		fetchSubscriptions(controller.signal)
			.then((d) => {
				setData(d);
				setState('ready');
			})
			.catch((e: unknown) => {
				if (controller.signal.aborted) return;
				setError(e instanceof Error ? e.message : 'Failed to load subscriptions');
				setState('error');
			});
		return () => controller.abort();
	}, [reloadKey]);

	const reload = useCallback(() => setReloadKey((k) => k + 1), []);

	const scan = useCallback(async () => {
		setScanning(true);
		setError(null);
		const before = data?.last_scan ?? null;
		try {
			await triggerSubscriptionScan();
			// The scan runs as a queued agent task — poll for the state file
			// to pick up a fresh `last_scan` before giving up.
			for (let i = 0; i < SCAN_POLL_MAX_TRIES; i++) {
				await new Promise((resolve) => setTimeout(resolve, SCAN_POLL_INTERVAL_MS));
				const fresh = await fetchSubscriptions().catch(() => null);
				if (fresh && fresh.last_scan !== before) {
					setData(fresh);
					break;
				}
			}
		} catch (e: unknown) {
			setError(e instanceof Error ? e.message : 'Failed to queue scan');
		} finally {
			setScanning(false);
		}
	}, [data?.last_scan]);

	return { state, data, error, scanning, scan, reload };
}
