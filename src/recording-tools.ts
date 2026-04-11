/**
 * Recording tools — screen recording, narration, subtitles, text injection.
 * Split from browser-tools.ts for modularity.
 */

import { execSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readFileSync, readlinkSync, existsSync, statSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { captureScreen, describeScreenshot } from './inline-tools.js';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

/** Send text to Gemini via sendRealtimeInput when available, otherwise sendContent. */
export function injectText(session: any, text: string) {
	try {
		const transport = session?.transport;
		if (typeof transport?.session?.sendRealtimeInput === 'function') {
			transport.session.sendRealtimeInput({ text });
		} else if (typeof transport?.sendContent === 'function') {
			transport.sendContent([{ role: 'user', text }], true);
		} else {
			console.warn(`${ts()} [InjectText] No supported text injection method on transport`);
		}
	} catch (err) {
		console.error(`${ts()} [InjectText] Error:`, err);
	}
}

// --- Recording state ---

export let demoState: 'idle' | 'recording' | 'done' = 'idle';

export function setDemoState(val: 'idle' | 'recording' | 'done') { demoState = val; }

/** Shared mute state — conversation-server checks this in audio output handler */
export let recordingMuted = false;

export function setRecordingMuted(val: boolean) { recordingMuted = val; }

let narrationActive = false;
let lastScreenRecordCall = 0;
export const SCREEN_RECORD_COOLDOWN_MS = 5_000;

// --- Live transcript subtitle tracking ---
const LIVE_TRANSCRIPT_SYMLINK = '/tmp/sutando-live-transcript.txt';
const LIVE_TRANSCRIPT_SRT_PATH = '/tmp/sutando-live-transcript-subtitle.srt';
let liveTranscriptRecordingStart = 0;
let liveTranscriptBaselineLines = 0;
let liveTranscriptResolvedPath = '';

function countTranscriptLines(): number {
	try {
		const p = liveTranscriptResolvedPath || LIVE_TRANSCRIPT_SYMLINK;
		if (!existsSync(p)) return 0;
		return readFileSync(p, 'utf8').split('\n').filter(l => l.startsWith('[')).length;
	} catch { return 0; }
}

/** Generate SRT from transcript lines added since recording started, then burn into video. */
export function burnLiveTranscriptSubtitles(videoPath: string): string | null {
	if (liveTranscriptRecordingStart === 0) return null;
	try {
		const p = liveTranscriptResolvedPath || LIVE_TRANSCRIPT_SYMLINK;
		if (!existsSync(p)) return null;
		const allLines = readFileSync(p, 'utf8').split('\n').filter(l => l.startsWith('['));
		const newLines = allLines.slice(liveTranscriptBaselineLines);
		if (newLines.length === 0) return null;

		// Convert wall-clock timestamps to relative (from recording start)
		const startWall = (() => {
			const d = new Date(liveTranscriptRecordingStart);
			return (d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds()) * 1000;
		})();

		const fmtTime = (ms: number): string => {
			const s = Math.floor(ms / 1000);
			const h = Math.floor(s / 3600);
			const m = Math.floor((s % 3600) / 60);
			const sec = s % 60;
			const millis = ms % 1000;
			return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')},${String(millis).padStart(3, '0')}`;
		};

		const entries: { text: string; timeMs: number }[] = [];
		for (const line of newLines) {
			const match = line.match(/^\[(\d{2}):(\d{2}):(\d{2})\]\s+(.+)$/);
			if (!match) continue;
			const [, hh, mm, ss, content] = match;
			// Exclude caller speech — already audible in the narrated audio track.
			// Subtitles only show Sutando's screen descriptions to avoid redundancy.
			if (content.startsWith('Caller:') || content.startsWith('User:')) continue;
			const text = content.replace(/^Sutando:\s*/, '');
			// Skip conversational responses — only keep screen descriptions
			if (text.length < 50 && /^(Sure|OK|Okay|Got it|I'll|I can|I'm |The recording|Is there|Hello|Hi |Done|Thanks|Already|Let me|Paused)/i.test(text)) continue;
			if (/anything else|can I help|help you with|what else|else I can do|shall I|would you like|want me to|let me know/i.test(text)) continue;
			const wallMs = (Number(hh) * 3600 + Number(mm) * 60 + Number(ss)) * 1000;
			entries.push({ text, timeMs: Math.max(0, wallMs - startWall) });
		}
		if (entries.length === 0) return null;

		// Split long entries (>15 words) into smaller chunks for readable subtitles
		const MAX_WORDS = 15;
		const chunked: { text: string; timeMs: number }[] = [];
		for (const e of entries) {
			const words = e.text.split(' ');
			if (words.length <= MAX_WORDS) {
				chunked.push(e);
			} else {
				const nChunks = Math.ceil(words.length / MAX_WORDS);
				for (let i = 0; i < nChunks; i++) {
					chunked.push({
						text: words.slice(i * MAX_WORDS, (i + 1) * MAX_WORDS).join(' '),
						timeMs: e.timeMs,
					});
				}
			}
		}

		// Auto-align: STT timestamps have ~12s lag, so wall-clock times are unreliable.
		// Distribute entries evenly across recording duration instead.
		// +5000 tail padding accounts for final description still displaying; *6000 fallback
		// when all timestamps collapse to the same second (single burst of descriptions).
		const totalDurationMs = chunked[chunked.length - 1].timeMs - chunked[0].timeMs;
		const recordingDurationMs = totalDurationMs > 0 ? totalDurationMs + 5000 : chunked.length * 6000;
		const interval = recordingDurationMs / chunked.length;
		for (let i = 0; i < chunked.length; i++) {
			chunked[i].timeMs = Math.round(i * interval);
		}
		const entries2 = chunked;

		let srt = '';
		for (let i = 0; i < entries2.length; i++) {
			const start = entries2[i].timeMs;
			const end = i < entries2.length - 1 ? entries2[i + 1].timeMs : start + 5000;
			srt += `${i + 1}\n${fmtTime(start)} --> ${fmtTime(end)}\n${entries2[i].text}\n\n`;
		}

		writeFileSync(LIVE_TRANSCRIPT_SRT_PATH, srt);
		console.log(`${ts()} [ScreenRecord] live transcript SRT: ${entries.length} blocks`);

		const outPath = videoPath.replace('.mov', '-subtitled.mov');
		execSync(
			// Match source bitrate to avoid 6x size inflation (narrated ~400kbps, subtitle burn was 2300kbps with -q:v 65)
			`ffmpeg -y -i "${videoPath}" -vf "subtitles=${LIVE_TRANSCRIPT_SRT_PATH}:force_style='FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,MarginV=30'" -c:v h264_videotoolbox -b:v 500k -c:a aac "${outPath}"`,
			{ timeout: 120_000 }
		);
		if (existsSync(outPath)) {
			console.log(`${ts()} [ScreenRecord] live transcript subtitles burned: ${outPath}`);
			return outPath;
		}
	} catch (err) {
		console.log(`${ts()} [ScreenRecord] live transcript subtitle failed: ${err}`);
	}
	return null;
}

// --- Helpers ---

// Check file exists AND has meaningful size (>1KB). Prevents returning
// a recording that ffmpeg is still writing or a narrated file mid-mux.
export function isReadableFile(path: string): boolean {
	try { return existsSync(path) && statSync(path).size > 1000; } catch { return false; }
}

export function findRecording(version?: 'raw' | 'narrated' | 'subtitled'): string | null {
	try {
		const files = execSync('ls -t /tmp/sutando-recording-*.mov 2>/dev/null | grep -v narrated | grep -v subtitled | head -1', { timeout: 3_000 }).toString().trim();
		if (files && isReadableFile(files)) {
			if (version === 'raw') return files;
			const narrated = files.replace('.mov', '-narrated.mov');
			const subtitled = narrated.replace('.mov', '-subtitled.mov');
			if (version === 'subtitled') return isReadableFile(subtitled) ? subtitled : (isReadableFile(narrated) ? narrated : files);
			if (version === 'narrated') return isReadableFile(narrated) ? narrated : files;
			// Default (no version): prefer subtitled > narrated > raw
			if (isReadableFile(subtitled)) return subtitled;
			if (isReadableFile(narrated)) return narrated;
			return files;
		}
	} catch {}
	return null;
}

// --- Stop active recording ---

/** Stop any active screen recording */
export function stopActiveRecording(): void {
	try { execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 5_000 }); } catch {}
}

// --- Recording hooks ---

/**
 * Set up all recording hooks on a voice session.
 * Call once per session — handles tool triggers, reconnect, and cleanup automatically.
 */
export function setupRecordingHooks(session: any): void {
	// Start narration when record_screen_with_narration is called
	session.eventBus?.subscribe?.('tool.call', (e: any) => {
		if (e?.toolName === 'record_screen_with_narration') {
			setTimeout(() => {
				if (existsSync('/tmp/sutando-screen-record.pid')) startRecordingNarration(session);
			}, 4000);
		}
	});
}

/** Called on Gemini reconnect — nudge to continue narrating if recording active */
export function onReconnect(session: any): void {
	if (!existsSync('/tmp/sutando-screen-record.pid')) return;
	try {
		injectText(session, '[System: You were narrating a screen demo. Continue where you left off — call describe_screen and keep narrating. Do NOT greet or say "I\'m back".]');
	} catch {}
}

/**
 * Start narration controller for an active recording.
 * Called by conversation-server when scroll_and_describe starts.
 * Handles: description pushing, stop detection, mute/unmute, reconnect narration.
 */
export function startRecordingNarration(session: any): void {
	if (narrationActive) return; // prevent duplicate controllers
	narrationActive = true;

	// Read scroll info for description interval + duration
	let descIntervalMs = 8000;
	let durationMs = 30000;
	try {
		if (existsSync('/tmp/sutando-scroll-info.json')) {
			const info = JSON.parse(readFileSync('/tmp/sutando-scroll-info.json', 'utf8'));
			descIntervalMs = Math.max(Math.round((info.msPerViewport || 8000) * 0.7), 5000);
			durationMs = (info.duration_seconds || 30) * 1000;
			console.log(`${ts()} [Recording] interval: ${descIntervalMs}ms, duration: ${durationMs}ms`);
		}
	} catch {}

	let lastDesc = '';
	const previousDescs: string[] = []; // track all narrated descriptions
	const startTime = Date.now();
	const STOP_PUSHING_BEFORE_END_MS = 8000; // stop pushing 8s before recording ends

	const pushDescription = async () => {
		if (!existsSync('/tmp/sutando-screen-record.pid')) return;
		// Stop pushing near the end so Gemini finishes naturally
		const elapsed = Date.now() - startTime;
		if (elapsed > durationMs - STOP_PUSHING_BEFORE_END_MS) {
			console.log(`${ts()} [Recording] near end — stopped pushing`);
			clearInterval(descTimer);
			try {
				injectText(session, '[System: Recording ending soon. Finish your current sentence and stop.]');
			} catch {}
			return;
		}
		try {
			const path = await captureScreen();
			if (!path) return;
			const desc = await describeScreenshot(path, previousDescs);
			if (!desc || desc === lastDesc) {
				if (desc === lastDesc) console.log(`${ts()} [Recording] skipped duplicate`);
				return;
			}
			lastDesc = desc;
			previousDescs.push(desc);
			if (!existsSync('/tmp/sutando-screen-record.pid')) return;
			const remaining = Math.round((durationMs - (Date.now() - startTime)) / 1000);
			const alreadySaid = previousDescs.slice(0, -1).map((d, i) => `${i + 1}. ${d.slice(0, 40)}`).join('; ');
			injectText(session, `[System: ${remaining}s left. Already narrated: ${alreadySaid || 'nothing yet'}. Now narrate this NEW content only (1 short sentence, no repeats). Do NOT say "anything else", "can I help", "is there", or any conversational filler — ONLY describe what is on screen: "${desc}"]`);
			console.log(`${ts()} [Recording] pushed: ${desc.slice(0, 60)}...`);
		} catch (err) {
			console.log(`${ts()} [Recording] push error: ${err}`);
		}
	};

	// Early first push + interval
	setTimeout(pushDescription, 5000);
	const descTimer = setInterval(pushDescription, descIntervalMs);

	// Timer-based stop — uses known duration, no polling
	// Stop pushing 8s before end (already handled in pushDescription)
	// At exactly durationMs: clear timers, send "recording complete"
	setTimeout(() => {
		clearInterval(descTimer);
		narrationActive = false;
		console.log(`${ts()} [Recording] timer fired — sending stop`);
		try {
			injectText(session, '[System: Recording just ended. Say "The recording is complete." immediately.]');
		} catch {}
	}, durationMs + 1000); // +1s buffer for auto-stop to finish
}

// recordScreenWithNarrationTool lives in remote-meeting-tools.ts
// (narration audio is only needed when user is remote via phone/Zoom)
// It calls this helper which encapsulates all scroll+record+subtitle logic.
export async function startNarrationRecording(duration_seconds: number): Promise<{ status: string; first_description?: string; message?: string; error?: string }> {
	const MAX_DURATION = 60;
	const capped = Math.min(duration_seconds, MAX_DURATION);
	if (duration_seconds > MAX_DURATION) console.log(`${ts()} [RecordWithNarration] capped duration from ${duration_seconds}s to ${MAX_DURATION}s`);
	try {
		if (demoState === 'recording') return { status: 'already_recording', message: 'Already recording.' };
		if (demoState === 'done') demoState = 'idle';
		demoState = 'recording';

		// Scroll to top
		execSync(`osascript -e 'tell application "System Events" to key code 126 using command down'`, { timeout: 5_000 });

		// Start recording + first describe_screen in parallel
		try { unlinkSync(LIVE_TRANSCRIPT_SRT_PATH); } catch {}
		execSync('python3 skills/screen-record/scripts/record.py start', { timeout: 10_000 });
		const captureRes = await fetch('http://localhost:7845/capture');
		const captureData = await captureRes.json() as { status: string; path?: string };
		const firstDesc = captureData.path ? await describeScreenshot(captureData.path) : '';
		// Resolve the symlink NOW — safe because only owner calls update the symlink
		// (non-owner calls like Zoom IVR write their own transcript but don't touch it).
		try { liveTranscriptResolvedPath = readlinkSync(LIVE_TRANSCRIPT_SYMLINK); } catch { liveTranscriptResolvedPath = ''; }
		liveTranscriptRecordingStart = Date.now();
		liveTranscriptBaselineLines = countTranscriptLines();

		// Adaptive scroll speed
		let pageHeight = 5000;
		try {
			pageHeight = parseInt(execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "document.body.scrollHeight - window.innerHeight"'`, { timeout: 3_000 }).toString().trim()) || 5000;
		} catch {}
		const SCROLL_INTERVAL_MS = 2500;
		const totalScrollSteps = (capped * 1000) / SCROLL_INTERVAL_MS;
		const pxPerStep = Math.ceil(pageHeight / totalScrollSteps);
		const viewportHeight = 900;
		const msPerViewport = Math.round((viewportHeight / pxPerStep) * SCROLL_INTERVAL_MS);
		writeFileSync('/tmp/sutando-scroll-info.json', JSON.stringify({ pageHeight, pxPerStep, msPerViewport, duration_seconds: capped }));
		console.log(`${ts()} [RecordWithNarration] page=${pageHeight}px, ${totalScrollSteps} steps, ${pxPerStep}px/step`);
		let scrolledTotal = 0;
		const scrollInterval = setInterval(() => {
			if (scrolledTotal >= pageHeight) return;
			try { execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollBy(0, ${pxPerStep})"'`, { timeout: 5_000 }); } catch {}
			scrolledTotal += pxPerStep;
		}, SCROLL_INTERVAL_MS);

		const myRecStart = liveTranscriptRecordingStart;
		setTimeout(async () => {
			clearInterval(scrollInterval);
			let stopResult: any = {};
			try { stopResult = JSON.parse(execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 10_000 }).toString().trim()); } catch {}
			const narrated = stopResult.path ? stopResult.path.replace('.mov', '-narrated.mov') : '';
			for (let w = 0; w < 5; w++) {
				if (narrated && isReadableFile(narrated)) break;
				await new Promise(r => setTimeout(r, 2000));
			}
			if (liveTranscriptRecordingStart > 0 && narrated && isReadableFile(narrated)) {
				const subtitled = burnLiveTranscriptSubtitles(narrated);
				if (subtitled) console.log(`${ts()} [RecordWithNarration] subtitle burned: ${subtitled}`);
				else console.log(`${ts()} [RecordWithNarration] subtitle burn failed`);
			}
			if (liveTranscriptRecordingStart === myRecStart) liveTranscriptRecordingStart = 0;
			demoState = 'done';
			console.log(`${ts()} [RecordWithNarration] auto-stop`);
		}, capped * 1000);

		console.log(`${ts()} [RecordWithNarration] recording started`);
		return { status: 'recording', first_description: firstDesc, message: `Recording started. IMMEDIATELY speak this narration — NO filler, NO "okay", NO "should I": "${firstDesc}". Auto-stops in ${capped}s.` };
	} catch (err) {
		return { status: 'error', error: `record_screen_with_narration failed: ${err instanceof Error ? err.message : err}` };
	}
}

// --- Screen recording tool ---

export const screenRecordTool: ToolDefinition = {
	name: 'screen_record',
	description:
		'Start or stop a bare screen recording WITHOUT narration or scrolling. For recording with narration, use scroll_and_describe instead.',
	parameters: z.object({
		action: z.enum(['start', 'stop']).describe('"start" begins recording, "stop" stops and saves the file'),
		duration_seconds: z.number().optional().describe('If provided with start, auto-stops after this many seconds.'),
		subtitle: z.boolean().optional().describe('If true, burn live conversation transcript as subtitles into the recording. Ask the user before setting this.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, duration_seconds, subtitle } = args as { action: 'start' | 'stop'; duration_seconds?: number; subtitle?: boolean };
		// Hard block: if already recording, refuse to start again
		if (action === 'start' && demoState === 'recording') {
			console.log(`${ts()} [ScreenRecord] BLOCKED duplicate start (already recording)`);
			return { status: 'already_recording', message: 'Recording is already in progress. Do NOT call screen_record start again.' };
		}
		const now = Date.now();
		if (now - lastScreenRecordCall < SCREEN_RECORD_COOLDOWN_MS) {
			return { status: 'cooldown', message: 'Wait a few seconds.' };
		}
		lastScreenRecordCall = now;
		try {
			const result = execSync(`python3 skills/screen-record/scripts/record.py ${action}`, { timeout: 10_000 }).toString().trim();
			// Auto-stop timer — cap at 60s regardless of what Gemini requests
			if (action === 'start') {
				demoState = 'recording';
				// Track transcript baseline for live subtitle generation (only if user wants subtitles)
				if (subtitle) {
					try { liveTranscriptResolvedPath = readlinkSync(LIVE_TRANSCRIPT_SYMLINK); } catch { liveTranscriptResolvedPath = ''; }
					liveTranscriptRecordingStart = Date.now();
					liveTranscriptBaselineLines = countTranscriptLines();
					try { unlinkSync(LIVE_TRANSCRIPT_SRT_PATH); } catch {}
					console.log(`${ts()} [ScreenRecord] live transcript subtitles enabled`);
				} else {
					liveTranscriptRecordingStart = 0;
				}
				const capped = Math.min(duration_seconds || 20, 60);
				setTimeout(() => {
					try {
						const stopResult = execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 10_000 }).toString().trim();
						const stopParsed = JSON.parse(stopResult);
						if (liveTranscriptRecordingStart > 0 && stopParsed.path && stopParsed.exists) {
							const narrated = stopParsed.path.replace('.mov', '-narrated.mov');
							burnLiveTranscriptSubtitles(isReadableFile(narrated) ? narrated : stopParsed.path);
						}
					} catch {}
					demoState = 'done';
					liveTranscriptRecordingStart = 0;
					console.log(`${ts()} [ScreenRecord] auto-stop after ${capped}s (requested ${duration_seconds}s)`);
				}, capped * 1000);
			}
			if (action === 'stop') {
				demoState = 'done';
				const parsed = JSON.parse(result);
				// Burn live transcript subtitles only if enabled at start
				if (liveTranscriptRecordingStart > 0 && parsed.path && parsed.exists) {
					const narrated = parsed.path.replace('.mov', '-narrated.mov');
					const subtitled = burnLiveTranscriptSubtitles(isReadableFile(narrated) ? narrated : parsed.path);
					if (subtitled) {
						parsed.subtitled_path = subtitled;
						console.log(`${ts()} [ScreenRecord] transcript subtitles: ${subtitled}`);
					}
				}
				liveTranscriptRecordingStart = 0;
				console.log(`${ts()} [ScreenRecord] ${action}: ${JSON.stringify(parsed)}`);
				return parsed;
			}
			const parsed = JSON.parse(result);
			console.log(`${ts()} [ScreenRecord] ${action}: ${result}`);
			return parsed;
		} catch (err) {
			return { error: `screen_record failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};
