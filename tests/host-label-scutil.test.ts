import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

/**
 * resolveHostLabel precedence: env → scutil LocalHostName → short hostname.
 *
 * TS parity for tests/host-label-scutil.test.py — guards the DHCP-hostname-
 * drift fix on the TypeScript side (#1745). Before this, hostLabel() skipped
 * scutil entirely and returned the (drift-prone) short hostname, so TS-side
 * per-host resolution diverged from the py/bash side. Run:
 *   npx tsx --test tests/host-label-scutil.test.ts
 */

import { resolveHostLabel } from '../src/util_paths.js';

const NEVER = (): string => {
	throw new Error('scutil should not be called');
};

describe('resolveHostLabel precedence (#1745 TS parity)', () => {
	it('env SUTANDO_HOST_LABEL wins and short-circuits before scutil', () => {
		assert.equal(resolveHostLabel({ SUTANDO_HOST_LABEL: 'Pinned' }, NEVER, 'ignored.local'), 'Pinned');
	});

	it('legacy SUTANDO_HOST_OVERRIDE is honored', () => {
		assert.equal(resolveHostLabel({ SUTANDO_HOST_OVERRIDE: 'Legacy' }, NEVER, 'ignored.local'), 'Legacy');
	});

	it('an explicit label is used RAW (dotted label must NOT be split)', () => {
		assert.equal(resolveHostLabel({ SUTANDO_HOST_LABEL: 'a.b' }, NEVER, 'ignored'), 'a.b');
	});

	it('scutil LocalHostName wins over a drifting hostname when no env label', () => {
		assert.equal(
			resolveHostLabel({}, () => 'Qingyuns-MacBook-Pro-2200', 'QingyunsMBP2200.attlocal.net'),
			'Qingyuns-MacBook-Pro-2200',
		);
	});

	it('falls back to short hostname when scutil yields empty (non-macOS)', () => {
		assert.equal(resolveHostLabel({}, () => '', 'QingyunsMBP2200.attlocal.net'), 'QingyunsMBP2200');
	});

	it('short-hostname fallback strips the mDNS/domain suffix', () => {
		assert.equal(resolveHostLabel({}, () => '', 'host.example.com'), 'host');
	});
});
