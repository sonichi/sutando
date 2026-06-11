/**
 * Collector daemon — the composition root for Sutando's local collector.
 *
 * Builds the ONE source-agnostic kernel `Collector`, registers every available
 * source `Normalizer` (Claude Code hooks today; voice-agent, filewatcher, and
 * bridges next — same collector, just more `.register(...)` lines), and serves
 * it over HTTP. There is NOT a per-source collector: this single process is the
 * local floor for all telemetry, normalizing heterogeneous sources into one
 * schema and (once a forward sink is configured) forwarding upstream.
 *
 * This file is the only place that knows about both the kernel collector and the
 * concrete source adapters — the kernel never imports an adapter, the adapters
 * never start a server. Wiring lives here.
 *
 *   SUTANDO_WORKSPACE=<dir> SUTANDO_OBS_PORT=4000 \
 *     tsx src/boot/collector.ts
 */

import { Collector } from '../kernel/collector/collector.js';
import { serveCollector } from '../kernel/collector/server.js';
import { ClaudeCodeHookNormalizer } from '../adapters/executor/claude-code/sources/hook-normalizer.js';
import { ClaudeCodeOtelNormalizer, CC_OTEL_SOURCE } from '../adapters/executor/claude-code/sources/otel-normalizer.js';

const collector = new Collector()
	.register(new ClaudeCodeHookNormalizer()) // obs events  (hooks → /ingest/claude-code-hooks)
	.register(new ClaudeCodeOtelNormalizer()); // token+cost metering (OTLP → /v1/metrics)
// Next sources plug in the SAME collector — one ingestion point, many normalizers:
//   .register(new VoiceAgentNormalizer())
//   .register(new FileWatcherNormalizer())

const port = Number(process.env.SUTANDO_OBS_PORT) || 4000;
// /v1/metrics → the CC OTel normalizer (OTLP is a standard protocol; the binding
// to a normalizer is composition, not kernel policy).
serveCollector(collector, { port, otlpSource: CC_OTEL_SOURCE });

const ws = process.env.SUTANDO_WORKSPACE ?? '~/.sutando/workspace';
console.log('collector — one local ingestion point for all sources');
console.log(`  sources:   ${collector.sources().join(', ') || '(none registered)'}`);
console.log(`  listening: http://localhost:${port}/ingest/<source>  ·  OTLP http://localhost:${port}/v1/metrics`);
console.log(`  writing:   ${ws}/logs/events-*.jsonl  +  ${ws}/data/usage/usage-*.jsonl  (metering ledger)`);
