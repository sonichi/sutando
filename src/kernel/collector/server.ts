/**
 * HTTP shell for the Collector — the long-running local daemon.
 *
 *   POST /ingest/<source>   raw payload for the normalizer registered under
 *                           <source>. Body is a single payload or an array of
 *                           them. (The CC shell hook posts here.)
 *   POST /ingest            ALREADY-normalized ObsEvent | UsageRecord (or an
 *                           array, or {events,usage}) — for in-process emitters
 *                           that map locally but ship here for durable store +
 *                           forward.
 *   GET  /health            { ok, sources, ingested }
 *
 * Source-agnostic: it knows nothing about Claude Code. The composition root
 * (`src/boot/collector.ts`) builds the Collector, registers the available
 * normalizers, and calls `serveCollector()`. Never fails an emitter — a bad body
 * or a normalizer miss is swallowed (204), because the emitter (a tool hook) must
 * not block or error on the agent's hot path.
 */

import { createServer, type Server } from 'node:http';
import type { Collector } from './collector.js';
import type { ObsEvent } from '../obs/types.js';
import type { UsageRecord } from '../meter/types.js';

const MAX_BODY = 4_000_000;

function looksLikeUsage(o: unknown): o is UsageRecord {
	return (
		!!o &&
		typeof o === 'object' &&
		typeof (o as Record<string, unknown>).usage_id === 'string' &&
		typeof (o as Record<string, unknown>).meter === 'string'
	);
}

/** Split a generic-ingest body into formed events vs usage records. Accepts a
 *  single object, an array, or an `{events,usage}` envelope. */
function splitFormed(parsed: unknown): { events: ObsEvent[]; usage: UsageRecord[] } {
	const events: ObsEvent[] = [];
	const usage: UsageRecord[] = [];
	const env = parsed as { events?: unknown[]; usage?: unknown[] };
	const items =
		Array.isArray(env?.events) || Array.isArray(env?.usage)
			? [...(env.events ?? []), ...(env.usage ?? [])]
			: Array.isArray(parsed)
				? parsed
				: [parsed];
	for (const o of items) (looksLikeUsage(o) ? usage : events).push(o as never);
	return { events, usage };
}

export function serveCollector(collector: Collector, opts?: { port?: number }): Server {
	let ingested = 0;
	const port = opts?.port ?? (Number(process.env.SUTANDO_OBS_PORT) || 4000);

	const server = createServer((req, res) => {
		const url = req.url ?? '/';

		if (req.method === 'POST' && url.startsWith('/ingest')) {
			let body = '';
			req.on('data', (c) => {
				body += c;
				if (body.length > MAX_BODY) req.destroy();
			});
			req.on('end', () => {
				let parsed: unknown;
				try {
					parsed = JSON.parse(body);
				} catch {
					res.writeHead(400, { 'content-type': 'text/plain' }).end('bad json');
					return;
				}
				try {
					const m = url.match(/^\/ingest\/([^/?]+)/);
					if (m) {
						const source = decodeURIComponent(m[1]);
						for (const p of Array.isArray(parsed) ? parsed : [parsed]) {
							const stat = collector.ingest(source, p);
							ingested += stat.events + stat.usage;
						}
					} else {
						const { events, usage } = splitFormed(parsed);
						collector.accept({ events, usage });
						ingested += events.length + usage.length;
					}
				} catch {
					/* never fail the emitter on a mapping/write error */
				}
				res.writeHead(204).end();
			});
			return;
		}

		if (url.startsWith('/health')) {
			res
				.writeHead(200, { 'content-type': 'application/json' })
				.end(JSON.stringify({ ok: true, sources: collector.sources(), ingested }));
			return;
		}

		res.writeHead(404).end();
	});

	server.listen(port);
	return server;
}
