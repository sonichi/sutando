/**
 * The Claude Code OTLP/HTTP metrics INGEST CONTRACT — the wire shape Claude
 * Code's native OpenTelemetry exporter POSTs to `/v1/metrics`, plus the decoder
 * that flattens it into the `OtelRecord`s the (already-tested) `otelMap`
 * consumes.
 *
 * This is metering's "detect → strictly type" boundary, mirroring `cc-hooks.ts`
 * for hooks. Hooks carry NO tokens; CC's OTel `claude_code.token.usage` /
 * `claude_code.cost.usage` metrics are the authoritative usage source, so this
 * is what turns the obs-only stream into real `claude.tokens` / `claude.cost`
 * ledger records.
 *
 * We model only the OTLP/JSON subset CC actually emits for metrics (Sum + Gauge
 * number data points). Each datapoint becomes one `OtelMetricRecord`; OTLP
 * attribute arrays (`[{key, value:{stringValue|intValue|...}}]`) are flattened
 * to the plain `{type, model, request_id, ...}` / resource shape `otelMap`
 * expects. Logs/traces are intentionally NOT decoded here — start-cli enables
 * ONLY metric export, so hooks stay the sole obs source (no duplicate events).
 */

import type { OtelRecord, ResourceAttrs } from './cc-records.js';

interface OtlpAnyValue {
	stringValue?: string;
	intValue?: string | number;
	doubleValue?: number;
	boolValue?: boolean;
}

interface OtlpKeyValue {
	key?: string;
	value?: OtlpAnyValue;
}

interface OtlpNumberDataPoint {
	timeUnixNano?: string | number;
	asInt?: string | number;
	asDouble?: number;
	attributes?: OtlpKeyValue[];
}

interface OtlpMetric {
	name?: string;
	sum?: { dataPoints?: OtlpNumberDataPoint[] };
	gauge?: { dataPoints?: OtlpNumberDataPoint[] };
}

interface OtlpResourceMetrics {
	resource?: { attributes?: OtlpKeyValue[] };
	scopeMetrics?: Array<{ metrics?: OtlpMetric[] }>;
}

interface OtlpExportMetricsRequest {
	resourceMetrics?: OtlpResourceMetrics[];
}

/** OTLP scalars are tagged unions; ints arrive as strings (JSON can't hold int64). */
function anyValue(v?: OtlpAnyValue): unknown {
	if (!v) return undefined;
	if (v.stringValue !== undefined) return v.stringValue;
	if (v.intValue !== undefined) return typeof v.intValue === 'string' ? Number(v.intValue) : v.intValue;
	if (v.doubleValue !== undefined) return v.doubleValue;
	if (v.boolValue !== undefined) return v.boolValue;
	return undefined;
}

function attrsToObject(kvs?: OtlpKeyValue[]): Record<string, unknown> {
	const o: Record<string, unknown> = {};
	for (const kv of kvs ?? []) if (kv && typeof kv.key === 'string') o[kv.key] = anyValue(kv.value);
	return o;
}

function dpValue(dp: OtlpNumberDataPoint): number {
	if (typeof dp.asDouble === 'number') return dp.asDouble;
	if (dp.asInt !== undefined) return typeof dp.asInt === 'string' ? Number(dp.asInt) : dp.asInt;
	return 0;
}

function dpTs(dp: OtlpNumberDataPoint): number | undefined {
	if (dp.timeUnixNano === undefined) return undefined;
	const n = typeof dp.timeUnixNano === 'string' ? Number(dp.timeUnixNano) : dp.timeUnixNano;
	return Number.isFinite(n) ? n / 1e9 : undefined; // nanoseconds → unix seconds
}

/**
 * Detect + flatten an OTLP/HTTP metrics export body into `OtelRecord`s (one per
 * datapoint). Returns `[]` for anything that isn't an OTLP metrics request, so
 * the collector drops non-metrics bodies. The boundary cast lives here, after
 * the `resourceMetrics` shape check.
 */
export function decodeOtlpMetrics(payload: unknown): OtelRecord[] {
	if (payload === null || typeof payload !== 'object') return [];
	const req = payload as OtlpExportMetricsRequest;
	if (!Array.isArray(req.resourceMetrics)) return [];

	const out: OtelRecord[] = [];
	for (const rm of req.resourceMetrics) {
		const resource = attrsToObject(rm.resource?.attributes) as ResourceAttrs;
		for (const sm of rm.scopeMetrics ?? []) {
			for (const m of sm.metrics ?? []) {
				if (typeof m.name !== 'string') continue;
				const dps = m.sum?.dataPoints ?? m.gauge?.dataPoints ?? [];
				for (const dp of dps) {
					out.push({
						signal: 'metric',
						metric: { name: m.name, value: dpValue(dp), ts: dpTs(dp), attrs: attrsToObject(dp.attributes), resource },
					});
				}
			}
		}
	}
	return out;
}
