/**
 * src/vault-secret.ts — the phone server's vault tier. `vault set KEY …`
 * stores a Keychain item; a service that reads process.env alone never sees
 * it. These cases pin the item the reader asks for (the account name
 * src/vault_intercept.py writes), the env-first order, and that every failure
 * reads as '' rather than a throw.
 *
 * Runs under `tsx --test` (npm test); needs no build and no keychain.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { vaultSecret, envOrVault, VAULT_KEYCHAIN_ACCOUNT, type SecretExec } from '../src/vault-secret.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');

function fakeExec(result: string | Error, calls: string[][]): SecretExec {
	return ((file: string, args: string[]) => {
		calls.push([file, ...args]);
		if (result instanceof Error) throw result;
		return result;
	}) as unknown as SecretExec;
}

test('the account name is the one src/vault_intercept.py writes with `vault set`', () => {
	const py = readFileSync(join(REPO, 'src', 'vault_intercept.py'), 'utf8');
	const m = /^_ACCOUNT = "([^"]+)"/m.exec(py);
	assert.ok(m, '_ACCOUNT not found in src/vault_intercept.py');
	assert.equal(VAULT_KEYCHAIN_ACCOUNT, m[1]);
});

test('vaultSecret asks the Keychain for the vault item and trims the answer', () => {
	const calls: string[][] = [];
	assert.equal(vaultSecret('TWILIO_AUTH_TOKEN', fakeExec('tok\n', calls)), 'tok');
	assert.deepEqual(calls, [['security', 'find-generic-password', '-a', 'sutando', '-s', 'TWILIO_AUTH_TOKEN', '-w']]);
});

test('a missing item, a locked keychain or no security binary reads as empty, never a throw', () => {
	const calls: string[][] = [];
	assert.equal(vaultSecret('TWILIO_AUTH_TOKEN', fakeExec(new Error('item not found'), calls)), '');
	assert.equal(calls.length, 1);
});

test('a key that is not an env-var name never reaches the keychain', () => {
	const calls: string[][] = [];
	assert.equal(vaultSecret('BAD KEY', fakeExec('x', calls)), '');
	assert.equal(vaultSecret('-s', fakeExec('x', calls)), '');
	assert.deepEqual(calls, []);
});

test('envOrVault: an exported value wins and the vault is not consulted', () => {
	const calls: string[][] = [];
	assert.equal(envOrVault('TWILIO_ACCOUNT_SID', { TWILIO_ACCOUNT_SID: ' ACenv ' }, fakeExec('ACvault', calls)), 'ACenv');
	assert.deepEqual(calls, []);
});

test('envOrVault: an absent, empty or whitespace value falls through to the vault', () => {
	const calls: string[][] = [];
	assert.equal(envOrVault('TWILIO_ACCOUNT_SID', {}, fakeExec('ACvault\n', calls)), 'ACvault');
	assert.equal(envOrVault('TWILIO_ACCOUNT_SID', { TWILIO_ACCOUNT_SID: '   ' }, fakeExec('ACvault', calls)), 'ACvault');
	assert.equal(calls.length, 2);
	assert.equal(envOrVault('TWILIO_ACCOUNT_SID', {}, fakeExec(new Error('no keychain'), calls)), '');
});
