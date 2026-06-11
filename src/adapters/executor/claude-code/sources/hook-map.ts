/**
 * `hookMap(payload, ctx)` — map one Claude Code hook payload to spine primitives.
 * Hooks are OBS-ONLY (lifecycle / tool decisions; no authoritative tokens). The
 * ~29-event tail is covered by a forward-compatible default branch. File-op tool
 * hooks (Read/Write/Edit) additionally emit a paired `file.*` event — this is
 * how core-cli file consumption is recorded without instrumenting the core. Pure.
 */

import type { ObsEvent, Outcome } from '../../../../kernel/obs/types.js';
import type { HookPayload, MapContext, MapResult } from './cc-records.js';
import { baseEvent, clean, fileOpFor, pathFromToolInput, tsOf } from './_map-util.js';

function snake(s: string): string {
	return s.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
}

export function hookMap(payload: HookPayload, ctx: MapContext): MapResult {
	const ts = tsOf(typeof payload.ts === 'number' ? payload.ts : undefined, ctx);
	const sessionId = payload.session_id;
	const common = { session_id: sessionId, cwd: payload.cwd, permission_mode: payload.permission_mode };
	const events: ObsEvent[] = [];

	const ev = (kind: string, outcome: Outcome, data: Record<string, unknown>): ObsEvent => {
		const e = baseEvent({ sessionId, kind, outcome, ctx, ts });
		e.data = clean({ ...common, ...data });
		return e;
	};

	const name = payload.hook_event_name;
	switch (name) {
		case 'PreToolUse':
			events.push(ev('tool.call', 'ok', { tool_name: payload.tool_name, tool_input: payload.tool_input }));
			break;
		case 'PostToolUse':
		case 'PostToolUseFailure': {
			const outcome: Outcome = name === 'PostToolUseFailure' ? 'error' : 'ok';
			events.push(
				ev('tool.result', outcome, {
					tool_name: payload.tool_name,
					tool_input: payload.tool_input,
					tool_output: payload.tool_output,
					error: payload.error,
				}),
			);
			const fo = fileOpFor(payload.tool_name);
			const path = pathFromToolInput(payload.tool_input);
			if (fo && path) {
				const fe = baseEvent({ sessionId, kind: fo.kind, outcome, ctx, ts });
				fe.source_file = path;
				fe.data = clean({ ...common, op: fo.op, tool_name: payload.tool_name });
				events.push(fe);
			}
			break;
		}
		case 'Stop':
			events.push(ev('cc.hook.stop', 'ok', {}));
			break;
		case 'SessionStart':
			events.push(ev('cc.hook.session_start', 'ok', { source: payload.source, model: payload.model }));
			break;
		case 'SessionEnd':
			events.push(ev('cc.hook.session_end', 'ok', { end_reason: payload.end_reason }));
			break;
		case 'PreCompact':
			events.push(ev('cc.hook.pre_compact', 'ok', { trigger: payload.trigger }));
			break;
		case 'Notification':
			events.push(ev('cc.hook.notification', 'ok', { notification_type: payload.notification_type }));
			break;
		case 'SubagentStart':
			events.push(ev('cc.hook.subagent_start', 'ok', { agent_type: payload.agent_type }));
			break;
		case 'SubagentStop':
			events.push(ev('cc.hook.subagent_stop', 'ok', { agent_type: payload.agent_type }));
			break;
		case 'TaskCreated':
			events.push(ev('cc.hook.task_created', 'ok', { task_id: payload.task_id, task_title: payload.task_title }));
			break;
		case 'TaskCompleted':
			events.push(ev('cc.hook.task_completed', 'ok', { task_id: payload.task_id }));
			break;
		default: {
			// forward-compatible: cc.hook.<snake(event)> carrying the payload minus
			// the common keys, so a not-yet-modeled hook still lands structured.
			const rest: Record<string, unknown> = {};
			for (const [k, v] of Object.entries(payload)) {
				if (['hook_event_name', 'session_id', 'transcript_path', 'cwd', 'permission_mode', 'ts'].includes(k)) continue;
				rest[k] = v;
			}
			events.push(ev('cc.hook.' + snake(name), 'ok', rest));
		}
	}

	return { events, usage: [] };
}
