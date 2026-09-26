/**
 * Point a Twilio number's voice + status webhooks at the base the phone server
 * bound — the same two REST calls as `twilio-setup.py set-webhook`, run at
 * startup when TWILIO_AUTO_WEBHOOK=1.
 *
 * Every failure is a logged skip, never a throw and never a stall: each call to
 * api.twilio.com carries a deadline (`timeoutMs`, 10 s by default) that holds
 * until its BODY has been read, so a hung Twilio API — or one that answers the
 * headers and then goes quiet — cannot hold the server's startup, and an HTTP or network error
 * leaves the server answering on the URL it bound with `set-webhook` as the
 * manual retry. The outcome is returned so a caller can log or test it.
 */
export interface TwilioCreds {
	sid: string;
	token: string;
	number: string;
}

export interface SyncOptions {
	fetchImpl?: typeof fetch;
	timeoutMs?: number;
	log?: (line: string) => void;
	error?: (line: string) => void;
}

export type SyncOutcome = 'unchanged' | 'updated' | 'skipped';

export const TWILIO_SYNC_TIMEOUT_MS = 10_000;

type OwnedNumber = { sid: string; voice_url?: string; status_callback?: string };

// A ref'd timer: AbortSignal.timeout() unrefs its own, so with nothing else pending it never fires.
function deadline(ms: number): { signal: AbortSignal; clear: () => void } {
	const ctl = new AbortController();
	const timer = setTimeout(() => ctl.abort(new DOMException(`no answer within ${ms} ms`, 'TimeoutError')), ms);
	return { signal: ctl.signal, clear: () => clearTimeout(timer) };
}

// Settle `p` or reject with the signal's reason, whichever comes first. The
// body read is raced against the signal rather than trusted to honour it: a
// Response that is not tied to the fetch signal (a test double, a polyfill)
// would otherwise read forever after the headers arrived.
function under<T>(signal: AbortSignal, p: Promise<T>): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		if (signal.aborted) { reject(signal.reason); return; }
		const onAbort = () => reject(signal.reason);
		signal.addEventListener('abort', onAbort, { once: true });
		p.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort));
	});
}

type Exchange<T> = { ok: boolean; status: number; body: T };

export async function syncTwilioWebhook(creds: TwilioCreds, base: string, opts: SyncOptions = {}): Promise<SyncOutcome> {
	const fetchImpl = opts.fetchImpl ?? fetch;
	const timeoutMs = opts.timeoutMs ?? TWILIO_SYNC_TIMEOUT_MS;
	const log = opts.log ?? (() => {});
	const error = opts.error ?? (() => {});
	const auth = Buffer.from(`${creds.sid}:${creds.token}`).toString('base64');
	const api = `https://api.twilio.com/2010-04-01/Accounts/${creds.sid}`;
	const wantVoice = `${base}/twilio/connect`;
	const wantStatus = `${base}/twilio/status`;
	// One deadline per exchange, cleared only after `read` has consumed the
	// body: clearing it when the headers arrived left a body that stalls after
	// them holding the sync past its own timeout (review of #4666).
	const call = async <T>(url: string, init: RequestInit, read: (res: Response) => Promise<T>): Promise<Exchange<T>> => {
		const d = deadline(timeoutMs);
		try {
			const res = await fetchImpl(url, { ...init, signal: d.signal });
			return { ok: res.ok, status: res.status, body: await under(d.signal, read(res)) };
		} finally { d.clear(); }
	};
	try {
		const list = await call(`${api}/IncomingPhoneNumbers.json?PhoneNumber=${encodeURIComponent(creds.number)}`, {
			headers: { Authorization: `Basic ${auth}` },
		}, async (res) => (res.ok ? await res.json() as { incoming_phone_numbers?: OwnedNumber[] } : {}));
		if (!list.ok) { error(`[Twilio] webhook sync: list failed HTTP ${list.status}`); return 'skipped'; }
		const num = list.body.incoming_phone_numbers?.[0];
		if (!num) { error('[Twilio] webhook sync: TWILIO_PHONE_NUMBER is not owned by this account'); return 'skipped'; }
		if (num.voice_url === wantVoice && num.status_callback === wantStatus) {
			log('[Twilio] webhook already points here');
			return 'unchanged';
		}
		const form = new URLSearchParams({ VoiceUrl: wantVoice, VoiceMethod: 'POST', StatusCallback: wantStatus, StatusCallbackMethod: 'POST' });
		const upd = await call(`${api}/IncomingPhoneNumbers/${num.sid}.json`, {
			method: 'POST',
			headers: { Authorization: `Basic ${auth}` },
			body: form,
		}, async (res) => (res.ok ? '' : (await res.text()).slice(0, 200)));
		if (!upd.ok) { error(`[Twilio] webhook sync: update failed HTTP ${upd.status}: ${upd.body}`); return 'skipped'; }
		log(`[Twilio] webhook now ${wantVoice}`);
		return 'updated';
	} catch (err) {
		if ((err as { name?: string } | null)?.name === 'TimeoutError') {
			error(`[Twilio] webhook sync skipped: api.twilio.com did not answer within ${timeoutMs} ms — run twilio-setup.py set-webhook`);
		} else {
			error(`[Twilio] webhook sync failed: ${err instanceof Error ? err.message : String(err)}`);
		}
		return 'skipped';
	}
}
