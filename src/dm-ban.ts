// dm-ban.sentinel gate — the TypeScript half of src/dm_ban.py's contract.
// Fails closed: any resolution error means banned, never silently delivers.

import { statSync } from 'node:fs';
import { join } from 'node:path';

/** True when DM delivery is suppressed. An unreadable sentinel counts as banned. */
export function isDmBanned(workspaceDir: string): boolean {
	try {
		statSync(join(workspaceDir, 'state', 'dm-ban.sentinel'));
	} catch (err) {
		// ENOENT is the only "not banned" answer; EACCES/EIO/ELOOP leave the
		// state unknown, and unknown must not authorise a send.
		return (err as NodeJS.ErrnoException)?.code !== 'ENOENT';
	}
	return true;
}
