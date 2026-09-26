// Which room a spoken name means, and which room a voice session is docked in.

export type RoomEntry = { id: string; name: string | null };

export const ROOM_ID_RE = /^![^\s:/]+:[^\s/]+$/;

const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();
// "the Qingyun Group room" names the room "Qingyun Group".
const bare = (s: string) => norm(s).replace(/^the /, '').replace(/ (room|channel)$/, '').trim();

/** Resolve a room id or name against the joined rooms: exact name first (case-insensitive),
 *  then a unique substring. An ambiguous or unknown name is an error naming the choices. */
export function matchRoom(query: string, rooms: RoomEntry[]): { room: RoomEntry } | { error: string } {
	const q = query.trim();
	if (ROOM_ID_RE.test(q)) return { room: rooms.find((r) => r.id === q) ?? { id: q, name: null } };
	const named = rooms.filter((r) => r.name);
	for (const key of [norm(q), bare(q)]) {
		if (!key) continue;
		const exact = named.filter((r) => norm(r.name!) === key);
		if (exact.length === 1) return { room: exact[0] };
		const partial = exact.length ? exact : named.filter((r) => norm(r.name!).includes(key));
		if (partial.length === 1) return { room: partial[0] };
		if (partial.length > 1)
			return { error: `"${q}" matches several rooms: ${partial.map((r) => `${r.name} (${r.id})`).join('; ')}. Ask which one.` };
	}
	const known = named.map((r) => r.name).join('; ');
	return { error: `no joined room is named "${q}"${known ? `. The rooms are: ${known}` : ''}` };
}

/** The AG2 Space room a voice session origin points at, or null for any other origin. */
export function originRoom(origin: unknown): RoomEntry | null {
	const o = origin as { channel?: unknown; target?: unknown; label?: unknown } | null;
	if (!o || o.channel !== 'ag2space' || typeof o.target !== 'string' || !ROOM_ID_RE.test(o.target)) return null;
	return { id: o.target, name: typeof o.label === 'string' ? o.label : null };
}
