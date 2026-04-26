/**
 * Recording, video playback, and scroll-and-describe tools.
 * Extracted from browser-tools.ts for readability.
 */

import { execSync, execFileSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readFileSync, readlinkSync, existsSync, statSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { demoStateRef, narrationSpeakingRef, lastSpokenRef, nextDescRef, scrollPausedRef } from './recording-state.js';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

/**
 * Auto-detect an ffmpeg binary that has the `subtitles` filter (requires libass).
 * Cached after first call so the probe only runs once per process lifetime.
 * Probe order: $FFMPEG_SUBTITLE_BIN env → system ffmpeg → homebrew narrow → homebrew full.
 */
let _cachedSubtitleFfmpeg: string | null | undefined;
function findFfmpegWithSubtitles(): string | null {
	if (_cachedSubtitleFfmpeg !== undefined) return _cachedSubtitleFfmpeg;
	const envBin = process.env.FFMPEG_SUBTITLE_BIN?.trim();
	if (envBin) { _cachedSubtitleFfmpeg = envBin; console.log(`${ts()} [ffmpeg] using $FFMPEG_SUBTITLE_BIN: ${envBin}`); return envBin; }
	const candidates = ['ffmpeg', '/opt/homebrew/bin/ffmpeg', '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'];
	for (const bin of candidates) {
		try {
			if (execSync(`${bin} -filters 2>&1`, { timeout: 5_000 }).toString().includes('subtitles')) {
				_cachedSubtitleFfmpeg = bin;
				console.log(`${ts()} [ffmpeg] subtitle filter found in: ${bin}`);
				return bin;
			}
		} catch {}
	}
	console.log(`${ts()} [ffmpeg] no binary with subtitles filter found`);
	_cachedSubtitleFfmpeg = null;
	return null;
}

/** Send text to Gemini via sendRealtimeInput when available, otherwise sendContent. */
function injectText(session: any, text: string) {
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

// Vision model — override via .env (default: flash-lite for this trivial 20-word task)
const VISION_MODEL = process.env.VISION_MODEL || 'gemini-3.1-flash-lite-preview';

// --- Shared recording state ---

/** Reset recording state — call when a new phone call starts or previous recording is stuck */
export function resetDemoState(): void {
	if (demoStateRef.value !== 'idle') {
		console.log(`${ts()} [DemoState] Reset from '${demoStateRef.value}' → 'idle'`);
		demoStateRef.value = 'idle';
	}
}

/** Shared mute state — conversation-server checks this in audio output handler */
export const recordingState = { muted: false };

/** Stop any active screen recording */
export function stopActiveRecording(): void {
	try { execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 5_000 }); } catch {}
}

/** Check if a recording is currently active */
export function isRecordingActive(): boolean {
	return existsSync('/tmp/sutando-screen-record.pid');
}

/** Check if recording audio should be muted */
export function isRecordingMuted(): boolean {
	return recordingState.muted;
}

// --- Live transcript subtitle tracking ---
// When subtitle=true, captures conversation transcript during recording
// and burns it as SRT into the video when recording stops.
// Symlink points to the active call's transcript (phone or voice agent)
const LIVE_TRANSCRIPT_SYMLINK = '/tmp/sutando-live-transcript.txt';
const LIVE_TRANSCRIPT_SRT_PATH = '/tmp/sutando-live-transcript-subtitle.srt';
const VOICE_TRANSCRIPT_PATH = '/tmp/sutando-live-transcript-voice.txt';
let liveTranscriptRecordingStart = 0;
let liveTranscriptBaselineLines = 0;

// Pick the freshest user-speech transcript (voice-agent or phone) and return
// the last few user-spoken lines as lowercase. Returns '' if neither is
// reasonably fresh (≥60s old) so callers fail-open rather than blocking on
// stale data — e.g. a phone-call symlink left over from hours ago when the
// current session is voice-agent.
function getRecentUserSpeech(): string {
	const candidates: string[] = [];
	if (existsSync(VOICE_TRANSCRIPT_PATH)) candidates.push(VOICE_TRANSCRIPT_PATH);
	try {
		const phonePath = readlinkSync(LIVE_TRANSCRIPT_SYMLINK);
		if (existsSync(phonePath)) candidates.push(phonePath);
	} catch {}
	let bestPath = '';
	let bestMtime = 0;
	for (const p of candidates) {
		try {
			const m = statSync(p).mtimeMs;
			if (m > bestMtime) { bestMtime = m; bestPath = p; }
		} catch {}
	}
	if (!bestPath || Date.now() - bestMtime > 60_000) return '';
	try {
		const lines = readFileSync(bestPath, 'utf8').split('\n');
		const userLines = lines.filter(l => /\b(Caller|User):/i.test(l));
		return userLines.slice(-3).join(' ').toLowerCase();
	} catch { return ''; }
}
// Resolved path to the call-specific transcript file, captured at recording start.
// A concurrent call (e.g. Zoom join) can overwrite the symlink, so we resolve it
// once and use the resolved path for the entire recording lifecycle.
let liveTranscriptResolvedPath = '';

function countTranscriptLines(): number {
	try {
		const p = liveTranscriptResolvedPath || LIVE_TRANSCRIPT_SYMLINK;
		if (!existsSync(p)) return 0;
		return readFileSync(p, 'utf8').split('\n').filter(l => l.startsWith('[')).length;
	} catch { return 0; }
}

/** Generate SRT from transcript lines added since recording started, then burn into video. */
function burnLiveTranscriptSubtitles(videoPath: string): string | null {
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
			if (/anything else|can I help|help you with|what else|else I can do|shall I|would you like|want me to|let me know/i.test(text)) continue;
			if (text.length < 50 && /^(Sure|OK|Okay|Got it|I'll|I can|I'm |The recording|Is there|Hello|Hi |Done|Thanks|Already|Let me|Paused)/i.test(text)) continue;
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
		const ffmpegBin = findFfmpegWithSubtitles();
		if (!ffmpegBin) {
			console.log(`${ts()} [ScreenRecord] no ffmpeg with subtitles filter — skipping burn. Install: brew install ffmpeg-full`);
			return null;
		}
		// Use execFileSync argv array to avoid shell interpolation of $ffmpegBin,
		// $videoPath, and $outPath (same CodeQL #27 class fixed for sips below).
		execFileSync(
			ffmpegBin,
			[
				'-y',
				'-i', videoPath,
				'-vf', `subtitles=${LIVE_TRANSCRIPT_SRT_PATH}:force_style='FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,MarginV=30'`,
				'-c:v', 'h264_videotoolbox',
				'-b:v', '500k',
				'-c:a', 'aac',
				outPath,
			],
			{ timeout: 120_000 }
		);
		if (existsSync(outPath)) {
			console.log(`${ts()} [ScreenRecord] live transcript subtitles burned: ${outPath} (ffmpeg=${ffmpegBin})`);
			return outPath;
		}
	} catch (err) {
		console.log(`${ts()} [ScreenRecord] live transcript subtitle failed: ${err}`);
	}
	return null;
}

// Check file exists AND has meaningful size (>1KB). Prevents returning
// a recording that ffmpeg is still writing or a narrated file mid-mux.
function isReadableFile(path: string): boolean {
	try { return existsSync(path) && statSync(path).size > 1000; } catch { return false; }
}

function findRecording(version?: 'raw' | 'narrated' | 'subtitled'): string | null {
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

// --- Vision helpers ---

async function describeScreenshot(imagePath: string, previousDescs: string[] = []): Promise<string> {
	const apiKey = process.env.GEMINI_API_KEY;
	if (!apiKey) return 'Vision description unavailable (no GEMINI_API_KEY)';
	try {
		// Fixes CodeQL #27 (js/command-line-injection): use execFileSync argv array instead of shell string
		const safePath = imagePath.replace(/[^a-zA-Z0-9_\-./]/g, '');
		const resized = safePath.endsWith('.png') ? safePath.replace(/\.png$/, '-sm.jpg') : safePath + '-sm.jpg';
		try {
			execFileSync('sips', ['-Z', '800', '-s', 'format', 'jpeg', safePath, '--out', resized], { timeout: 2_000, stdio: 'ignore' });
		} catch { /* use original if resize fails */ }
		const actualPath = existsSync(resized) ? resized : imagePath;
		const mimeType = actualPath.endsWith('.jpg') ? 'image/jpeg' : 'image/png';
		const imageData = readFileSync(actualPath).toString('base64');
		// Issue #189: when continuing a narration, the vision model should build
		// on what was already said instead of re-introducing the page every
		// time. First call: introduce with the heading. Later calls: flow on.
		// Vision model describes content. First call introduces; follow-ups focus on NEW content only.
		const guard = 'ONLY describe what you SEE in the image. Do NOT use external knowledge.';
		let prompt: string;
		if (previousDescs.length === 0) {
			prompt = `Describe what is on screen in 1 sentence (max 20 words). Name the page/heading. ${guard}`;
		} else {
			const alreadyCovered = previousDescs.slice(-2).join(' ');
			prompt = `Already described: "${alreadyCovered}". What NEW section headings or content are now visible that were NOT in the previous descriptions? Ignore anything already mentioned. 1 sentence, max 15 words, only new content. If nothing new, say "same content". ${guard}`;
		}
		const res = await fetch(
			`https://generativelanguage.googleapis.com/v1beta/models/${VISION_MODEL}:generateContent?key=${apiKey}`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					contents: [{
						parts: [
							{ text: prompt },
							{ inlineData: { mimeType, data: imageData } },
						],
					}],
					generationConfig: { maxOutputTokens: 40 },
				}),
			},
		);
		const data = await res.json() as any;
		if (!data?.candidates?.[0]) {
			const reason = data?.promptFeedback?.blockReason || data?.error?.message || JSON.stringify(data).slice(0, 200);
			console.log(`${new Date().toLocaleTimeString()} [DescribeScreen] API response: ${reason}`);
			return `Could not describe the screen. (${reason})`;
		}
		return data.candidates[0].content?.parts?.[0]?.text ?? 'Could not describe the screen.';
	} catch (err) {
		return `Vision error: ${err instanceof Error ? err.message : err}`;
	}
}

async function captureScreen(): Promise<string | null> {
	try {
		const res = await fetch('http://localhost:7845/capture');
		const data = await res.json() as { status: string; path?: string };
		return data.status === 'ok' && data.path ? data.path : null;
	} catch { return null; }
}

export function scrollDown(pixels: number = 600) {
	// Use widest-element heuristic (same as scrollTool) so embedded/nested scrollable containers work
	// NO keyboard fallback here — Chrome activate + Page Down disrupts narration audio capture
	// during recording (breaks subtitle generation). Interactive scrollTool has the keyboard
	// fallback for the Zoom screen share case; recording uses JS-only.
	const js = `(function(){var best=document.scrollingElement||document.documentElement,bw=0;document.querySelectorAll('*').forEach(function(el){var d=el.scrollHeight-el.clientHeight;if(d>50&&el.clientHeight>200){var w=el.getBoundingClientRect().width;if(w>bw){best=el;bw=w}}});best.scrollBy(0,${pixels})})()`;
	const tmpScroll = `/tmp/sutando-scroll-rec-${Date.now()}.scpt`;
	writeFileSync(tmpScroll, `tell application "Google Chrome" to tell active tab of front window to execute javascript "${js.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`);
	execSync(`osascript ${tmpScroll}`, { timeout: 5_000 });
	try { unlinkSync(tmpScroll); } catch {}
	// Repaint trigger: Chrome defers visual repaints during Zoom screen share.
	// CGEvent scroll wheel events force a repaint through the OS input pipeline
	// without stealing focus (unlike keyboard fallback which breaks narration).
	try {
		execSync(`swift src/scroll-wheel.swift 1`, { timeout: 3_000 });
	} catch { /* best-effort — scroll already happened via JS */ }
}

// --- Tools ---

export const scrollAndDescribeTool: ToolDefinition = {
	name: 'record_screen_with_narration',
	description:
		'Record a NARRATED demo video — auto-scrolls the page and returns descriptions for you to speak. ' +
		'Use ONLY when user says "record with narration", "record for N seconds", or "demo video". ' +
		'For plain screen recording WITHOUT narration, use screen_record instead. ' +
		'Call ONCE with duration_seconds. SPEAK the returned description as your first words (do NOT announce "starting recording"). ' +
		'New descriptions will be pushed as the page scrolls — speak each one. NEVER repeat earlier narration. ' +
		'Recording auto-stops. Do NOT call this more than once per recording.',
	parameters: z.object({
		duration_seconds: z.number().optional().describe('Target duration in seconds (default 15, max 60). ALWAYS seconds, never minutes.'),
	}),
	execution: 'inline',
	async execute(args) {
		const MAX_DURATION = 60;
		const rawDuration = (args as { duration_seconds?: number }).duration_seconds ?? 15;
		const duration_seconds = Math.min(rawDuration, MAX_DURATION);
		if (rawDuration > MAX_DURATION) console.log(`${ts()} [ScrollAndDescribe] capped duration from ${rawDuration}s to ${MAX_DURATION}s`);
		try {
			// Prevent duplicate recordings
			if (demoStateRef.value === 'recording') return { status: 'already_recording', message: 'Already recording.' };
			// Reset from previous recording — allow new one
			if (demoStateRef.value === 'done') demoStateRef.value = 'idle';
			demoStateRef.value = 'recording';

			// Scroll to top and wait for it to take effect
			execSync(`osascript -e 'tell application "Google Chrome" to activate' -e 'delay 0.3' -e 'tell application "System Events" to key code 126 using command down'`, { timeout: 5_000 });
			// Also use JS scroll as backup (keyboard may not work if Chrome isn't focused)
			try { execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollTo(0,0)"'`, { timeout: 3_000 }); } catch {}
			await new Promise(r => setTimeout(r, 500)); // let scroll settle

			// Capture + describe FIRST, then start recording.
			// This way the vision API latency doesn't eat into recording time.
			const captureRes = await fetch('http://localhost:7845/capture');
			const captureData = await captureRes.json() as { status: string; path?: string };
			const firstDesc = captureData.path ? await describeScreenshot(captureData.path) : '';
			try { unlinkSync(LIVE_TRANSCRIPT_SRT_PATH); } catch {}
			execSync('python3 skills/screen-record/scripts/record.py start', { timeout: 10_000 });
			// Set subtitle baseline — pick whichever transcript was updated more recently.
			// Voice agent writes to -voice.txt; phone conversation-server writes to -CA{sid}.txt via symlink.
			const voiceTranscript = '/tmp/sutando-live-transcript-voice.txt';
			let phoneTranscript = '';
			try { phoneTranscript = readlinkSync(LIVE_TRANSCRIPT_SYMLINK); } catch {}
			if (existsSync(voiceTranscript) && phoneTranscript && existsSync(phoneTranscript)) {
				// Both exist — use whichever was modified more recently
				const vMtime = statSync(voiceTranscript).mtimeMs;
				const pMtime = statSync(phoneTranscript).mtimeMs;
				liveTranscriptResolvedPath = pMtime > vMtime ? phoneTranscript : voiceTranscript;
			} else {
				liveTranscriptResolvedPath = (phoneTranscript && existsSync(phoneTranscript)) ? phoneTranscript : (existsSync(voiceTranscript) ? voiceTranscript : '');
			}
			liveTranscriptRecordingStart = Date.now();
			liveTranscriptBaselineLines = countTranscriptLines();

			// Fixed scroll timer — one pass top-to-bottom over the full duration.
			// Description pushes happen via the narration controller at a separate cadence.
			let pageHeight = 5000;
			try {
				pageHeight = parseInt(execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "document.body.scrollHeight - window.innerHeight"'`, { timeout: 3_000 }).toString().trim()) || 5000;
			} catch {}
			const viewportHeight = 900;
			writeFileSync('/tmp/sutando-scroll-info.json', JSON.stringify({ pageHeight, viewportHeight, duration_seconds }));
			console.log(`${ts()} [ScrollAndDescribe] page=${pageHeight}px, narration-driven scroll`);
			// No fixed scroll timer — scroll is driven by narration cycle:
			// speak desc → pre-capture scrolls to next viewport → capture → speak → repeat
			scrollPausedRef.value = true; // start paused, first content already captured

			// Auto-stop after duration — wait for narration-tee mux, then burn subtitles.
			// Capture start time: if user starts a 2nd recording before this timer fires,
			// liveTranscriptRecordingStart will be overwritten. Only clear if still ours.
			const myRecStart = liveTranscriptRecordingStart;
			setTimeout(async () => {
				scrollPausedRef.value = false;
				let stopResult: any = {};
				try {
					const raw = execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 10_000 }).toString().trim();
					stopResult = JSON.parse(raw);
				} catch {}
				// Explicitly flush narration-tee (it normally triggers on next audio chunk,
				// but after recording stops Gemini may not send audio for seconds).
				try {
					const { cleanup: flushNarrationTee } = await import('../skills/screen-record/scripts/narration-tee.js');
					flushNarrationTee();
				} catch {}
				// Wait for narrated.mov to exist (narration-tee mux ~2-6s after flush)
				const narrated = stopResult.path ? stopResult.path.replace('.mov', '-narrated.mov') : '';
				for (let w = 0; w < 8; w++) {
					if (narrated && isReadableFile(narrated)) break;
					await new Promise(r => setTimeout(r, 2000));
				}
				// Burn live transcript subtitles on narrated version only
				if (liveTranscriptRecordingStart > 0 && narrated && isReadableFile(narrated)) {
					const subtitled = burnLiveTranscriptSubtitles(narrated);
					if (subtitled) console.log(`${ts()} [ScrollAndDescribe] subtitle burned: ${subtitled}`);
					else console.log(`${ts()} [ScrollAndDescribe] subtitle burn failed (no transcript lines or ffmpeg error)`);
				}
				if (liveTranscriptRecordingStart === myRecStart) liveTranscriptRecordingStart = 0;
				demoStateRef.value = 'done';
				console.log(`${ts()} [ScrollAndDescribe] auto-stop`);
			}, duration_seconds * 1000);

			console.log(`${ts()} [ScrollAndDescribe] recording started with first desc`);
			// Start narration controller directly (don't rely on eventBus hook)
			if (_narrationSession) {
				setTimeout(() => {
					if (isRecordingActive()) {
						console.log(`${ts()} [ScrollAndDescribe] starting narration controller`);
						startRecordingNarration(_narrationSession);
					}
				}, 4000);
			}
			return {
				status: 'recording',
				first_description: firstDesc,
				message: `Recording started. IMMEDIATELY speak this narration — NO filler, NO "okay", NO "should I": "${firstDesc}". Auto-stops in ${duration_seconds}s.`,
			};
		} catch (err) {
			return { error: `record_screen_with_narration failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Video playback tools — split from a single polymorphic playRecordingTool into 6
// single-purpose tools. Gemini selects more reliably with narrow descriptions than
// with one tool that has an "action" enum. The old tool caused persistent confusion
// between "open" and "play" (42% of calls in diagnostics had wrong action selection).
export const openFileTool: ToolDefinition = {
	name: 'open_file',
	description:
		'Open a file with the default macOS app. ALWAYS pass a `path` — get it from the recording tool\'s return value or ask the user. ' +
		'Use for: "open the video/recording", "open the file", "open that", "can you open it". ' +
		'If the user says "open the log" or similar, ASK which log they mean (voice-agent, discord-bridge, etc.) — do NOT default to a recording. ' +
		'Do NOT call play_video after this — wait for user to explicitly say "play". ' +
		'NEVER call this tool to enter fullscreen on a video that is already open — call the `fullscreen` tool instead. ' +
		'Known files: "diagnostic tracker" or "diagnostics" = /tmp/phone-diagnostics-tracker.html, ' +
		'"voice diagnostics" = /tmp/voice-diagnostics-tracker.html. ' +
		'If you need to find the latest recording but lost the path, pass find_recording=true. ' +
		'For OPENING A NEW video file (.mp4 / .mov) that should play as a presentation, pass fullscreen=true — ' +
		'QuickTime will activate fullscreen "present" mode after opening.',
	parameters: z.object({
		path: z.string().optional().describe('File path to open. Get this from the recording tool result. Use known file aliases for diagnostic tracker etc.'),
		find_recording: z.boolean().optional().describe('If true and no path given, find and open the latest screen recording. Only use as a fallback when you lost the recording path.'),
		fullscreen: z.boolean().optional().describe('If true, enter QuickTime Player fullscreen (present mode) after opening. Use for video showcase moments. No effect on non-video files or if QuickTime didn\'t claim the file.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { path: filePath, find_recording, fullscreen } = args as { path?: string; find_recording?: boolean; fullscreen?: boolean };
		console.log(`${ts()} [OpenFile] called (path=${filePath || 'none'}, find_latest=${find_recording || false}, fullscreen=${fullscreen || false})`);
		demoStateRef.value = 'idle';
		try {
			let recPath = filePath ? filePath.replace(/^~/, process.env.HOME || '') : null;
			if (recPath && !existsSync(recPath)) {
				console.log(`${ts()} [OpenFile] path "${recPath}" does not exist`);
				return { error: `File not found: ${recPath}. Ask the user for the correct path or use "work" to locate it.` };
			}
			// Only find latest recording if explicitly requested — not as a silent default.
			// The recording tool should have already told you the path.
			if (!recPath && find_recording) {
				recPath = findRecording();
			}
			if (!recPath) {
				return { error: 'No path provided. Pass the file path from the recording tool result, or set find_recording=true to search.' };
			}
			if (!isReadableFile(recPath)) {
				return { error: `File not readable or too small: ${recPath}. It may still be writing — try again in a few seconds.` };
			}
			if (recPath.includes('sutando-recording')) {
				writeFileSync('/tmp/sutando-playback-path', recPath);
			}
			// execFileSync — no shell interpolation of caller-controlled recPath
			// (same CodeQL js/command-line-injection class as #27).
			// When fullscreen, force the open through QuickTime Player so we don't
			// race with whatever app currently claims .mp4 (VLC/IINA/etc). For
			// non-fullscreen opens, keep the default-app behaviour for non-video
			// files (logs, PDFs, diagnostic trackers) — those should use whatever
			// the user has set as the default.
			if (fullscreen) {
				execFileSync('open', ['-a', 'QuickTime Player', recPath], { timeout: 5_000 });
			} else {
				execFileSync('open', [recPath], { timeout: 5_000 });
			}
			try { execSync(`osascript -e 'tell application "QuickTime Player" to activate'`, { timeout: 3_000 }); } catch {}
			// Fullscreen (present mode) for video showcase. Wait for QuickTime to
			// finish loading THIS file (path-match, not just any document) before
			// presenting — prevents fullscreening a stale prior document if QT
			// already had one open. If the 3s budget elapses without a match
			// (slow load, symlink/relative-path mismatch, prior doc with no
			// readable path), we leave the new video windowed rather than
			// risk presenting the wrong document — Chi can ⌘Ctrl+F manually.
			if (fullscreen) {
				const escaped = recPath.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
				// During Zoom screen-share, Zoom's floating control bar holds the
				// foreground z-order — QuickTime's 'present' Apple event succeeds
				// but the visible window doesn't come forward, so the user never
				// sees fullscreen. Send Ctrl+Cmd+F (Enter Full Screen) routed
				// through 'tell process "QuickTime Player"' so System Events
				// targets QT directly instead of going through global keystroke
				// focus that Zoom is grabbing — same pattern as the Chrome fix.
				const presentScript = [
					`set targetPath to "${escaped}"`,
					'tell application "QuickTime Player"',
					'  activate',
					'  set foundDoc to missing value',
					'  repeat 30 times',
					'    try',
					'      repeat with d in documents',
					'        if (POSIX path of (path of d)) is targetPath then',
					'          set foundDoc to d',
					'          exit repeat',
					'        end if',
					'      end repeat',
					'    end try',
					'    if foundDoc is not missing value then exit repeat',
					'    delay 0.1',
					'  end repeat',
					'end tell',
					'delay 0.3',
					'tell application "System Events"',
					'  tell process "QuickTime Player"',
					'    set frontmost to true',
					'    keystroke "f" using {command down, control down}',
					'  end tell',
					'end tell',
				].join('\n');
				try {
					execFileSync('/usr/bin/osascript', ['-e', presentScript], { timeout: 5_000 });
				} catch (err) {
					console.log(`${ts()} [OpenFile] fullscreen AppleScript failed (non-fatal): ${err}`);
				}
			}
			const size = statSync(recPath).size;
			let duration_seconds: number | null = null;
			try {
				const dur = execFileSync(
					'/opt/homebrew/bin/ffprobe',
					['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', recPath],
					{ timeout: 5_000 }
				).toString().trim();
				duration_seconds = Math.round(parseFloat(dur));
			} catch {}
			console.log(`${ts()} [OpenFile] opened ${recPath} (${(size / 1024 / 1024).toFixed(1)}MB, ${duration_seconds ?? '?'}s)`);
			return {
				status: 'opened',
				path: recPath,
				size_mb: +(size / 1024 / 1024).toFixed(1),
				duration_seconds,
				instruction: 'File opened. When user says play, call play_video.',
			};
		} catch (err) {
			return { error: `open_file failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

/** Helper: start QuickTime playback + stream audio to phone */
async function startPlayback(seekSec: number = 0): Promise<{ status: string; path?: string; error?: string; instruction?: string }> {
	let recPath: string | null = null;
	try { recPath = readFileSync('/tmp/sutando-playback-path', 'utf8').trim() || null; } catch {}
	if (!recPath) recPath = findRecording();
	if (!recPath) return { status: 'error', error: 'No video to play. Open a video first with open_video.' };
	let alreadyOpen = false;
	try {
		const c = execSync(`osascript -e 'tell application "QuickTime Player" to count of documents'`, { timeout: 2_000 }).toString().trim();
		alreadyOpen = parseInt(c) > 0;
	} catch {}
	if (!alreadyOpen) {
		execSync(`open "${recPath}"`, { timeout: 5_000 });
		for (let i = 0; i < 10; i++) {
			try { const c = execSync(`osascript -e 'tell application "QuickTime Player" to count of documents'`, { timeout: 2_000 }).toString().trim(); if (parseInt(c) > 0) break; } catch {}
			await new Promise(r => setTimeout(r, 300));
		}
	}
	if (seekSec === 0) {
		try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'set d to document 1' -e 'set current time of d to 0' -e 'end tell'`, { timeout: 3_000 }); } catch {}
	}
	try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
	fetch(`http://localhost:${process.env.PHONE_PORT || '3100'}/play-audio`, {
		method: 'POST', headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ path: recPath, seekSec }),
	}).catch(() => {});
	await new Promise(r => setTimeout(r, 300));
	try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'activate' -e 'play document 1' -e 'end tell'`, { timeout: 5_000 }); } catch {}
	return { status: 'playing', path: recPath, instruction: 'Video is playing. Say NOTHING.' };
}

let lastResumeTime = 0;

export const playVideoTool: ToolDefinition = {
	name: 'play_video',
	description: 'Play the video from the beginning. Use ONLY when user explicitly says "play" or "play it".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [PlayVideo] called`);
		lastResumeTime = Date.now(); // Set cooldown on play to prevent auto-pause
		try { return await startPlayback(0); } catch (err) { return { error: `${err}` }; }
	},
};

export const resumeVideoTool: ToolDefinition = {
	name: 'resume_video',
	description: 'Resume the paused video from where it stopped. Use ONLY when user says "resume", "continue", "go on".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [ResumeVideo] called`);
		// Only resume if user said "resume"/"continue"/"go on"/"play" in recent transcript.
		// Picks freshest of voice-agent vs phone transcript; fail-open if neither is fresh.
		const recent = getRecentUserSpeech();
		if (recent && !/\b(resume|continue|go on|play it|play the)\b/.test(recent)) {
			console.log(`${ts()} [ResumeVideo] BLOCKED — no resume keyword in recent user speech: "${recent.slice(-80)}"`);
			return { status: 'paused', instruction: 'Video is still paused. Only resume when user explicitly says "resume" or "play".' };
		}
		try {
			try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
			lastResumeTime = Date.now();
			try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'activate' -e 'play document 1' -e 'end tell'`, { timeout: 5_000 }); } catch {}
			// Restart audio stream to phone at current position
			let seekSec = 0;
			try {
				seekSec = parseFloat(execSync(`osascript -e 'tell application "QuickTime Player" to get current time of document 1'`, { timeout: 3_000 }).toString().trim()) || 0;
			} catch {}
			let recPath = '';
			try { recPath = findRecording() || ''; } catch {}
			if (recPath) {
				fetch(`http://localhost:${process.env.PHONE_PORT || '3100'}/play-audio`, {
					method: 'POST', headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ path: recPath, seekSec }),
				}).catch(() => {});
				await new Promise(r => setTimeout(r, 300));
			}
			return { status: 'playing', instruction: 'Video resumed. Say NOTHING.' };
		} catch (err) { return { error: `${err}` }; }
	},
};

export const replayVideoTool: ToolDefinition = {
	name: 'replay_video',
	description: 'Replay the video from the beginning. Use when user says "start over", "replay", "play again".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [ReplayVideo] called`);
		try { return await startPlayback(0); } catch (err) { return { error: `${err}` }; }
	},
};

// "continue" intentionally NOT in pause_video — it belongs on resume_video.
// Adding it here caused Gemini to pause when user said "continue".
export const pauseVideoTool: ToolDefinition = {
	name: 'pause_video',
	description:
		'Pause the video. Use when user says "pause", "stop", or "hold".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [PauseVideo] called`);
		// Block pause for 8s after play/resume to prevent Gemini from hearing video audio and auto-pausing
		const sinceLast = Date.now() - lastResumeTime;
		if (sinceLast < 8000) {
			console.log(`${ts()} [PauseVideo] BLOCKED — ${sinceLast}ms since play/resume (cooldown 8s)`);
			return { status: 'playing', instruction: 'Video is still playing. Do NOT pause unless user explicitly says "pause" or "stop".' };
		}
		// Mirror resume_video's runtime guard: only pause if the user actually
		// said a pause keyword recently. Without this, a Gemini hallucination
		// outside the 8s cooldown still fires (Susan's 2026-04-16 report).
		// Picks freshest of voice-agent vs phone transcript; fail-open if neither is fresh.
		const recent = getRecentUserSpeech();
		if (recent && !/\b(pause|stop|hold|wait)\b/.test(recent)) {
			console.log(`${ts()} [PauseVideo] BLOCKED — no pause keyword in recent user speech: "${recent.slice(-80)}"`);
			return { status: 'playing', instruction: 'Video is still playing. Only pause when user explicitly says "pause" or "stop".' };
		}
		try { writeFileSync('/tmp/sutando-playback-pause', '1'); } catch {}
		try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'if (count of documents) > 0 then' -e 'pause document 1' -e 'end if' -e 'end tell'`, { timeout: 5_000 }); } catch {}
		return { status: 'paused', instruction: 'Paused. When user says play/resume, call play_video.' };
	},
};

export const closeVideoTool: ToolDefinition = {
	name: 'close_video',
	description:
		'"close the video" (Cmd+W, app=QuickTime Player). Instant — do NOT use work for simple keystrokes. NEVER use Cmd+Q to close QuickTime — use Cmd+W to close the window only.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [CloseVideo] called`);
		try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'activate' -e 'end tell' -e 'delay 0.3' -e 'tell application "System Events" to keystroke "w" using command down'`, { timeout: 5_000 }); } catch {}
		try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
		try { unlinkSync('/tmp/sutando-playback-path'); } catch {}
		return { status: 'closed' };
	},
};

// --- Screen recording ---

let lastScreenRecordCall = 0;
const SCREEN_RECORD_COOLDOWN_MS = 5_000;

export const screenRecordTool: ToolDefinition = {
	name: 'screen_record',
	description:
		'Start or stop PLAIN screen recording (no narration, no auto-scroll). ' +
		'Use ONLY when user explicitly says "start recording", "record the screen", or "screen record". ' +
		'Do NOT match on "fullscreen", "full screen", "play fullscreen", "make it full screen", or any cue with "screen" that is not preceded by "record" — those go to fullscreen_presenter or play_video. ' +
		'Do NOT use record_screen_with_narration for plain recording requests. ' +
		'Uses ffmpeg avfoundation for reliable .mov output. ' +
		'When starting, ASK the user if they want live transcript subtitles burned into the recording.',
	parameters: z.object({
		action: z.enum(['start', 'stop']).describe('"start" begins recording, "stop" stops and saves the file'),
		duration_seconds: z.number().optional().describe('If provided with start, auto-stops after this many seconds.'),
		subtitle: z.boolean().optional().describe('If true, burn live conversation transcript as subtitles into the recording. Ask the user before setting this.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, duration_seconds, subtitle } = args as { action: 'start' | 'stop'; duration_seconds?: number; subtitle?: boolean };
		// Hard block: if already recording, refuse to start again
		if (action === 'start' && demoStateRef.value === 'recording') {
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
				demoStateRef.value = 'recording';
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
					demoStateRef.value = 'done';
					liveTranscriptRecordingStart = 0;
					console.log(`${ts()} [ScreenRecord] auto-stop after ${capped}s (requested ${duration_seconds}s)`);
				}, capped * 1000);
			}
			if (action === 'stop') {
				demoStateRef.value = 'done';
				const parsed = JSON.parse(result);
				// Build explicit file list so the model knows exactly what's available.
				// The model should pass the recommended path to open_file — no findRecording guessing.
				const files: { raw?: string; narrated?: string; subtitled?: string; recommended?: string } = {};
				if (parsed.path && parsed.exists) {
					files.raw = parsed.path;
					const narrated = parsed.path.replace('.mov', '-narrated.mov');
					if (isReadableFile(narrated)) files.narrated = narrated;
					// Burn live transcript subtitles only if enabled at start
					if (liveTranscriptRecordingStart > 0) {
						const subtitled = burnLiveTranscriptSubtitles(isReadableFile(narrated) ? narrated : parsed.path);
						if (subtitled) {
							files.subtitled = subtitled;
							console.log(`${ts()} [ScreenRecord] transcript subtitles: ${subtitled}`);
						}
					}
					// Recommend best available: subtitled > narrated > raw
					files.recommended = files.subtitled || files.narrated || files.raw;
				}
				liveTranscriptRecordingStart = 0;
				console.log(`${ts()} [ScreenRecord] ${action}: ${JSON.stringify({ ...parsed, files })}`);
				return {
					...parsed,
					files,
					instruction: files.recommended
						? `Recording stopped. Available files: ${Object.entries(files).map(([k,v]) => `${k}=${v}`).join(', ')}. To open, call open_file with path="${files.recommended}". If user wants a different version, use the appropriate path from the list.`
						: 'Recording stopped but no files found.',
				};
			}
			const parsed = JSON.parse(result);
			console.log(`${ts()} [ScreenRecord] ${action}: ${result}`);
			return parsed;
		} catch (err) {
			return { error: `screen_record failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

/**
 * Set up all recording hooks on a voice session.
 * Call once per session — handles tool triggers, reconnect, and cleanup automatically.
 */
let _narrationSession: any = null;

/** Exposed for voice-agent to call when speech finishes and pre-capture is ready */
export let _tryInjectNow: (() => void) | null = null;

export function setupRecordingHooks(session: any): void {
	_narrationSession = session;
	// Start narration when record_screen_with_narration is called
	session.eventBus?.subscribe?.('tool.call', (e: any) => {
		if (e?.toolName === 'record_screen_with_narration') {
			console.log(`${ts()} [RecordingHooks] tool.call event for record_screen_with_narration`);
			setTimeout(() => {
				if (isRecordingActive()) startRecordingNarration(session);
			}, 4000);
		}
	});
}

/** Called on Gemini reconnect — nudge to continue narrating if recording active */
export function onReconnect(session: any): void {
	if (!isRecordingActive()) return;
	try {
		injectText(session, '[System: You were narrating a screen demo. Continue where you left off — call describe_screen and keep narrating. Do NOT greet or say "I\'m back".]');
	} catch {}
}

/** Called on call end — stop any active recording */
export function onCallEnd(): void {
	stopActiveRecording();
}

/**
 * Start narration controller for an active recording.
 * Called by conversation-server when record_screen_with_narration starts.
 * Handles: description pushing, stop detection, mute/unmute, reconnect narration.
 */
let narrationActive = false;

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
	const STOP_PUSHING_BEFORE_END_MS = 8000;

	// Set speaking flag — first description is spoken from tool return
	narrationSpeakingRef.value = true;
	nextDescRef.value = null;
	let lastPushTime = Date.now();
	const MAX_SPEAKING_TIME = 8000; // force-clear after 8s if onTurnCompleted hasn't fired

	// Pre-capture: scroll to next viewport, then capture + describe.
	// Screen stays still during speech; scroll happens here between narrations.
	const preCapture = async () => {
		if (!existsSync('/tmp/sutando-screen-record.pid')) return;
		try {
			// Scroll one viewport worth to reveal new content, then pause + capture
			scrollPausedRef.value = false;
			scrollDown(1400); // ~1.5 viewports — ensures mostly new content visible
			await new Promise(r => setTimeout(r, 500)); // let scroll settle
			scrollPausedRef.value = true;
			console.log(`${ts()} [Recording] pre-capture: scrolled + capturing...`);
			const path = await captureScreen();
			if (!path) { scrollPausedRef.value = false; return; }
			const desc = await describeScreenshot(path, previousDescs);
			if (desc && desc !== lastDesc) {
				nextDescRef.value = desc;
				console.log(`${ts()} [Recording] pre-captured: ${desc.slice(0, 60)}...`);
				// Try injecting immediately if Gemini already finished speaking
				if (!narrationSpeakingRef.value) {
					console.log(`${ts()} [Recording] Gemini idle — injecting immediately`);
					tryInject();
				}
			} else {
				nextDescRef.value = null;
				scrollPausedRef.value = false; // resume scroll if nothing new
			}
			// scroll stays paused until Gemini finishes speaking + we inject
		} catch (err) {
			scrollPausedRef.value = false;
			console.log(`${ts()} [Recording] pre-capture error: ${err}`);
		}
	};

	// Called by interval — if pre-captured desc is ready and Gemini finished speaking, inject it
	const tryInject = () => {
		_tryInjectNow = tryInject; // expose for voice-agent callback
		if (!existsSync('/tmp/sutando-screen-record.pid')) return;
		// Force-clear speaking flag after MAX_SPEAKING_TIME to prevent deadlock
		if (narrationSpeakingRef.value && (Date.now() - lastPushTime) > MAX_SPEAKING_TIME) {
			console.log(`${ts()} [Recording] force-clearing speaking flag (${MAX_SPEAKING_TIME}ms timeout)`);
			narrationSpeakingRef.value = false;
		}
		if (narrationSpeakingRef.value) return; // still speaking
		if (!nextDescRef.value) {
			// No pre-capture ready — start one
			if (!scrollPausedRef.value) preCapture();
			return;
		}
		const elapsed = Date.now() - startTime;
		const stopBefore = Math.min(STOP_PUSHING_BEFORE_END_MS, durationMs * 0.25);
		if (elapsed > durationMs - stopBefore) {
			console.log(`${ts()} [Recording] near end — stopped pushing`);
			clearInterval(descTimer);
			scrollPausedRef.value = false;
			try { injectText(session, '[System: Recording ending soon. Finish your current sentence and stop.]'); } catch {}
			return;
		}
		// Inject the pre-captured description
		let desc = nextDescRef.value!;
		nextDescRef.value = null;
		lastDesc = desc;
		previousDescs.push(desc);
		const remaining = Math.round((durationMs - (Date.now() - startTime)) / 1000);
		const lastSaid = lastSpokenRef.value || '(first description)';
		narrationSpeakingRef.value = true;
		lastPushTime = Date.now();
		// Screen stays on the captured content while Gemini narrates it — no scroll during speech.
		// Scroll will advance AFTER speech finishes (in preCapture, which scrolls → captures → describes).
		injectText(session, `[System: Narrate what's new on screen. You just said: "${lastSaid}". The screen now shows: "${desc}". DO NOT read this description verbatim — rephrase it in your own words as a natural continuation. One sentence, ~5 seconds. Do NOT say "anything else", "can I help", "is there", or any conversational filler — ONLY describe what is on screen.]`);
		console.log(`${ts()} [Recording] pushed: ${desc}`);
		// Start pre-capturing next while Gemini speaks this one
		setTimeout(preCapture, 2000);
	};

	// First pre-capture: wait 3s for first desc to start being spoken, then
	// scroll to next viewport + capture while Gemini speaks
	setTimeout(preCapture, 3000);
	const descTimer = setInterval(tryInject, descIntervalMs);

	setTimeout(() => {
		clearInterval(descTimer);
		narrationActive = false;
		console.log(`${ts()} [Recording] timer fired — sending stop`);
		try {
			injectText(session, '[System: Recording just ended. Say "The recording is complete." immediately.]');
		} catch {}
	}, durationMs + 1000);
}
