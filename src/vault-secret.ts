/**
 * Read a secret the owner stored with `vault set KEY …` from the Keychain item
 * that src/vault_intercept.py writes (`security add-generic-password -a sutando -s KEY`).
 *
 * TypeScript twin of src/channel_token.py's vault tier: a service that reads
 * its credentials from process.env alone makes `vault set` a no-op for it.
 * `envOrVault()` keeps the same order as the Python resolver — an exported
 * value (the sourced .env included) wins, the vault answers only when the
 * environment has nothing usable.
 *
 * Total on failure: no `security` binary, a locked keychain, a missing item
 * or a bad key name all read as '' — never a throw, never a log line that
 * carries the value.
 */
import { execFileSync } from 'node:child_process';

export const VAULT_KEYCHAIN_ACCOUNT = 'sutando';

// Keys double as env-var names (src/vault_intercept.py's _ENV_KEY_RE).
const ENV_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;

export type SecretExec = typeof execFileSync;

export function vaultSecret(key: string, exec: SecretExec = execFileSync): string {
	if (!ENV_KEY.test(key)) return '';
	try {
		const out = exec('security', ['find-generic-password', '-a', VAULT_KEYCHAIN_ACCOUNT, '-s', key, '-w'], {
			encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 5_000,
		});
		return String(out).trim();
	} catch {
		return '';
	}
}

export function envOrVault(key: string, env: NodeJS.ProcessEnv = process.env, exec?: SecretExec): string {
	const fromEnv = (env[key] ?? '').trim();
	return fromEnv || vaultSecret(key, exec);
}
