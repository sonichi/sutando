/**
 * task_envelope.ts — TypeScript mirror of src/task_envelope.py's stamping
 * half, for the TS task writers (voice delegation seam, context-drop,
 * wearable). Wire format, key location, and canonical-slot rules are the
 * Python module's; the cross-language parity test pins them together.
 * Verification stays Python-only — consumers are Python, so TS never
 * needs (and does not get) a verify path.
 */
import { createHmac, randomBytes } from 'node:crypto';
import { linkSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

const STAMP_PREFIX = 'envelope_hmac: v1:';

export function keyPath(workspace?: string): string {
	return join(workspace ?? resolveWorkspace(), 'state', 'auth', 'task-hmac.key');
}

function parseKey(text: string, p: string): Buffer {
	// Buffer.from(..,'hex') never throws: an empty or malformed key file
	// silently yields a short (even zero-length) key that stamps envelopes
	// no verifier accepts. Reject anything but exactly 32 bytes of hex.
	const key = Buffer.from(text, 'hex');
	if (text.length !== 64 || key.length !== 32) {
		throw new Error(`invalid task-hmac key at ${p} (want 64 hex chars, got ${text.length})`);
	}
	return key;
}

function loadOrCreateKey(workspace?: string): Buffer {
	const p = keyPath(workspace);
	let text: string | null = null;
	try {
		text = readFileSync(p, 'utf-8').trim();
	} catch { /* no key yet — create below */ }
	if (text === null) {
		mkdirSync(dirname(p), { recursive: true });
		// Complete bytes land in a tmp file first; link() publishes atomically
		// with first-writer-wins — a reader can never see a partial key.
		const tmp = `${p}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`;
		writeFileSync(tmp, randomBytes(32).toString('hex'), { mode: 0o600, flag: 'wx' });
		try {
			linkSync(tmp, p);
		} catch { /* concurrent creator won — read theirs */ } finally {
			try { unlinkSync(tmp); } catch { /* already gone */ }
		}
		text = readFileSync(p, 'utf-8').trim();
	}
	return parseKey(text, p);
}

/** Canonical slot only (line 0, or line 1 after `id:`) — a stamp-shaped
 *  line anywhere else is user content and must survive byte-identically. */
function stripStamp(text: string): { body: string; mac: string | null } {
	const lines = text.split('\n');
	for (const i of [0, 1]) {
		if (i < lines.length && lines[i].startsWith(STAMP_PREFIX)
			&& (i === 0 || lines[0].startsWith('id:'))) {
			const mac = lines[i].slice(STAMP_PREFIX.length).trim();
			return { body: [...lines.slice(0, i), ...lines.slice(i + 1)].join('\n'), mac };
		}
	}
	return { body: text, mac: null };
}

function mac(bodyWithoutStamp: string, key: Buffer): string {
	return createHmac('sha256', key).update(bodyWithoutStamp, 'utf-8').digest('hex');
}

/** Mirror of Python stamp_text: fresh stamp after the `id:` line (slot 1),
 *  else at line 0; a pre-existing canonical stamp is replaced, never doubled. */
export function stampText(text: string, workspace?: string): string {
	const { body } = stripStamp(text);
	const key = loadOrCreateKey(workspace);
	const lines = body.split('\n');
	const at = lines.length > 0 && lines[0].startsWith('id:') ? 1 : 0;
	lines.splice(at, 0, STAMP_PREFIX + mac(body, key));
	return lines.join('\n');
}

/** Fail-open wrapper for writer edges: a stamping error costs the stamp,
 *  never the task write. */
export function tryStampText(text: string, workspace?: string): string {
	try {
		return stampText(text, workspace);
	} catch {
		return text;
	}
}
