/**
 * Collector daemon — the composition root for Sutando's local collector.
 *
 * Builds the ONE source-agnostic `Collector`, registers every available
 * source `Normalizer` (Claude Code hooks today; voice-agent, filewatcher, and
 * bridges next — same collector, just more `.register(...)` lines), and serves
 * it over HTTP. There is NOT a per-source collector: this single process is the
 * local floor for all telemetry, normalizing heterogeneous sources into one
 * schema and — when `metering.endpoint` is configured — forwarding the billable
 * usage records upstream (events forward via a configured `observability` sink).
 *
 * This file is the only place that knows about both the collector and the
 * concrete source normalizers — the collector never imports a source, the sources
 * never start a server. Wiring lives here.
 *
 *   SUTANDO_WORKSPACE=<dir> SUTANDO_OBS_PORT=4000 \
 *     tsx src/observability/boot.ts
 */

import { Collector } from './collector/collector.js';
import { serveCollector } from './collector/server.js';
import { resolveWorkspace } from '../workspace_default.js';
import { loadObservabilityConfig } from './config.js';
import { ClaudeCodeHookNormalizer } from './claude/hook-normalizer.js';
import { ClaudeCodeOtelNormalizer, CC_OTEL_SOURCE } from './claude/otel-normalizer.js';
import { RealtimeNormalizer } from './realtime-normalizer.js';

const collector = new Collector()
	.register(new ClaudeCodeHookNormalizer()) // obs events  (hooks → /ingest/claude-code-hooks)
	.register(new ClaudeCodeOtelNormalizer()) // token+cost metering (OTLP → /v1/metrics)
	.register(new RealtimeNormalizer()); // voice + phone seconds (voice-agent/phone → /ingest/realtime)
// Next sources plug in the SAME collector — one ingestion point, many normalizers:
//   .register(new FileWatcherNormalizer())

const port = Number(process.env.SUTANDO_OBS_PORT) || 4000;
// /v1/metrics → the CC OTel normalizer (OTLP is a standard protocol; the binding
// to a normalizer is composition, not collector policy).
const server = serveCollector(collector, { port, otlpSource: CC_OTEL_SOURCE });

const ws = resolveWorkspace();
const metering = loadObservabilityConfig().metering;
console.log('collector — one local ingestion point for all sources');
console.log(`  sources:   ${collector.sources().join(', ') || '(none registered)'}`);
console.log(`  listening: http://localhost:${port}/ingest/<source>  ·  OTLP http://localhost:${port}/v1/metrics`);
console.log(`  writing:   ${ws}/logs/events-*.jsonl  +  ${ws}/data/usage/usage-*.jsonl  (metering ledger)`);
const headerCount = Object.keys(metering.headers ?? {}).length;
console.log(
	metering.enabled && metering.endpoint
		? `  exporting: usage → ${metering.endpoint}  (metering.enabled${headerCount ? `, ${headerCount} custom header(s)` : ''})`
		: '  exporting: off  (set SUTANDO_METERING_ENABLED=1 + SUTANDO_METERING_ENDPOINT=<url> to forward usage upstream)',
);

// Flush the usage forwarder's final partial batch on shutdown.
for (const sig of ['SIGTERM', 'SIGINT'] as const) {
	process.once(sig, () => {
		void collector.stop().finally(() => {
			server.close();
			process.exit(0);
		});
	});
}
