/** The owner's wall clock for the `local_time:` task header; mirrors
 *  local_task_protocol.local_time_value (ISO-8601 with offset, then the IANA zone). */

/** IANA zone of this host as the runtime resolves it (TZ, else /etc/localtime); null when unnamed. */
export function hostZoneName(): string | null {
	try {
		return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
	} catch {
		return null;
	}
}

function pad(n: number): string { return String(n).padStart(2, '0'); }

/** `2026-10-08T13:35:44-07:00 America/Los_Angeles`; the bare offset form when no zone resolves. */
export function localTimeValue(now: Date = new Date(), zone: string | null = hostZoneName()): string {
	const instant = Math.floor(now.getTime() / 1000) * 1000;
	let wall: number;
	let name = zone;
	try {
		if (!name) throw new Error('no zone');
		const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
			timeZone: name, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
			hour: '2-digit', minute: '2-digit', second: '2-digit',
		}).formatToParts(new Date(instant)).map(p => [p.type, p.value]));
		wall = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
	} catch {
		name = null;
		wall = instant - new Date(instant).getTimezoneOffset() * 60000;
	}
	const offMin = Math.round((wall - instant) / 60000);
	const d = new Date(wall);
	const sign = offMin < 0 ? '-' : '+';
	const abs = Math.abs(offMin);
	const stamp = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T` +
		`${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}` +
		`${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
	return name ? `${stamp} ${name}` : stamp;
}
