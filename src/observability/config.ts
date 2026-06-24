/**
 * Narrow config slice for the observability + metering spine.
 *
 * This loader resolves ONLY the `observability`, `metering`, and `tenant`
 * blocks — it is deliberately NOT the full Spine-C config loader (voice /
 * telephony / stt / tts / send / services registry). Those are a separate,
 * later concern; this slice ships the minimum the obs/meter seam needs.
 *
 * Resolution order (each layer overlays the previous, field by field):
 *   1. in-code OBSERVABILITY_DEFAULTS — the floor
 *   2. environment knobs — SUTANDO_TENANT_ID / SUTANDO_TENANT_MODE /
 *      SUTANDO_METERING_ENABLED / SUTANDO_METERING_ENDPOINT / SUTANDO_METERING_HEADERS
 *   3. workspace override — `<workspace>/config/observability.json` (machine-local; wins)
 *
 * Mirrors the `loadVoiceConfig` idiom (src/voice-config.ts): overlay over
 * defaults, handle nested blocks explicitly, warn and fall back on a parse
 * error. Twin of observability-config.py.
 */

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from '../workspace_default.js';

export interface SinkConfig {
	type: string;
	path?: string;
	endpoint?: string;
	headers?: Record<string, string>;
	[k: string]: unknown;
}

export interface ObservabilitySection {
	sinks: SinkConfig[];
	sampling: { trace: number };
}

export interface MeteringSection {
	enabled: boolean;
	endpoint: string | null;
	batchMax: number;
	/** Extra HTTP headers sent on every usage-forward POST — e.g. an auth token
	 *  (`{ "Authorization": "Bearer …" }`) or a shared secret for the receiver.
	 *  Static; for a rotating token, inject a headersProvider on the forwarder. */
	headers?: Record<string, string>;
}

export interface TenantSection {
	id: string | null;
	mode: 'byok' | 'managed';
}

export interface ObservabilityConfig {
	observability: ObservabilitySection;
	metering: MeteringSection;
	tenant: TenantSection;
}

/** In-code defaults — the floor that the env knobs and workspace override layer
 *  onto. */
export const OBSERVABILITY_DEFAULTS: ObservabilityConfig = {
	observability: { sinks: [{ type: 'jsonl-file' }], sampling: { trace: 1.0 } },
	metering: { enabled: false, endpoint: null, batchMax: 100 },
	tenant: { id: null, mode: 'byok' },
};

function isTrueish(v: string): boolean {
	return ['1', 'true', 'yes', 'on'].includes(v.trim().toLowerCase());
}

/** Overlay the three blocks of `raw` onto `base`, key by key. Keys absent from
 *  `raw` keep their `base` value (so each layer is a true partial override).
 *  The sinks array is taken verbatim when present. */
function overlay(base: ObservabilityConfig, raw: Record<string, unknown>): ObservabilityConfig {
	const obs = (raw.observability ?? {}) as Partial<ObservabilitySection>;
	const meter = (raw.metering ?? {}) as Partial<MeteringSection>;
	const tenant = (raw.tenant ?? {}) as Partial<TenantSection>;
	return {
		observability: {
			sinks: Array.isArray(obs.sinks) ? (obs.sinks as SinkConfig[]) : base.observability.sinks,
			sampling: { trace: obs.sampling?.trace ?? base.observability.sampling.trace },
		},
		metering: {
			enabled: meter.enabled ?? base.metering.enabled,
			endpoint: meter.endpoint ?? base.metering.endpoint,
			batchMax: meter.batchMax ?? base.metering.batchMax,
			headers: meter.headers ?? base.metering.headers,
		},
		tenant: {
			id: tenant.id ?? base.tenant.id,
			mode: tenant.mode === 'managed' ? 'managed' : tenant.mode === 'byok' ? 'byok' : base.tenant.mode,
		},
	};
}

function readJsonFile(path: string): Record<string, unknown> | null {
	if (!existsSync(path)) return null;
	try {
		return JSON.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>;
	} catch (e) {
		console.warn(`[observability-config] failed to parse ${path}, ignoring: ${(e as Error).message}`);
		return null;
	}
}

export function loadObservabilityConfig(opts?: { workspace?: string }): ObservabilityConfig {
	// 1. in-code defaults (the floor)
	let cfg = structuredClone(OBSERVABILITY_DEFAULTS);

	// 2. environment knobs
	const tenantId = process.env.SUTANDO_TENANT_ID?.trim();
	if (tenantId) cfg.tenant.id = tenantId;
	const tenantMode = process.env.SUTANDO_TENANT_MODE?.trim();
	if (tenantMode) cfg.tenant.mode = tenantMode === 'managed' ? 'managed' : 'byok';
	const metEnabled = process.env.SUTANDO_METERING_ENABLED?.trim();
	if (metEnabled !== undefined && metEnabled !== '') cfg.metering.enabled = isTrueish(metEnabled);
	const metEndpoint = process.env.SUTANDO_METERING_ENDPOINT?.trim();
	if (metEndpoint) cfg.metering.endpoint = metEndpoint;
	const metHeaders = process.env.SUTANDO_METERING_HEADERS?.trim();
	if (metHeaders) {
		try {
			const parsed = JSON.parse(metHeaders) as Record<string, string>;
			if (parsed && typeof parsed === 'object') cfg.metering.headers = parsed;
		} catch {
			console.warn('[observability-config] SUTANDO_METERING_HEADERS is not valid JSON, ignoring');
		}
	}

	// 3. workspace override (machine-local; wins, per the documented order)
	const ws = opts?.workspace ?? resolveWorkspace();
	const overrideRaw = readJsonFile(join(ws, 'config', 'observability.json'));
	if (overrideRaw) cfg = overlay(cfg, overrideRaw);

	return cfg;
}
