/**
 * Writes the relay agent's task table out for the status page, the way bodhi's history stores write the
 * conversation: the table lives in memory and calls back after each change; this writes asynchronously,
 * one write at a time, coalescing changes that arrive while a write is in flight (the last state wins).
 * A failed write is logged, never thrown, and never slows the table.
 */
import { mkdir, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

export function createVoiceTaskStatusWriter(opts: {
	path: string;
	snapshot: () => unknown;
	log?: (message: string) => void;
}): { changed(): void; flush(): Promise<void> } {
	let writing: Promise<void> | null = null;
	let dirty = false;
	let last = '';
	const writeOnce = async (): Promise<void> => {
		while (dirty) {
			dirty = false;
			const body = JSON.stringify(opts.snapshot());
			if (body === last) continue;
			try {
				await mkdir(dirname(opts.path), { recursive: true });
				const tmp = `${opts.path}.${process.pid}.tmp`;
				await writeFile(tmp, body);
				await rename(tmp, opts.path);
				last = body;
			} catch (err) {
				opts.log?.(`[RelayAgent] task status not written: ${err instanceof Error ? err.message : err}`);
			}
		}
	};
	return {
		changed() {
			dirty = true;
			if (!writing) writing = writeOnce().finally(() => { writing = null; });
		},
		async flush() {
			while (writing) await writing;
		},
	};
}
