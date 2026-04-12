/**
 * Narration Audio Tee — captures Gemini's voice during screen recordings.
 *
 * When a screen recording is active (PID file exists), tees the outbound
 * PCM audio to a raw file. When recording stops, ffmpeg muxes the narration
 * with the video to produce a *-narrated.mov file.
 *
 * Usage: optionally imported by conversation-server.
 * Phone agent works without this module.
 */

import { createWriteStream, existsSync, readFileSync, type WriteStream } from 'node:fs';
import { execSync } from 'node:child_process';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });
const SCREEN_REC_PID = '/tmp/sutando-screen-record.pid';

let narrationStream: WriteStream | null = null;
let narrationPath: string | null = null;
let narrationVideoPath: string | null = null;

export function teeAudio(pcmBuf: Buffer): void {
	const recordingActive = existsSync(SCREEN_REC_PID);
	if (recordingActive && !narrationStream) {
		try {
			const pidInfo = JSON.parse(readFileSync(SCREEN_REC_PID, 'utf8'));
			narrationVideoPath = pidInfo.path || null;
		} catch { narrationVideoPath = null; }
		narrationPath = `/tmp/sutando-narration-${Date.now()}.raw`;
		narrationStream = createWriteStream(narrationPath);
		console.log(`${ts()} [NarrationTee] started → ${narrationPath} (video: ${narrationVideoPath})`);
	} else if (!recordingActive && narrationStream) {
		const audioFile = narrationPath!;
		const videoFile = narrationVideoPath;
		narrationStream.end(() => {
			if (videoFile && existsSync(videoFile)) {
				const outPath = videoFile.replace('.mov', '-narrated.mov');
				try {
					// -map 1:v -map 0:a: use video from the recording (input 1) and audio from
					// the narration tee (input 0). Without -map, ffmpeg defaults to the video's
					// own audio track which is silent mic ambient — not the Gemini narration.
					execSync(`/opt/homebrew/bin/ffmpeg -y -f s16le -ar 24000 -ac 1 -i "${audioFile}" -i "${videoFile}" -map 1:v -map 0:a -c:v copy -c:a aac -shortest "${outPath}"`, { timeout: 60_000 });
					console.log(`${ts()} [NarrationTee] muxed → ${outPath}`);
				} catch (e) {
					console.log(`${ts()} [NarrationTee] mux failed: ${e instanceof Error ? e.message : e}`);
				}
			}
		});
		console.log(`${ts()} [NarrationTee] stopped → ${audioFile}`);
		narrationStream = null;
		narrationPath = null;
		narrationVideoPath = null;
	}
	if (narrationStream) narrationStream.write(pcmBuf);
}

export function cleanup(): void {
	if (!narrationStream) return;
	const audioFile = narrationPath!;
	const videoFile = narrationVideoPath;
	narrationStream.end(() => {
		if (videoFile && existsSync(videoFile)) {
			const outPath = videoFile.replace('.mov', '-narrated.mov');
			try {
				// -map 1:v -map 0:a: use video from the recording (input 1) and audio from
				// the narration tee (input 0). Without -map, ffmpeg defaults to the video's
				// own audio track which is silent mic ambient — not the Gemini narration.
				execSync(`/opt/homebrew/bin/ffmpeg -y -f s16le -ar 24000 -ac 1 -i "${audioFile}" -i "${videoFile}" -map 1:v -map 0:a -c:v copy -c:a aac -shortest "${outPath}"`, { timeout: 60_000 });
				console.log(`${ts()} [NarrationTee] muxed on cleanup → ${outPath}`);
			} catch (e) {
				console.log(`${ts()} [NarrationTee] mux on cleanup failed: ${e instanceof Error ? e.message : e}`);
			}
		}
	});
	narrationStream = null;
	narrationPath = null;
	narrationVideoPath = null;
}
