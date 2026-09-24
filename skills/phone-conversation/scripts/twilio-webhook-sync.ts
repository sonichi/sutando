/**
 * Point a Twilio number's voice + status webhooks at the base the phone server
 * bound — the same two REST calls as `twilio-setup.py set-webhook`, run at
 * startup when TWILIO_AUTO_WEBHOOK=1.
 *
 * Every failure is a logged skip, never a throw and never a stall: each call to
 * api.twilio.com carries a deadline (`timeoutMs`, 10 s by default), so a hung
 * Twilio API cannot hold the server's startup, and an HTTP or network error
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

export async function syncTwilioWebhook(creds: TwilioCreds, base: string, opts: SyncOptions = {}): Promise<SyncOutcome> {
	const fetchImpl = opts.fetchImpl ?? fetch;
	const timeoutMs = opts.timeoutMs ?? TWILIO_SYNC_TIMEOUT_MS;
	const log = opts.log ?? (() => {});
	const error = opts.error ?? (() => {});
	const auth = Buffer.from(`${creds.sid}:${creds.token}`).toString('base64');
	const api = `https://api.twilio.com/2010-04-01/Accounts/${creds.sid}`;
	const wantVoice = `${base}/twilio/connect`;
	const wantStatus = `${base}/twilio/status`;
	try {
		const list = await fetchImpl(`${api}/IncomingPhoneNumbers.json?PhoneNumber=${encodeURIComponent(creds.number)}`, {
			headers: { Authorization: `Basic ${auth}` },
			signal: AbortSignal.timeout(timeoutMs),
		});
		if (!list.ok) { error(`[Twilio] webhook sync: list failed HTTP ${list.status}`); return 'skipped'; }
		const data = await list.json() as { incoming_phone_numbers?: OwnedNumber[] };
		const num = data.incoming_phone_numbers?.[0];
		if (!num) { error('[Twilio] webhook sync: TWILIO_PHONE_NUMBER is not owned by this account'); return 'skipped'; }
		if (num.voice_url === wantVoice && num.status_callback === wantStatus) {
			log('[Twilio] webhook already points here');
			return 'unchanged';
		}
		const form = new URLSearchParams({ VoiceUrl: wantVoice, VoiceMethod: 'POST', StatusCallback: wantStatus, StatusCallbackMethod: 'POST' });
		const upd = await fetchImpl(`${api}/IncomingPhoneNumbers/${num.sid}.json`, {
			method: 'POST',
			headers: { Authorization: `Basic ${auth}` },
			body: form,
			signal: AbortSignal.timeout(timeoutMs),
		});
		if (!upd.ok) { error(`[Twilio] webhook sync: update failed HTTP ${upd.status}: ${(await upd.text()).slice(0, 200)}`); return 'skipped'; }
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
