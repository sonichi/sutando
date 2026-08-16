import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { isDmBanned } from '../src/dm-ban.js';

// The TypeScript half of src/dm_ban.py's contract. The defect this pins: the
// runtime used existsSync(), which returns false for EVERY error, so an
// unreadable sentinel authorised the very DM the ban exists to suppress.

function ws(): string {
	const d = mkdtempSync(join(tmpdir(), 'dmban-'));
	mkdirSync(join(d, 'state'), { recursive: true });
	return d;
}

describe('isDmBanned', () => {
	it('sentinel present → banned', () => {
		const d = ws();
		writeFileSync(join(d, 'state', 'dm-ban.sentinel'), '');
		assert.equal(isDmBanned(d), true);
	});

	it('sentinel absent (ENOENT) → NOT banned — the only non-banned answer', () => {
		assert.equal(isDmBanned(ws()), false);
	});

	it('unreadable state dir → banned, because unknown must not authorise a send', () => {
		const d = ws();
		writeFileSync(join(d, 'state', 'dm-ban.sentinel'), '');
		chmodSync(join(d, 'state'), 0o000);
		try {
			// existsSync() returns false here, which is what made the old code
			// fail OPEN; statSync raises EACCES and we count that as banned.
			if (process.getuid?.() === 0) return;   // root ignores the mode bits
			assert.equal(isDmBanned(d), true);
		} finally {
			chmodSync(join(d, 'state'), 0o755);
			rmSync(d, { recursive: true, force: true });
		}
	});

	it('a missing workspace root resolves to NOT banned, never to a throw', () => {
		assert.equal(isDmBanned(join(tmpdir(), 'dmban-nonexistent-root')), false);
	});
});
