import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	actorFromResourceAttrs,
	spanIdFromCcSpan,
	tenantFromResource,
	traceIdFromSession,
	usageIdFromRequest,
} from '../../../src/observability/claude/ids.js';

describe('cc-source id adoption', () => {
	it('one CC session = one trace', () => {
		assert.equal(traceIdFromSession('sess-9'), 'cc-sess:sess-9');
		assert.equal(traceIdFromSession(undefined), 'cc-sess:unknown');
	});

	it('span id pass-through, namespaced', () => {
		assert.equal(spanIdFromCcSpan('abc'), 'sp_abc');
		assert.equal(spanIdFromCcSpan(undefined), undefined);
	});

	it('usage_id keys on request_id, with type suffix and session fallback', () => {
		assert.equal(usageIdFromRequest('req_1', {}), 'cc:tok:req_1');
		assert.equal(usageIdFromRequest('req_1', {}, 'input'), 'cc:tok:req_1:input');
		assert.equal(usageIdFromRequest(undefined, { sessionId: 'sess-9', tsBucket: 1717900000 }), 'cc:tok:sess-9:1717900000');
	});

	it('actor + tenant from resource, degrading gracefully', () => {
		assert.deepEqual(actorFromResourceAttrs({ 'session.id': 's', 'user.id': 'u-42', 'organization.id': 'org-7' }), {
			user_id: 'u-42',
			channel: 'claude-code',
			access_tier: 'owner',
			tenant_id: 'org-7',
		});
		assert.deepEqual(actorFromResourceAttrs(undefined), { user_id: 'core', channel: 'claude-code', access_tier: 'owner', tenant_id: null });
		assert.equal(tenantFromResource({}), null);
	});
});
