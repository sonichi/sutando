import { afterEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import type { AddressInfo } from 'node:net';
import type { Server } from 'node:http';
import { Collector } from '../../src/observability/collector/collector.js';
import { serveCollector } from '../../src/observability/collector/server.js';

let server: Server | undefined;

afterEach(
	() =>
		new Promise<void>((resolve) => {
			if (!server) return resolve();
			server.close(() => resolve());
			server = undefined;
		}),
);

describe('collector server identity', () => {
	it('identifies itself on /health so startup rejects foreign listeners', async () => {
		server = serveCollector(new Collector(), { port: 0 });
		await new Promise<void>((resolve) => server?.once('listening', resolve));
		const { port } = server.address() as AddressInfo;
		const response = await fetch(`http://127.0.0.1:${port}/health`);
		assert.equal(response.status, 200);
		assert.deepEqual(await response.json(), {
			ok: true,
			service: 'sutando-observability-collector',
			sources: [],
			ingested: 0,
		});
	});
});
