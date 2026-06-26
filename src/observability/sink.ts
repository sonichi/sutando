/**
 * Observability sinks. A `Sink` accepts a finished `ObsEvent` and ships it
 * somewhere; it MUST be best-effort (never throw out — `emit()` isolates each
 * sink, but a sink that swallows its own errors keeps the others clean).
 *
 * The `Sink` interface stays in `src/observability` permanently. The concrete
 * `JsonlFileSink` body is adapted from `src/event_log.py` (crash-safe O_APPEND,
 * single write per line, daily local-date file). In a later phase the concrete
 * sinks may move to their own file; the interface does not move. The
 * `otlp-http` sink is NOT built here — only the interface it will implement.
 *
 * Twin of sink.py.
 */

import { appendFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from '../workspace_default.js';
import type { ObsEvent } from './events.js';
import type { SinkConfig } from './config.js';

export interface Sink {
	readonly type: string;
	write(ev: ObsEvent): void; // best-effort; must not throw out
}

function dailyFilePath(dir: string, atMs: number): string {
	const d = new Date(atMs);
	const y = d.getFullYear();
	const m = String(d.getMonth() + 1).padStart(2, '0');
	const day = String(d.getDate()).padStart(2, '0');
	return join(dir, `events-${y}-${m}-${day}.jsonl`);
}

/** Append-only JSONL sink: `<dir>/events-YYYY-MM-DD.jsonl` (default dir
 *  `<workspace>/logs`). One compact JSON object per line, atomic O_APPEND. */
export class JsonlFileSink implements Sink {
	readonly type = 'jsonl-file';
	private readonly dir: string;

	constructor(opts?: { dir?: string }) {
		this.dir = opts?.dir ?? join(resolveWorkspace(), 'logs');
	}

	write(ev: ObsEvent): void {
		try {
			mkdirSync(this.dir, { recursive: true });
			const line = JSON.stringify(ev) + '\n';
			const path = dailyFilePath(this.dir, ev.ts * 1000);
			appendFileSync(path, line, { flag: 'a' });
		} catch (e) {
			try {
				process.stderr.write(`[obs] jsonl-file sink failed to write ${ev.kind}: ${(e as Error).message}\n`);
			} catch {
				/* even the warn is best-effort */
			}
		}
	}
}

/** Build a concrete sink from a config entry. Returns null for an unsupported
 *  type (e.g. otlp-http, not built yet) with a one-line warn. */
export function sinkFromConfig(cfg: SinkConfig): Sink | null {
	switch (cfg.type) {
		case 'jsonl-file':
			return new JsonlFileSink({ dir: typeof cfg.path === 'string' ? cfg.path : undefined });
		default:
			try {
				process.stderr.write(`[obs] sink type '${cfg.type}' not supported yet, skipping\n`);
			} catch {
				/* best-effort */
			}
			return null;
	}
}
