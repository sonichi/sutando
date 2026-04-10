/**
 * Meeting tools — Zoom, Google Meet, phone dial-in, video playback sync.
 * Renamed from summon-tools.ts for clarity.
 */

import { execSync } from 'node:child_process';
import { writeFileSync, readFileSync, unlinkSync, existsSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { findRecording, isReadableFile } from './recording-tools.js';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

const ZOOM_PMI = process.env.ZOOM_PERSONAL_MEETING_ID ?? '';

const ZOOM_PASSCODE = process.env.ZOOM_PERSONAL_PASSCODE ?? '';

const PHONE_PORT = Number(process.env.PHONE_PORT) || 3100;
const ZOOM_DEFAULT_SHARE_SCREEN = process.env.ZOOM_DEFAULT_SHARE_SCREEN !== 'false'; // default true

// --- Video playback tools ---

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

export const playVideoInMeetingTool: ToolDefinition = {
	name: 'play_video_in_meeting',
	description: 'Play the video from the beginning. Use ONLY when user explicitly says "play" or "play it".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [PlayVideo] called`);
		try { return await startPlayback(0); } catch (err) { return { error: `${err}` }; }
	},
};

export const resumeVideoInMeetingTool: ToolDefinition = {
	name: 'resume_video_in_meeting',
	description: 'Resume the paused video from where it stopped. Use ONLY when user says "resume", "continue", "go on".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [ResumeVideo] called`);
		try {
			try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
			try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'activate' -e 'play document 1' -e 'end tell'`, { timeout: 5_000 }); } catch {}
			return { status: 'playing', instruction: 'Video resumed. Say NOTHING.' };
		} catch (err) { return { error: `${err}` }; }
	},
};

// "continue" intentionally NOT in pause_video — it belongs on resume_video.
// Adding it here caused Gemini to pause when user said "continue".
export const pauseVideoInMeetingTool: ToolDefinition = {
	name: 'pause_video_in_meeting',
	description:
		'Pause the video. Use when user says "pause", "stop", or "hold".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [PauseVideo] called`);
		try { writeFileSync('/tmp/sutando-playback-pause', '1'); } catch {}
		try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'if (count of documents) > 0 then' -e 'pause document 1' -e 'end if' -e 'end tell'`, { timeout: 5_000 }); } catch {}
		return { status: 'paused', instruction: 'Paused. When user says play/resume, call play_video.' };
	},
};

// --- Summon (Zoom + screen share) ---

export const summonTool: ToolDefinition = {
	name: 'summon',
	description:
		'Summon Sutando\'s screen — opens Zoom with screen sharing so the user can see and control remotely. ' +
		'Use when user says "summon", "share my screen", "start zoom", "let me see your screen". ' +
		'Instant — do NOT use work for this.' +
		(ZOOM_PMI ? ` Default meeting: ${ZOOM_PMI}.` : ''),
	parameters: z.object({
		meetingId: z.string().optional().describe('Zoom meeting ID. Omit for personal room.'),
		passcode: z.string().optional().describe('Passcode. Omit for personal room.'),
		shareScreen: z.boolean().optional().describe('Share screen after joining (default: true)'),
		dialIn: z.boolean().optional().describe('Also dial into the meeting via phone for voice (default: false). Only if user explicitly asks.'),
	}),
	execution: 'inline',
	async execute(args, ctx) {
		const { meetingId, passcode, shareScreen = ZOOM_DEFAULT_SHARE_SCREEN, dialIn = false } = args as { meetingId?: string; passcode?: string; shareScreen?: boolean; dialIn?: boolean };
		const pwd = passcode ?? ZOOM_PASSCODE;
		const cleanId = (meetingId ?? ZOOM_PMI).replace(/\D/g, '');
		if (!cleanId || cleanId.length < 6) return { error: `Invalid meeting ID: "${meetingId}"` };

		try {
			// Check if already in a Zoom meeting
			let alreadyInMeeting = false;
			try {
				const winNames = execSync(`osascript -e 'tell application "System Events" to return name of every window of process "zoom.us"'`, { timeout: 3_000 }).toString().trim();
				alreadyInMeeting = winNames.includes('Zoom Meeting') || winNames.includes('zoom share') || winNames.includes('floating video');
			} catch {}

			if (alreadyInMeeting) {
				console.log(`${ts()} [Summon] Already in a Zoom meeting — skipping join, going straight to screen share`);
			} else {
				// Use the running Zoom app if available — avoids login prompt from zoommtg:// protocol
				const zoomRunning = (() => { try { execSync('pgrep -f "zoom.us"', { timeout: 2_000 }); return true; } catch { return false; } })();

				if (zoomRunning) {
					console.log(`${ts()} [Summon] Zoom running — joining via app`);
					const joinUrl = `https://zoom.us/j/${cleanId}${pwd ? '?pwd=' + pwd : ''}`;
					execSync(`open "${joinUrl}"`, { timeout: 10_000 });
				} else {
					console.log(`${ts()} [Summon] Launching Zoom`);
					let zoomUrl = `zoommtg://zoom.us/join?confno=${cleanId}`;
					if (pwd) zoomUrl += `&pwd=${pwd}`;
					execSync(`open "${zoomUrl}"`, { timeout: 10_000 });
				}

				// Wait for Zoom preview window, then click Join button
				console.log(`${ts()} [Summon] Waiting for Zoom preview window...`);
			await new Promise(r => setTimeout(r, 2000));
			try {
				// Click Join button using cliclick (no Quartz dependency needed)
				const previewCoords = execSync(`osascript -e '
					tell application "zoom.us" to activate
					tell application "System Events"
						tell process "zoom.us"
							repeat with w in windows
								try
									set wName to name of w
									if wName contains "Meeting" or wName contains "Personal" or wName contains "Preview" then
										set wPos to position of w
										set wSize to size of w
										return (item 1 of wPos as text) & "," & (item 2 of wPos as text) & "," & (item 1 of wSize as text) & "," & (item 2 of wSize as text)
									end if
								end try
							end repeat
						end tell
					end tell
					return "not_found"
				'`, { timeout: 10_000 }).toString().trim();
				if (previewCoords !== 'not_found') {
					const [px, py, pw, ph] = previewCoords.split(',').map(Number);
					const jx = px + Math.round(pw * 0.80);
					const jy = py + Math.round(ph * 0.93);
					try { execSync(`cliclick c:${jx},${jy}`, { timeout: 3_000 }); } catch {}
					console.log(`${ts()} [Summon] Join button clicked at (${jx},${jy})`);
				} else {
					console.log(`${ts()} [Summon] No preview window — may have auto-joined`);
				}
			} catch (err) {
				console.log(`${ts()} [Summon] Join click failed (may have auto-joined): ${err}`);
			}
			} // end: not already in meeting

			// If phone dial-in requested, wait for host to join before dialing
			if (dialIn) {
				console.log(`${ts()} [Summon] Waiting for desktop to join as host...`);
				let hostJoined = false;
				for (let i = 0; i < 20; i++) {
					try {
						const winNames = execSync(`osascript -e 'tell application "System Events" to return name of every window of process "zoom.us"'`, { timeout: 3_000 }).toString().trim();
						if (winNames.includes('Zoom Meeting') || winNames.includes('Meeting')) {
							hostJoined = true;
							break;
						}
					} catch {}
					await new Promise(r => setTimeout(r, 1000));
				}
				console.log(`${ts()} [Summon] Host joined: ${hostJoined}`);
				if (hostJoined) {
					console.log(`${ts()} [Summon] Waiting 3s for Zoom server to register host...`);
					await new Promise(r => setTimeout(r, 3000));
				}
			}

			// Phone dial-in only when explicitly requested (not all meetings support it)
			let phoneJoined = false;
			if (dialIn) try {
				const ping = await fetch(`http://localhost:${PHONE_PORT}/health`, { signal: AbortSignal.timeout(2000) });
				if (ping.ok) {
					console.log(`${ts()} [Summon] Phone server available — dialing into meeting for voice`);
					const res = await fetch(`http://localhost:${PHONE_PORT}/meeting`, {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ meetingId: cleanId, passcode: pwd, platform: 'zoom' }),
					});
					const data = await res.json() as { callSid?: string; error?: string };
					if (data.callSid) {
						phoneJoined = true;
						console.log(`${ts()} [Summon] Phone call placed: ${data.callSid} — voice agent stays connected until phone joins`);
						// Mute Zoom mic + speaker so voice agent doesn't pick up Zoom audio
						try {
							execSync(`osascript -e '
								tell application "System Events"
									tell process "zoom.us"
										-- Mute mic (Cmd+Shift+A)
										keystroke "a" using {command down, shift down}
									end tell
								end tell
								-- Mute system audio so Zoom speaker doesn\'t bleed into voice agent mic
								set volume output volume 0
							'`, { timeout: 5_000 });
							console.log(`${ts()} [Summon] Zoom mic + system audio muted`);
						} catch { console.log(`${ts()} [Summon] Zoom mute failed`); }
						// Voice agent stays alive — system audio muted prevents Zoom speaker
						// from being picked up by voice agent mic
					} else {
						console.log(`${ts()} [Summon] Phone join failed: ${data.error}`);
					}
				}
			} catch {
				console.log(`${ts()} [Summon] Phone server not available — screen share only`);
			}

			// Handle audio dialogs
			await new Promise(r => setTimeout(r, 1000));
			if (dialIn) {
				// Phone handles audio — close the "Join audio" window
				try {
					execSync(`osascript -e '
						tell application "zoom.us" to activate
						delay 0.5
						tell application "System Events"
							tell process "zoom.us"
								repeat with w in windows
									if name of w is "Join audio" then
										click button 1 of w
										return "closed Join audio"
									end if
								end repeat
							end tell
						end tell
					'`, { timeout: 5_000 });
					console.log(`${ts()} [Summon] Join audio window closed (phone handles audio)`);
				} catch { console.log(`${ts()} [Summon] No Join audio window to close`); }
			} else {
				// No phone dial-in — handle audio dialog
				// Detect if machine has audio input (mic)
				const hasMic = (() => { try { return execSync(`system_profiler SPAudioDataType 2>/dev/null | grep -c "Input"`, { timeout: 5_000 }).toString().trim() !== '0'; } catch { return false; } })();
				console.log(`${ts()} [Summon] Audio input detected: ${hasMic}`);

				// Handle the audio dialogs using cliclick for reliable clicking
				// Zoom uses web-based UI that AppleScript can't access by button name
				for (let attempt = 0; attempt < 3; attempt++) {
					await new Promise(r => setTimeout(r, 1500));
					try {
						// Get the audio dialog window position
						const coords = execSync(`osascript -e '
							tell application "System Events"
								tell process "zoom.us"
									repeat with w in windows
										set wName to name of w
										if wName contains "audio" or wName contains "Audio" then
											set wPos to position of w
											set wSize to size of w
											return (item 1 of wPos as text) & "," & (item 2 of wPos as text) & "," & (item 1 of wSize as text) & "," & (item 2 of wSize as text)
										end if
									end repeat
								end tell
							end tell
							return "none"
						'`, { timeout: 5_000 }).toString().trim();

						if (coords === 'none') {
							console.log(`${ts()} [Summon] No audio dialog found (attempt ${attempt + 1})`);
							break;
						}

						const [x0, y0, w, h] = coords.split(',').map(Number);
						if (hasMic) {
							// Click "Join with Computer Audio" — centered blue button, ~50% across, ~55% down
							const bx = x0 + Math.round(w * 0.5);
							const by = y0 + Math.round(h * 0.55);
							execSync(`cliclick c:${bx},${by}`, { timeout: 3_000 });
							console.log(`${ts()} [Summon] Clicked Join with Computer Audio at (${bx},${by})`);
						} else {
							// No mic — first click dismisses to "Continue without audio?" dialog
							// Then click "Continue" (left button, ~40% across, ~75% down)
							const bx = x0 + Math.round(w * 0.5);
							const by = y0 + Math.round(h * 0.55);
							execSync(`cliclick c:${bx},${by}`, { timeout: 3_000 });
							console.log(`${ts()} [Summon] Clicked audio dialog at (${bx},${by}), checking for confirmation...`);
							await new Promise(r => setTimeout(r, 1000));
							// Check if "continue without audio" confirmation appeared
							try {
								const coords2 = execSync(`osascript -e '
									tell application "System Events"
										tell process "zoom.us"
											repeat with w in windows
												set wName to name of w
												if wName contains "audio" or wName contains "Audio" then
													set wPos to position of w
													set wSize to size of w
													return (item 1 of wPos as text) & "," & (item 2 of wPos as text) & "," & (item 1 of wSize as text) & "," & (item 2 of wSize as text)
												end if
											end repeat
										end tell
									end tell
									return "none"
								'`, { timeout: 5_000 }).toString().trim();
								if (coords2 !== 'none') {
									const [x2, y2, w2, h2] = coords2.split(',').map(Number);
									// "Continue" button is left of center, ~38% across, ~72% down
									const cx = x2 + Math.round(w2 * 0.38);
									const cy = y2 + Math.round(h2 * 0.72);
									execSync(`cliclick c:${cx},${cy}`, { timeout: 3_000 });
									console.log(`${ts()} [Summon] Clicked Continue (no audio) at (${cx},${cy})`);
								}
							} catch {}
						}
						break;
					} catch (e) {
						console.log(`${ts()} [Summon] Audio dialog handling attempt ${attempt + 1} failed: ${e}`);
					}
				}
			}

			// Wait for Zoom meeting window to appear (adaptive, up to 30s)
			console.log(`${ts()} [Summon] Waiting for Zoom meeting window...`);
			let zoomReady = false;
			for (let i = 0; i < 30; i++) {
				try {
					const check = execSync(`osascript -e 'tell application "System Events" to return (count of windows of process "zoom.us")'`, { timeout: 3_000 }).toString().trim();
					if (parseInt(check) > 0) { zoomReady = true; break; }
				} catch {}
				await new Promise(r => setTimeout(r, 1000));
			}

			if (shareScreen && zoomReady) {
				console.log(`${ts()} [Summon] Zoom ready — sharing screen...`);
				try {
					// Screen share: try menu bar first (most reliable), fall back to keyboard shortcut
					execSync(`osascript -e '
						tell application "zoom.us" to activate
						delay 2
						-- Try menu bar: Meeting > Share Screen (most reliable)
						try
							tell application "System Events"
								tell process "zoom.us"
									click menu item "Share Screen" of menu "Meeting" of menu bar 1
								end tell
							end tell
						on error
							-- Fallback: keyboard shortcut
							tell application "System Events"
								tell process "zoom.us"
									keystroke "s" using {command down, shift down}
								end tell
							end tell
						end try
						delay 3
						-- Enable "Share sound" checkbox so computer audio goes through Zoom
						tell application "System Events"
							tell process "zoom.us"
								try
									set soundCB to checkbox "Share sound" of window 1
									if value of soundCB is 0 then click soundCB
								end try
							end tell
						end tell
						delay 0.5
						-- If share dialog appeared, click Share button or press Enter
						tell application "System Events"
							tell process "zoom.us"
								try
									-- Look for Share button in the share dialog
									set shareButtons to buttons of window 1 whose title is "Share"
									if (count of shareButtons) > 0 then
										click item 1 of shareButtons
									else
										keystroke return
									end if
								on error
									keystroke return
								end try
							end tell
						end tell
					'`, { timeout: 15_000 });
					console.log(`${ts()} [Summon] Screen share started`);
					// Handle "audio conference" panel after screen share
					await new Promise(r => setTimeout(r, 2000));
					if (dialIn) {
						// Phone handles audio — dismiss the panel
						try {
							execSync(`osascript -e '
								tell application "System Events"
									tell process "zoom.us"
										repeat with w in windows
											if name of w contains "audio conference" then
												set focused of w to true
												keystroke "w" using command down
												return "closed"
											end if
										end repeat
										return "not found"
									end tell
								end tell
							'`, { timeout: 5_000 });
							console.log(`${ts()} [Summon] Audio conference panel dismissed (phone handles audio)`);
						} catch {
							console.log(`${ts()} [Summon] No audio conference panel to dismiss`);
						}
					} else {
						// No phone — join computer audio
						try {
							execSync(`osascript -e '
								tell application "System Events"
									tell process "zoom.us"
										repeat with w in windows
											if name of w contains "audio conference" then
												try
													click button "Join with Computer Audio" of w
													return "joined computer audio"
												end try
												repeat with b in buttons of w
													if name of b contains "Computer Audio" then
														click b
														return "joined computer audio"
													end if
												end repeat
												set focused of w to true
												keystroke "w" using command down
												return "closed (no computer audio button)"
											end if
										end repeat
										return "not found"
									end tell
								end tell
							'`, { timeout: 5_000 });
							console.log(`${ts()} [Summon] Audio conference panel — joined computer audio`);
						} catch {
							console.log(`${ts()} [Summon] No audio conference panel found`);
						}
					}
				} catch (err) {
					console.log(`${ts()} [Summon] Screen share failed: ${err}`);
				}
			} else if (shareScreen) {
				console.log(`${ts()} [Summon] Zoom window not detected after 30s — skipping screen share`);
			}

			// Mute Zoom audio after joining. Zoom presents two choices on entry:
			// "Join Audio" or "Test Speaker & Microphone" (ringtone test). With
			// "Automatically join computer audio" enabled, it skips the dialog and
			// joins audio directly — avoiding the ringtone test. But audio is now
			// live, so we must mute immediately. Phone handles audio via Twilio.
			try {
				execSync(`osascript -e '
					tell application "zoom.us" to activate
					delay 0.5
					tell application "System Events"
						tell process "zoom.us"
							click menu item "Mute audio" of menu "Meeting" of menu bar 1
						end tell
					end tell
				'`, { timeout: 5_000 });
				console.log(`${ts()} [Summon] Muted Zoom audio (phone handles audio)`);
			} catch {
				console.log(`${ts()} [Summon] Could not mute Zoom audio`);
			}

			return {
				status: 'summoned',
				meetingId: cleanId,
				screenShare: shareScreen,
				phoneAgent: phoneJoined,
				instruction: phoneJoined
					? 'Screen is shared and Sutando is dialing in via phone. Voice stays connected.'
					: 'Zoom meeting joined with screen sharing and computer audio. Voice stays connected.',
			};
		} catch (err) {
			return { error: `Summon failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Dismiss — leave the current Zoom meeting
export const dismissTool: ToolDefinition = {
	name: 'dismiss',
	description:
		'Leave the current Zoom meeting. The opposite of summon/join_zoom. ' +
		'Use when user says "dismiss", "leave zoom", "end meeting", "leave the call", "hang up zoom".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			// 1. Stop screen share (Cmd+Shift+S), 2. Cmd+W leave dialog, 3. Enter confirm
			execSync(`osascript -e '
tell application "zoom.us"
	activate
end tell
delay 0.5
tell application "System Events"
	-- Stop screen share first
	keystroke "s" using {command down, shift down}
	delay 1
	-- Open leave dialog
	keystroke "w" using command down
	delay 1.5
	-- Confirm (Enter hits default "End meeting for all")
	key code 36
end tell'`, { timeout: 15_000 });
			// Verify — if Zoom still has meeting windows, force kill
			try {
				const check = execSync(`osascript -e 'tell application "System Events" to tell process "zoom.us" to return count of windows'`, { timeout: 3_000 }).toString().trim();
				if (parseInt(check) > 2) {
					execSync('killall "zoom.us" 2>/dev/null; sleep 1', { timeout: 5_000 });
					console.log(`${ts()} [Dismiss] Force killed Zoom (${check} windows remaining)`);
				}
			} catch {}
			console.log(`${ts()} [Dismiss] Left Zoom meeting`);
			return { status: 'left_meeting' };
		} catch (err) {
			return { error: `Dismiss failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Join Zoom via desktop app + computer audio (no screen share)
export const joinZoomTool: ToolDefinition = {
	name: 'join_zoom',
	description: 'Join a Zoom meeting via the desktop app with computer audio. No screen sharing. Use when user says "join the zoom", "join meeting", or provides a Zoom meeting ID.',
	parameters: z.object({
		meetingId: z.string().optional().describe('Zoom meeting ID. Omit for personal room.'),
		passcode: z.string().optional().describe('Meeting passcode. Omit for personal room.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { meetingId, passcode } = args as { meetingId?: string; passcode?: string };
		const pwd = passcode ?? ZOOM_PASSCODE;
		const cleanId = (meetingId ?? ZOOM_PMI).replace(/\D/g, '');
		if (!cleanId || cleanId.length < 6) return { error: `Invalid meeting ID: "${meetingId}"` };

		try {
			// Check if already in meeting
			let alreadyIn = false;
			try {
				const winNames = execSync(`osascript -e 'tell application "System Events" to return name of every window of process "zoom.us"'`, { timeout: 3_000 }).toString().trim();
				alreadyIn = winNames.includes('Zoom Meeting') || winNames.includes('zoom share');
			} catch {}

			if (!alreadyIn) {
				const zoomRunning = (() => { try { execSync('pgrep -f "zoom.us"', { timeout: 2_000 }); return true; } catch { return false; } })();
				if (zoomRunning) {
					execSync(`open "https://zoom.us/j/${cleanId}${pwd ? '?pwd=' + pwd : ''}"`, { timeout: 10_000 });
				} else {
					let zoomUrl = `zoommtg://zoom.us/join?confno=${cleanId}`;
					if (pwd) zoomUrl += `&pwd=${pwd}`;
					execSync(`open "${zoomUrl}"`, { timeout: 10_000 });
				}

				// Click Join button if preview window appears
				await new Promise(r => setTimeout(r, 3000));
				try {
					execSync(`/usr/bin/python3 -c "
import Quartz, subprocess, time
result = subprocess.run(['osascript', '-e', '''
tell application \\\"zoom.us\\\" to activate
tell application \\\"System Events\\\"
    tell process \\\"zoom.us\\\"
        repeat with w in windows
            try
                set wName to name of w
                if wName contains \\\"Meeting\\\" or wName contains \\\"Personal\\\" then
                    set wPos to position of w
                    set wSize to size of w
                    return (item 1 of wPos as text) & \\\",\\\" & (item 2 of wPos as text) & \\\",\\\" & (item 1 of wSize as text) & \\\",\\\" & (item 2 of wSize as text)
                end if
            end try
        end repeat
    end tell
end tell
'''], capture_output=True, text=True)
if result.stdout.strip():
    parts = result.stdout.strip().split(',')
    x, y, w, h = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
    bx = x + w * 0.5
    by = y + h * 0.85
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (bx, by), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
    time.sleep(0.05)
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (bx, by), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
"`, { timeout: 15_000 });
				} catch {}

				// Handle "Continue without audio?" dialog if it appears
				await new Promise(r => setTimeout(r, 1500));
				try {
					execSync(`osascript -e '
						tell application "System Events"
							tell process "zoom.us"
								repeat with w in windows
									if name of w contains "without audio" then
										click button 1 of w
										return "dismissed"
									end if
								end repeat
							end tell
						end tell
					'`, { timeout: 3_000 });
				} catch {}
			}

			// Click "Join with Computer Audio"
			await new Promise(r => setTimeout(r, 1000));
			try {
				execSync(`osascript -e '
					tell application "zoom.us" to activate
					delay 0.5
					tell application "System Events"
						tell process "zoom.us"
							repeat with w in windows
								if name of w is "Join audio" then
									try
										click button "Join with Computer Audio" of w
										return "joined"
									end try
									repeat with b in buttons of w
										if name of b contains "Computer Audio" then
											click b
											return "joined"
										end if
									end repeat
								end if
							end repeat
						end tell
					end tell
				'`, { timeout: 5_000 });
				console.log(`${ts()} [join_zoom] Joined computer audio`);
			} catch {}

			// Handle "audio conference" variant
			await new Promise(r => setTimeout(r, 500));
			try {
				execSync(`osascript -e '
					tell application "System Events"
						tell process "zoom.us"
							repeat with w in windows
								if name of w contains "audio conference" then
									try
										click button "Join with Computer Audio" of w
										return "joined"
									end try
									repeat with b in buttons of w
										if name of b contains "Computer Audio" then
											click b
											return "joined"
										end if
									end repeat
								end if
							end repeat
						end tell
					end tell
				'`, { timeout: 5_000 });
			} catch {}

			return { status: 'joined', meetingId: cleanId, method: 'computer_audio', instruction: 'Joined Zoom with computer audio. No screen sharing.' };
		} catch (err) {
			return { error: `join_zoom failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Join Google Meet via browser + computer audio
export const joinGmeetTool: ToolDefinition = {
	name: 'join_gmeet',
	description: 'Join a Google Meet meeting via browser with computer audio. Use when user says "join the meet" or provides a Google Meet link/code.',
	parameters: z.object({
		meetingCode: z.string().describe('Google Meet code (e.g., "abc-defg-hij") or full URL'),
	}),
	execution: 'inline',
	async execute(args) {
		const { meetingCode } = args as { meetingCode: string };
		// Extract code from URL or use as-is
		const code = meetingCode.replace(/^https?:\/\/meet\.google\.com\//, '').replace(/\?.*$/, '').trim();
		if (!code) return { error: 'Invalid meeting code' };

		const meetUrl = `https://meet.google.com/${code}`;

		try {
			// Open in Chrome
			execSync(`open -a "Google Chrome" "${meetUrl}"`, { timeout: 10_000 });
			console.log(`${ts()} [join_gmeet] Opened ${meetUrl} in Chrome`);

			// Wait for page to load
			await new Promise(r => setTimeout(r, 5000));

			// Focus the Meet tab and disable camera on preview screen
			try {
				execSync(`osascript -e '
					tell application "Google Chrome"
						set windowList to every window
						repeat with w in windowList
							set tabList to every tab of w
							set tabIdx to 1
							repeat with t in tabList
								if URL of t contains "meet.google.com" then
									set active tab index of w to tabIdx
									set index of w to 1
									activate
									return "focused"
								end if
								set tabIdx to tabIdx + 1
							end repeat
						end repeat
					end tell
				'`, { timeout: 5_000 });
			} catch {}

			// Disable camera by clicking the camera toggle button on the preview
			// The button is in the center-bottom of the preview area
			await new Promise(r => setTimeout(r, 1000));
			try {
				execSync(`/usr/bin/python3 -c "
import Quartz, subprocess, time

# Get Chrome window position and size
result = subprocess.run(['osascript', '-e', '''
tell application \\\"System Events\\\"
    tell process \\\"Google Chrome\\\"
        set winPos to position of front window
        set winSize to size of front window
        return (item 1 of winPos as text) & \\\",\\\" & (item 2 of winPos as text) & \\\",\\\" & (item 1 of winSize as text) & \\\",\\\" & (item 2 of winSize as text)
    end tell
end tell
'''], capture_output=True, text=True, timeout=5)

if result.stdout.strip():
    parts = result.stdout.strip().split(',')
    wx, wy, ww, wh = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
    # Camera button is roughly at 36% across, 68% down in the window
    cx = wx + ww * 0.36
    cy = wy + wh * 0.68
    # Click the camera button
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (cx, cy), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
    time.sleep(0.05)
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (cx, cy), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
    print(f'Clicked camera at ({cx},{cy})')
"`, { timeout: 10_000 });
				console.log(`${ts()} [join_gmeet] Camera button clicked`);
			} catch { console.log(`${ts()} [join_gmeet] Could not click camera button`); }

			await new Promise(r => setTimeout(r, 500));

			// Click Join now button
			try {
				execSync(`osascript -e '
					tell application "Google Chrome"
						tell active tab of front window
							execute javascript "
								const btns = document.querySelectorAll(\\\"button\\\");
								for (const b of btns) {
									if (b.textContent.includes(\\\"Join now\\\") || b.textContent.includes(\\\"Ask to join\\\")) {
										b.click();
										\\\"clicked\\\";
									}
								}
							"
						end tell
					end tell
				'`, { timeout: 10_000 });
				console.log(`${ts()} [join_gmeet] Clicked Join button`);
			} catch {
				await new Promise(r => setTimeout(r, 3000));
				try {
					execSync(`osascript -e '
						tell application "Google Chrome"
							tell active tab of front window
								execute javascript "
									const btns = document.querySelectorAll(\\\"button\\\");
									for (const b of btns) {
										if (b.textContent.includes(\\\"Join now\\\") || b.textContent.includes(\\\"Ask to join\\\")) {
											b.click();
											\\\"clicked\\\";
										}
									}
								"
							end tell
						end tell
					'`, { timeout: 10_000 });
				} catch {}
			}

			return { status: 'joined', meetingCode: code, method: 'browser_audio', instruction: 'Joined Google Meet via browser with computer audio. Camera off.' };
		} catch (err) {
			return { error: `join_gmeet failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Meeting ID lookup (inline, bypasses task bridge) ---

export const lookupMeetingIdTool: ToolDefinition = {
	name: 'lookup_meeting_id',
	description:
		'Look up the Zoom personal meeting ID from the environment. Instant — does NOT go through the task bridge. ' +
		'Use for: "what\'s the Zoom meeting ID", "find the meeting ID", "get the Zoom ID".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		const meetingId = process.env.ZOOM_PERSONAL_MEETING_ID;
		if (!meetingId) {
			return { error: 'No ZOOM_PERSONAL_MEETING_ID found in environment.' };
		}
		const passcode = process.env.ZOOM_PERSONAL_PASSCODE || process.env.ZOOM_PASSCODE || null;
		console.log(`${ts()} [LookupMeetingId] found: ${meetingId}${passcode ? ' (with passcode)' : ''}`);
		return { meetingId, passcode, source: 'ZOOM_PERSONAL_MEETING_ID from .env', instruction: passcode ? `Meeting ID: ${meetingId}, Passcode: ${passcode}. Include BOTH when telling someone to join.` : `Meeting ID: ${meetingId}. No passcode needed.` };
	},
};
