/**
 * Native PIM consent — the TS twin of skills/macos-tools/scripts/native_pim_consent.py.
 *
 * Inline voice/phone tools that would drive the native macOS Calendar, Reminders
 * or Contacts app (a macOS Automation prompt on the owner's screen) call
 * `checkNativePimConsent()` first. Allowed only by the host opt-in the owner set:
 * `SUTANDO_ALLOW_NATIVE_PIM=1` in the server environment, or the persisted marker
 * `<workspace>/state/native-pim-consent` written by `native_pim_consent.py grant`.
 * A stored macOS denial (`<workspace>/state/<app>-automation-denied`, shared with
 * the Python scripts and the morning briefing) is final: no retry, no re-prompt.
 * The tool never asserts consent on its own — this guards against acting on the
 * agent's initiative; it is not an authorisation boundary.
 */

import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

export const CONSENT_MARKER = 'native-pim-consent';
export const GRANT_COMMAND = 'python3 skills/macos-tools/scripts/native_pim_consent.py grant';

export type NativePimApp = 'Calendar' | 'Reminders' | 'Contacts';

export type NativePimConsent =
	| { allowed: true }
	| { allowed: false; reason: 'no-consent' | 'denied'; message: string };

export interface ConsentOptions {
	env?: NodeJS.ProcessEnv;
	workspace?: string;
}

export function denialMarkerName(app: NativePimApp): string {
	return `${app.toLowerCase()}-automation-denied`;
}

export function consentMarkerPath(workspace?: string): string {
	return join(workspace ?? resolveWorkspace(), 'state', CONSENT_MARKER);
}

export function denialMarkerPath(app: NativePimApp, workspace?: string): string {
	return join(workspace ?? resolveWorkspace(), 'state', denialMarkerName(app));
}

/** macOS refused the Apple event: `-1743` / "Not authorized to send Apple events". */
export function isDenialError(text: string): boolean {
	const lowered = (text || '').toLowerCase();
	return lowered.includes('-1743') || lowered.includes('not authorized to send apple events');
}

export function noConsentMessage(app: NativePimApp): string {
	return (
		`The local macOS ${app} app needs the owner's permission once (it raises a macOS prompt), ` +
		`so I did not open it. The owner can allow it for this host by running \`${GRANT_COMMAND}\` ` +
		`in their own terminal, or by setting SUTANDO_ALLOW_NATIVE_PIM=1 in the server .env; ` +
		`until then, ask the owner for the details directly.`
	);
}

export function denialMessage(app: NativePimApp): string {
	return (
		`macOS denied automation access to ${app} (System Settings → Privacy & Security → Automation). ` +
		`I will not retry or ask again; the owner can grant it there and then run \`${GRANT_COMMAND}\`.`
	);
}

export function checkNativePimConsent(app: NativePimApp, opts: ConsentOptions = {}): NativePimConsent {
	const env = opts.env ?? process.env;
	const workspace = opts.workspace ?? resolveWorkspace();
	if (existsSync(denialMarkerPath(app, workspace))) {
		return { allowed: false, reason: 'denied', message: denialMessage(app) };
	}
	const envAllows = (env.SUTANDO_ALLOW_NATIVE_PIM ?? '').trim() === '1';
	if (envAllows || existsSync(consentMarkerPath(workspace))) return { allowed: true };
	return { allowed: false, reason: 'no-consent', message: noConsentMessage(app) };
}

/** Persist a macOS denial so no later call re-asks; an unwritable state dir is not an error. */
export function recordNativePimDenial(app: NativePimApp, workspace?: string): void {
	try {
		const path = denialMarkerPath(app, workspace);
		mkdirSync(join(path, '..'), { recursive: true });
		writeFileSync(path, new Date().toISOString());
	} catch {}
}
