import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildWorkTool } from '../src/voice-host.js';

test('wearable work declares inline delegation acknowledgement on either route', () => {
	for (const route of [{ kind: 'dir' }, { kind: 'gateway', agentId: 'test-agent' }] as const) {
		const tool = buildWorkTool({ deviceId: 'test-device' }, route);
		assert.equal(tool.name, 'work');
		assert.equal(tool.execution, 'inline');
		assert.deepEqual(tool.parameters.parse({ task: 'Check the build' }), { task: 'Check the build' });
		assert.equal(tool.parameters.safeParse({}).success, false);
	}
});
