/**
 * Collector daemon — the composition root for Sutando's local collector.
 *
 * Builds the ONE source-agnostic `Collector`, registers every available
 * source `Normalizer` (Claude Code hooks today; voice-agent, filewatcher, and
 * bridges next — same collector, just more `.register(...)` lines), and serves
 * it over HTTP. There is NOT a per-source collector: this single process is the
 * local floor for all telemetry, normalizing heterogeneous sources into one
 * schema and (once a forward sink is configured) forwarding upstream.
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
serveCollector(collector, { port, otlpSource: CC_OTEL_SOURCE });

const ws = resolveWorkspace();
console.log('collector — one local ingestion point for all sources');
console.log(`  sources:   ${collector.sources().join(', ') || '(none registered)'}`);
console.log(`  listening: http://localhost:${port}/ingest/<source>  ·  OTLP http://localhost:${port}/v1/metrics`);
console.log(`  writing:   ${ws}/logs/events-*.jsonl  +  ${ws}/data/usage/usage-*.jsonl  (metering ledger)`);
