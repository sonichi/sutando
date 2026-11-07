import { lstatSync, readFileSync, realpathSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, isAbsolute, join } from 'node:path';

export function captureArtifactDir(): string {
	return process.env.SUTANDO_SCREENSHOT_DIR || join(tmpdir(), 'sutando-screenshots');
}

export function verifyCaptureArtifact(p: unknown, dir = captureArtifactDir()): string {
	if (typeof p !== 'string' || !p || !isAbsolute(p) || !/^screen-[A-Za-z0-9_.-]+\.(?:jpe?g|png)$/.test(basename(p))) {
		throw new Error('screen-capture-server returned an untrusted path');
	}
	try {
		const directoryStat = lstatSync(dir);
		const fileStat = lstatSync(p);
		const directoryRealPath = realpathSync(dir);
		if (!directoryStat.isDirectory() || fileStat.isSymbolicLink() || !fileStat.isFile()) {
			throw new Error('screen-capture-server returned an untrusted path');
		}
		if (realpathSync(dirname(p)) !== directoryRealPath) {
			throw new Error('screen-capture-server returned an untrusted path');
		}
		if (typeof process.getuid === 'function') {
			const uid = process.getuid();
			if (directoryStat.uid !== uid || (directoryStat.mode & 0o022) !== 0 || fileStat.uid !== uid) {
				throw new Error('screen-capture-server returned an untrusted path');
			}
		}
		return realpathSync(p);
	} catch (error) {
		if (error instanceof Error && error.message === 'screen-capture-server returned an untrusted path') throw error;
		throw new Error('screen-capture-server returned an untrusted path', { cause: error });
	}
}

export function takeCaptureArtifact(p: unknown): Buffer {
	const artifact = verifyCaptureArtifact(p);
	const data = readFileSync(artifact);
	try {
		unlinkSync(artifact);
	} catch (error) {
		console.warn(`[CaptureArtifact] failed to unlink ${artifact}: ${error instanceof Error ? error.message : error}`);
	}
	return data;
}
