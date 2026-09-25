/**
 * Native PIM consent — the voice-process client of skills/macos-tools/scripts/native_pim_consent.py.
 *
 * Inline voice/phone tools that would drive the native macOS Calendar, Reminders
 * or Contacts app (a macOS Automation prompt on the owner's screen) call
 * `checkNativePimConsent()` first and `reportNativePimError()` on an osascript
 * failure. Both run the Python script's `check` / `report-error` subcommands, so
 * the policy (host opt-in via `SUTANDO_ALLOW_NATIVE_PIM=1` or
 * `<workspace>/state/native-pim-consent`, stored `-1743` denials, messages) has
 * one owner; nothing here re-implements a marker name or a match. The script
 * unavailable, an interpreter missing, or unparseable output all read as "not
 * allowed": a tool that cannot check consent does not open the app. The tool
 * never asserts consent on its own — this guards against acting on the agent's
 * initiative; it is not an authorisation boundary.
 */

import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { requirePython } from './python-binary.js';
import { findRepoRoot } from './sutando_config.js';

export const GRANT_COMMAND = 'python3 skills/macos-tools/scripts/native_pim_consent.py grant';
const SCRIPT_RELATIVE = join('skills', 'macos-tools', 'scripts', 'native_pim_consent.py');
const ERROR_TEXT_LIMIT = 4000;

export type NativePimApp = 'Calendar' | 'Reminders' | 'Contacts';

export type NativePimConsent =
	| { allowed: true }
	| { allowed: false; reason: 'no-consent' | 'denied' | 'unavailable'; message: string };

export interface ConsentOptions {
	env?: NodeJS.ProcessEnv;
	workspace?: string;
	/** Tests: the interpreter, the script, and the spawner to use instead of the real ones. */
	python?: string;
	script?: string;
	execFileSync?: typeof execFileSync;
}

export function consentScriptPath(): string {
	const here = dirname(fileURLToPath(import.meta.url));
	return join(findRepoRoot(here) ?? join(here, '..'), SCRIPT_RELATIVE);
}

function runConsent(args: string[], opts: ConsentOptions): unknown {
	const exec = opts.execFileSync ?? execFileSync;
	const python = opts.python ?? requirePython();
	const script = opts.script ?? consentScriptPath();
	const source = opts.env ?? process.env;
	const argv = [script, ...args, ...(opts.workspace ? ['--workspace', opts.workspace] : [])];
	// Only the consent variable comes from the caller's env; the interpreter keeps the rest.
	const env = { ...process.env, SUTANDO_ALLOW_NATIVE_PIM: source.SUTANDO_ALLOW_NATIVE_PIM ?? '' };
	const out = exec(python, argv, { env, timeout: 10_000, stdio: ['ignore', 'pipe', 'pipe'] }).toString();
	return JSON.parse(out);
}

function unavailable(app: NativePimApp, err: unknown): NativePimConsent {
	const detail = err instanceof Error ? err.message : String(err);
	return {
		allowed: false,
		reason: 'unavailable',
		message:
			`The consent check for the local macOS ${app} app could not run (${detail.split('\n')[0]}), ` +
			`so the app was not opened. Ask the owner for the details directly.`,
	};
}

export function checkNativePimConsent(app: NativePimApp, opts: ConsentOptions = {}): NativePimConsent {
	let verdict: unknown;
	try {
		verdict = runConsent(['check', app], opts);
	} catch (err) {
		return unavailable(app, err);
	}
	const v = verdict as { allowed?: unknown; reason?: unknown; message?: unknown };
	if (v?.allowed === true) return { allowed: true };
	if (v?.reason === 'denied' || v?.reason === 'no-consent') {
		return { allowed: false, reason: v.reason, message: String(v.message ?? '') };
	}
	return unavailable(app, new Error('unexpected consent output'));
}

/**
 * Classify an osascript failure through the same policy: a macOS denial is
 * recorded in the shared marker (no later caller re-asks) and answered as final.
 */
export function reportNativePimError(
	app: NativePimApp,
	errorText: string,
	opts: ConsentOptions = {},
): { denied: boolean; message?: string } {
	try {
		const v = runConsent(['report-error', app, '--error', errorText.slice(0, ERROR_TEXT_LIMIT)], opts) as {
			denied?: unknown;
			message?: unknown;
		};
		if (v?.denied === true) return { denied: true, message: String(v.message ?? '') };
	} catch {}
	return { denied: false };
}
