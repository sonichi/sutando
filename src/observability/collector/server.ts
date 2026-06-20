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
 *   POST /v1/metrics        OTLP/HTTP metrics — routed to the `otlpSource`
 *                           normalizer (CC's native OTel exporter posts here).
 *                           /v1/logs and /v1/traces are accepted + dropped so
 *                           the exporter never sees an error.
 *   GET  /health            { ok, sources, ingested }
 *
 * Protocol-aware but normalizer-agnostic: the `/v1/*` paths are the OTLP
 * standard (not Claude-specific); WHICH normalizer they route to is injected by
 * the composition root (`src/boot/collector.ts`) via `otlpSource`. Never fails an
 * emitter — a bad body or a normalizer miss is swallowed, because the emitter (a
 * tool hook / the OTel SDK) must not block or error on the agent's hot path.
 */

import { createServer, type Server } from 'node:http';
import type { Collector } from './collector.js';
import type { ObsEvent } from '../events.js';
import type { UsageRecord } from '../usage.js';

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

/** Read a capped request body, then hand the parsed JSON (or null on bad JSON)
 *  plus the raw byte length to `done`. */
function readJsonBody(req: import('node:http').IncomingMessage, done: (parsed: unknown | null, bytes: number) => void): void {
	let body = '';
	req.on('data', (c) => {
		body += c;
		if (body.length > MAX_BODY) req.destroy();
	});
	req.on('end', () => {
		try {
			done(JSON.parse(body), body.length);
		} catch {
			done(null, body.length);
		}
	});
}

export function serveCollector(collector: Collector, opts?: { port?: number; host?: string; otlpSource?: string }): Server {
	let ingested = 0;
	const port = opts?.port ?? (Number(process.env.SUTANDO_OBS_PORT) || 4000);
	// Localhost-by-default, opt into LAN exposure explicitly — same env-override
	// shape as DASHBOARD_BIND / AGENT_API_BIND. The collector carries full prompt
	// text + tool inputs and has no auth, so it must not bind 0.0.0.0 by default.
	const host = opts?.host ?? process.env.SUTANDO_OBS_BIND ?? '127.0.0.1';
	const otlpSource = opts?.otlpSource;

	const server = createServer((req, res) => {
		const url = req.url ?? '/';

		if (req.method === 'POST' && url.startsWith('/ingest')) {
			readJsonBody(req, (parsed) => {
				if (parsed === null) {
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

		// OTLP/HTTP standard paths. /v1/metrics → the configured otlpSource;
		// /v1/logs and /v1/traces are accepted + dropped (we enable only metric
		// export). Always reply 200 {} so the OTel SDK exporter sees success.
		if (req.method === 'POST' && url.startsWith('/v1/')) {
			const ctype = String(req.headers['content-type'] ?? '');
			readJsonBody(req, (parsed, bytes) => {
				try {
					if (url.startsWith('/v1/metrics') && otlpSource) {
						if (parsed === null) {
							// almost always means the exporter is sending protobuf, which
							// this JSON decoder can't read — the #1 "no metering" cause.
							process.stderr.write(
								`[collector] /v1/metrics: ${bytes}B body not JSON (content-type: ${ctype || 'none'}); ` +
									`set OTEL_EXPORTER_OTLP_PROTOCOL=http/json on the core if the exporter sends protobuf\n`,
							);
						} else {
							const stat = collector.ingest(otlpSource, parsed);
							ingested += stat.events + stat.usage;
							process.stderr.write(`[collector] /v1/metrics: ${bytes}B → +${stat.usage} usage, +${stat.events} obs\n`);
						}
					}
				} catch {
					/* never fail the OTel exporter */
				}
				res.writeHead(200, { 'content-type': 'application/json' }).end('{}');
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

	server.listen(port, host);
	if (host !== '127.0.0.1' && host !== 'localhost') {
		process.stderr.write(
			`[collector] LAN exposure enabled via SUTANDO_OBS_BIND=${host} — the collector has NO ` +
				`authentication; anyone on this network can POST to /ingest and read every prompt + tool input it relays\n`,
		);
	}
	return server;
}
