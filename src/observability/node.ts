/**
 * Node (machine) identity for the observability + metering spine.
 *
 * Every emitted event/usage record carries `node` — which MACHINE produced it —
 * so a multi-core fleet's events are attributable per host.
 *
 * Resolution (twin of `node.py`, intentionally identical and decoupled):
 *   1. `SUTANDO_NODE_ID` env var (explicit override).
 *   2. short hostname (first dot-segment).
 *
 * Deliberately does NOT reach into the workspace/memory layer to read
 * `stand-identity.json` — that keeps this module free of the V1 hold-list
 * (`util_paths`) dependency. A deployment that wants the stand-identity machine
 * name as the node id has `runtime/boot` export `SUTANDO_NODE_ID` from it; the
 * the module just reads the env. Cached after first resolution.
 */

import { hostname } from 'node:os';

let cached: string | undefined;

export function nodeId(): string {
	if (cached !== undefined) return cached;
	const override = process.env.SUTANDO_NODE_ID?.trim();
	if (override) {
		cached = override;
		return cached;
	}
	cached = hostname().split('.')[0] || 'unknown';
	return cached;
}

/** Test hook — clears the cache so an env change takes effect. */
export function resetNodeId(): void {
	cached = undefined;
}
