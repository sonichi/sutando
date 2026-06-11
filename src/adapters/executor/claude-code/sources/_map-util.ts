/** Shared helpers for the Claude Code mappers. Pure. */

import type { ObsEvent, Outcome } from '../../../../kernel/obs/types.js';
import type { MapContext, ResourceAttrs } from './cc-records.js';
import { actorFromResourceAttrs, traceIdFromSession } from './ids.js';

export const CORE_SOURCE = 'core-cli';

/** Drop keys whose value is `undefined` so mapper output deep-equals clean
 *  golden objects (and serializes identically). */
export function clean<T extends Record<string, unknown>>(o: T): T {
	const out: Record<string, unknown> = {};
	for (const [k, v] of Object.entries(o)) if (v !== undefined) out[k] = v;
	return out as T;
}

export function num(v: unknown): number | undefined {
	return typeof v === 'number' ? v : undefined;
}

export function tsOf(recTs: number | undefined, ctx: MapContext): number {
	return typeof recTs === 'number' ? recTs : ctx.receivedAt;
}

export function tsBucket(ts: number): number {
	return Math.floor(ts);
}

export function baseEvent(opts: {
	res?: ResourceAttrs;
	sessionId?: string;
	kind: string;
	outcome: Outcome;
	ctx: MapContext;
	ts: number;
}): ObsEvent {
	const sessionId = opts.sessionId ?? opts.res?.['session.id'];
	return {
		schema: 1,
		ts: opts.ts,
		trace_id: traceIdFromSession(sessionId),
		node: opts.ctx.node,
		source: CORE_SOURCE,
		actor: actorFromResourceAttrs(opts.res),
		kind: opts.kind,
		outcome: opts.outcome,
	};
}

/** Map a file-touching tool to its paired `file.*` event kind. */
export function fileOpFor(toolName?: string): { kind: 'file.read' | 'file.change'; op?: string } | null {
	switch (toolName) {
		case 'Read':
			return { kind: 'file.read' };
		case 'Write':
			return { kind: 'file.change', op: 'written' };
		case 'Edit':
		case 'MultiEdit':
			return { kind: 'file.change', op: 'modified' };
		default:
			return null;
	}
}

export function pathFromToolInput(input?: Record<string, unknown>): string | undefined {
	if (!input) return undefined;
	return (
		(typeof input.file_path === 'string' ? input.file_path : undefined) ??
		(typeof input.path === 'string' ? input.path : undefined) ??
		(typeof input.notebook_path === 'string' ? input.notebook_path : undefined)
	);
}
