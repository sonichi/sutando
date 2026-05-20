/**
 * Typed fetch wrappers for the Sutando conversation HTTP API
 * (src/web-server.ts). One module so every page hits the API through
 * the same code path — components never call `fetch` directly per
 * CLAUDE.md § Frontend Conventions ("no fetch calls in components").
 */

import { resolveConfig } from './config';

export type AgentState = 'idle' | 'listening' | 'speaking' | 'working' | 'seeing';

const apiUrl = (path: string): string => {
	const { apiOrigin } = resolveConfig();
	return `${apiOrigin}${path}`;
};

export async function fetchVoiceMode(signal?: AbortSignal): Promise<{ mode: 'active' | 'meeting' }> {
	const res = await fetch(apiUrl('/voice-mode'), { signal });
	if (!res.ok) throw new Error(`/voice-mode returned ${res.status}`);
	return (await res.json()) as { mode: 'active' | 'meeting' };
}

/**
 * Report mute / voice / agent-state to the conversation server.
 * Mirrors the legacy `fetch('/mute-state?…')` plumbing — the menu-bar
 * app reads from the same endpoint to draw the recording indicator.
 */
export async function postMuteState(patch: {
	muted?: boolean;
	voice?: boolean;
	state?: string;
}): Promise<void> {
	const qs = new URLSearchParams();
	if (patch.muted !== undefined) qs.set('muted', String(patch.muted));
	if (patch.voice !== undefined) qs.set('voice', String(patch.voice));
	if (patch.state !== undefined) qs.set('state', patch.state);
	await fetch(apiUrl(`/mute-state?${qs.toString()}`)).catch(() => {});
}

export interface SlackSettingsStatus {
	botConfigured: boolean;
	appConfigured: boolean;
}

/**
 * Report whether the Slack bridge tokens are already on disk
 * (`~/.claude/channels/slack/.env`). The endpoint never echoes the token
 * values — only a configured/not-configured flag per token.
 */
export async function fetchSlackSettings(signal?: AbortSignal): Promise<SlackSettingsStatus> {
	const res = await fetch(apiUrl('/settings/slack'), { signal });
	if (!res.ok) throw new Error(`/settings/slack returned ${res.status}`);
	return (await res.json()) as SlackSettingsStatus;
}

/**
 * Persist Slack bridge credentials. The server validates the `xoxb-` /
 * `xapp-` prefixes and writes `~/.claude/channels/slack/.env` mode 0600.
 */
export async function saveSlackSettings(botToken: string, appToken: string): Promise<void> {
	const res = await fetch(apiUrl('/settings/slack'), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ botToken, appToken }),
	});
	if (!res.ok) {
		const detail = (await res.json().catch(() => null)) as { error?: string } | null;
		throw new Error(detail?.error ?? `/settings/slack returned ${res.status}`);
	}
}

export interface StandIdentity {
	name?: string;
	nameOrigin?: string;
	avatarGenerated?: boolean;
	avatarUrl?: string;
}

/**
 * Fetch the persistent identity (custom name + generated avatar URL) the
 * `agent-universe` dashboard owns on port 7844. Failure-safe — the legacy
 * `.catch(()=>{})` style was load-bearing because the dashboard server is
 * optional. Returns null when the endpoint is unreachable.
 */
export async function fetchStandIdentity(signal?: AbortSignal): Promise<StandIdentity | null> {
	const host = window.location.hostname || 'localhost';
	const url = `http://${host}:7844/stand-identity`;
	try {
		const res = await fetch(url, { signal });
		if (!res.ok) return null;
		return (await res.json()) as StandIdentity;
	} catch {
		return null;
	}
}
