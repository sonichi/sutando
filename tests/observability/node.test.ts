import { describe, it, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { hostname } from 'node:os';
import { nodeId, resetNodeId } from '../../src/observability/node.js';

afterEach(() => {
	delete process.env.SUTANDO_NODE_ID;
	resetNodeId();
});

describe('kernel/_shared/node', () => {
	it('honors the SUTANDO_NODE_ID override', () => {
		process.env.SUTANDO_NODE_ID = 'mac-studio-test';
		resetNodeId();
		assert.equal(nodeId(), 'mac-studio-test');
	});

	it('falls back to the short hostname', () => {
		delete process.env.SUTANDO_NODE_ID;
		resetNodeId();
		assert.equal(nodeId(), hostname().split('.')[0] || 'unknown');
	});

	it('caches within a process until reset', () => {
		process.env.SUTANDO_NODE_ID = 'first';
		resetNodeId();
		assert.equal(nodeId(), 'first');
		process.env.SUTANDO_NODE_ID = 'second'; // no reset → cached value holds
		assert.equal(nodeId(), 'first');
	});
});
