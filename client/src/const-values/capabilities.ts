/**
 * Static "What I can do" capability list shown on the idle conversation
 * screen. Ported from the legacy web-client capabilities panel (#826,
 * closes #675 v1). Owner-voice / terse. SKILL.md auto-generation, dynamic
 * chips, and the first-run localStorage card are deferred to v2.
 */

export interface Capability {
	label: string;
	desc: string;
}

export const CAPABILITIES_HEADING = 'What I can do';

export const CAPABILITIES: readonly Capability[] = [
	{ label: 'Voice + screen', desc: 'control any Mac app by voice; read what is on screen' },
	{ label: 'Comms', desc: 'phone, iMessage, Telegram, Discord' },
	{ label: 'Calendar + email', desc: 'Gmail / Google Calendar; draft replies; book meetings' },
	{ label: 'Autonomous work', desc: 'ships code, summarizes news, runs benchmarks' },
	{ label: 'Fleet', desc: 'coordinates with other Sutandos under access tiers' },
	{ label: 'Skills', desc: 'ask "list skills" to see everything installed' },
];
