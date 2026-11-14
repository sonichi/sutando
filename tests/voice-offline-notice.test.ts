// #6368: a voice agent restarted during a region outage announced "voice offline" again on every
// restart, because which categories had been announced lived only in the process.

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, chmodSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { OFFLINE_NOTICE_TTL_S, OfflineNoticeStore } from '../src/voice-offline-notice.js';

const fresh = () => join(mkdtempSync(join(tmpdir(), 'voice-offline-notice-')), 'state', 'voice-offline-notified.json');

describe('the offline notice is owed once per outage', () => {
	it('a restarted process knows the owner was already told', () => {
		const path = fresh();
		const first = new OfflineNoticeStore(path);
		assert.equal(first.has('location_unsupported'), false);
		first.add('location_unsupported');
		const restarted = new OfflineNoticeStore(path);
		assert.equal(restarted.has('location_unsupported'), true, 'the restart would announce it again');
		assert.equal(restarted.has('auth_invalid'), false, 'another category is still owed its notice');
	});

	it('recovery clears it, so the next outage is announced', () => {
		const path = fresh();
		new OfflineNoticeStore(path).add('location_unsupported').add('quota_exceeded');
		const recovered = new OfflineNoticeStore(path);
		assert.equal(recovered.size, 2);
		assert.deepEqual([...recovered].sort(), ['location_unsupported', 'quota_exceeded']);
		recovered.clear();
		assert.equal(recovered.size, 0);
		assert.equal(new OfflineNoticeStore(path).has('location_unsupported'), false,
			'a later process must announce the next outage');
	});

	it('another process adding a category is seen without a restart', () => {
		const path = fresh();
		const a = new OfflineNoticeStore(path);
		const b = new OfflineNoticeStore(path);
		b.add('credits_depleted');
		assert.equal(a.has('credits_depleted'), true);
	});
});

describe('the record over time and across processes', () => {
	it('a notice lapses after a day, so a long outage is announced once a day', () => {
		const path = fresh();
		let now = 1_000_000;
		new OfflineNoticeStore(path, () => now).add('location_unsupported');
		now += OFFLINE_NOTICE_TTL_S - 1;
		assert.equal(new OfflineNoticeStore(path, () => now).has('location_unsupported'), true);
		now += 2;
		assert.equal(new OfflineNoticeStore(path, () => now).has('location_unsupported'), false,
			'a notice that never lapses would silence the next outage of that kind forever');
	});

	it('each category keeps the time it was first announced', () => {
		const path = fresh();
		let now = 1_000_000;
		const store = new OfflineNoticeStore(path, () => now);
		store.add('location_unsupported');
		now += 600;
		store.add('quota_exceeded');
		store.add('location_unsupported');
		const recorded = JSON.parse(readFileSync(path, 'utf8')).categories;
		assert.deepEqual(recorded, { location_unsupported: 1_000_000, quota_exceeded: 1_000_600 });
	});

	it('a clear another process wrote is seen here', () => {
		const path = fresh();
		const a = new OfflineNoticeStore(path);
		a.add('location_unsupported');
		new OfflineNoticeStore(path).clear();
		assert.equal(a.has('location_unsupported'), false);
	});
});

describe('it fails soft', () => {
	it('an unreadable record is an empty one, and a new add works', () => {
		const path = fresh();
		mkdirSync(join(path, '..'), { recursive: true });
		writeFileSync(path, '{not json');
		const store = new OfflineNoticeStore(path);
		assert.equal(store.size, 0);
		store.add('location_unsupported');
		assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')).categories.location_unsupported > 0, true);
	});

	it('a record of the wrong shape names nothing', () => {
		const path = fresh();
		mkdirSync(join(path, '..'), { recursive: true });
		writeFileSync(path, JSON.stringify({ categories: 'location_unsupported' }));
		assert.equal(new OfflineNoticeStore(path).has('location_unsupported'), false);
		writeFileSync(path, 'null');
		assert.equal(new OfflineNoticeStore(path).size, 0);
	});

	const posixNonRoot = { skip: process.platform === 'win32' || process.getuid?.() === 0 };

	it('a recovery that cannot be saved is not undone by the stale file', posixNonRoot, () => {
		const path = fresh();
		const store = new OfflineNoticeStore(path);
		store.add('location_unsupported');
		chmodSync(join(path, '..'), 0o555);
		try {
			store.clear();
			assert.equal(store.size, 0, 'the old file put the category back');
			assert.equal(store.has('location_unsupported'), false);
			store.add('auth_invalid');
			assert.deepEqual([...store], ['auth_invalid'], 'this process holds the set until a write succeeds');
		} finally {
			chmodSync(join(path, '..'), 0o755);
		}
		store.add('quota_exceeded');
		assert.deepEqual(Object.keys(JSON.parse(readFileSync(path, 'utf8')).categories).sort(),
			['auth_invalid', 'quota_exceeded'], 'the next successful write records what this process held');
	});

	it('a record that cannot be written still holds for this process', posixNonRoot, () => {
		const dir = mkdtempSync(join(tmpdir(), 'voice-offline-notice-ro-'));
		chmodSync(dir, 0o555);
		try {
			const store = new OfflineNoticeStore(join(dir, 'state', 'voice-offline-notified.json'));
			store.add('location_unsupported');
			assert.equal(store.has('location_unsupported'), true);
			assert.equal(existsSync(join(dir, 'state')), false);
		} finally {
			chmodSync(dir, 0o755);
		}
	});
});
