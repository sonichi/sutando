import { useMemo, useState } from 'react';
import { APP_COPY } from '@/const-values/app-copy';
import type { Subscription } from '@/types/subscription';
import { monthlyUsd } from '@/utils/subscription-burn';

type SortKey = 'vendor' | 'amount' | 'status';

// Full literal class strings so Tailwind's source scan picks them up.
const STATUS_TONE: Record<Subscription['status'], string> = {
	active: 'text-[color:var(--color-success)]',
	cancelled: 'text-[color:var(--color-danger)] line-through',
	uncertain: 'text-[color:var(--color-warning)]',
};

export interface SubscriptionTableProps {
	subscriptions: readonly Subscription[];
	/** Vendors flagged `added` by the most recent scan — marked with a dot. */
	addedVendors: ReadonlySet<string>;
}

const TH = 'px-3 py-2 text-left font-medium text-[color:var(--color-text-mute)]';
const TD = 'px-3 py-2 text-[color:var(--color-text-dim)]';

export default function SubscriptionTable({ subscriptions, addedVendors }: SubscriptionTableProps) {
	const [sortKey, setSortKey] = useState<SortKey>('amount');
	const [asc, setAsc] = useState(false);

	const sorted = useMemo(() => {
		const rows = [...subscriptions];
		rows.sort((a, b) => {
			const cmp =
				sortKey === 'vendor'
					? a.vendor.localeCompare(b.vendor)
					: sortKey === 'amount'
						? monthlyUsd(a) - monthlyUsd(b)
						: a.status.localeCompare(b.status);
			return asc ? cmp : -cmp;
		});
		return rows;
	}, [subscriptions, sortKey, asc]);

	const onSort = (key: SortKey) => {
		if (key === sortKey) {
			setAsc((v) => !v);
			return;
		}
		setSortKey(key);
		setAsc(key === 'vendor');
	};

	const arrow = (key: SortKey) => (key === sortKey ? (asc ? ' ▲' : ' ▼') : '');

	return (
		<div className="overflow-x-auto rounded-xl border border-neutral-800">
			<table className="w-full border-collapse text-sm">
				<thead className="border-b border-neutral-800 bg-[color:var(--color-surface)]">
					<tr>
						<th className={`${TH} cursor-pointer`} onClick={() => onSort('vendor')}>
							{APP_COPY.subsColVendor}
							{arrow('vendor')}
						</th>
						<th className={`${TH} cursor-pointer`} onClick={() => onSort('amount')}>
							{APP_COPY.subsColAmount}
							{arrow('amount')}
						</th>
						<th className={TH}>{APP_COPY.subsColFrequency}</th>
						<th className={TH}>{APP_COPY.subsColAccount}</th>
						<th className={TH}>{APP_COPY.subsColNextCharge}</th>
						<th className={`${TH} cursor-pointer`} onClick={() => onSort('status')}>
							{APP_COPY.subsColStatus}
							{arrow('status')}
						</th>
					</tr>
				</thead>
				<tbody>
					{sorted.map((sub, i) => (
						<tr key={`${sub.vendor}-${i}`} className="border-b border-neutral-900 last:border-0">
							<td className={`${TD} text-[color:var(--color-text)]`}>
								{addedVendors.has(sub.vendor) ? (
									<span
										className="mr-1.5 text-[color:var(--color-success)]"
										title="Added since the previous scan"
									>
										●
									</span>
								) : null}
								{sub.vendor}
							</td>
							<td className={TD}>
								{Number(sub.amount ?? 0).toFixed(2)} {sub.currency}
							</td>
							<td className={TD}>{sub.frequency}</td>
							<td className={TD}>{sub.account ?? '—'}</td>
							<td className={TD}>{sub.next_charge ?? '—'}</td>
							<td className={`px-3 py-2 ${STATUS_TONE[sub.status]}`}>{sub.status}</td>
						</tr>
					))}
				</tbody>
			</table>
		</div>
	);
}
