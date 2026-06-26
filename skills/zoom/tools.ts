/**
 * Zoom inline tools — summon, dismiss, join_zoom.
 *
 * Extracted from src/meeting-tools.ts (issue #786).  Core services import
 * these via src/inline-tools.ts re-export; they can also be imported
 * directly by skills that override or extend Zoom behaviour (an external
 * plugin since #1427 uses the dismiss-override pattern).
 */

import { execSync, execFileSync } from 'node:child_process';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

// Lazy env reads — ESM hoists imports before dotenv runs, so reading at
// import time gets empty values. These getters read at call time instead.
const getZoomPMI = () => process.env.ZOOM_PERSONAL_MEETING_ID ?? '';
const getZoomPasscode = () => process.env.ZOOM_PERSONAL_PASSCODE ?? '';
const getPhonePort = () => Number(process.env.PHONE_PORT) || 3100;
const getShareScreen = () => process.env.ZOOM_DEFAULT_SHARE_SCREEN !== 'false';

export const summonTool: ToolDefinition = {
	name: 'summon',
	description:
		'Summon Sutando\'s screen — opens Zoom with screen sharing so the user can see and control remotely. ' +
		'Use when user says "summon", "share my screen", "start zoom", "let me see your screen". ' +
		'Instant — do NOT use work for this.' +
		(getZoomPMI() ? ` Default meeting: ${getZoomPMI()}.` : ''),
	parameters: z.object({
		meetingId: z.string().optional().describe('Zoom meeting ID. Omit for personal room.'),
		passcode: z.string().optional().describe('Passcode. Omit for personal room.'),
		shareScreen: z.boolean().optional().describe('Share screen after joining (default: true)'),
		dialIn: z.boolean().optional().describe('Also dial into the meeting via phone for voice (default: false). Only if user explicitly asks.'),
	}),
	execution: 'inline',
	// Bodhi's default tool timeout is 30s; on Studio Zoom Workplace, "open
	// meeting after Join click" was observed at 29-31s. Per Mini's PR #546
	// review, the worst path can hit ~85s (Join up to 10s + 60s combined
	// readiness/Join-retry loop + share script 15s + mute/cleanup 10s + dial-in
	// host wait 20s). 120s gives meaningful margin without being absurd.
	timeout: 120_000,
	async execute(args, ctx) {
		const { meetingId, passcode, shareScreen = getShareScreen(), dialIn = false } = args as { meetingId?: string; passcode?: string; shareScreen?: boolean; dialIn?: boolean };
		const pwd = passcode ?? getZoomPasscode();
		const cleanId = (meetingId ?? getZoomPMI()).replace(/\D/g, '');
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
				// Always use the zoommtg:// deeplink — direct to the Zoom app. The
				// HTTPS https://zoom.us/j/... URL is unreliable on Zoom Workplace
				// (Studio): it routes through the browser and the in-app prompt
				// doesn't always fire, leaving Zoom on the home dashboard while the
				// rest of summonTool falsely treats "any zoom window" as ready and
				// fires Share Screen into a context that can't honor it. Fixed
				// 2026-04-29 after a phone-summon test where the meeting never
				// actually opened on Studio.
				console.log(`${ts()} [Summon] Joining via zoommtg:// deeplink`);
				// URL-encode the passcode — arbitrary string from the caller
				// (Gemini tool arg or $ZOOM_PERSONAL_PASSCODE). Pre-fix it was
				// spliced raw into BOTH the URL AND a shell command, so a
				// passcode like `"; rm -rf ~/.config; #` would break out of
				// the quoted shell argument and execute. cleanId is digit-only
				// (`.replace(/\D/g, '')`) so safe; pwd needs the encode pass.
				let zoomUrl = `zoommtg://zoom.us/join?confno=${cleanId}`;
				if (pwd) zoomUrl += `&pwd=${encodeURIComponent(pwd)}`;
				// execFileSync — `open` receives the URL as a single argv
				// entry, never spliced into a shell string. Closes the shell-
				// injection class entirely.
				execFileSync('open', [zoomUrl], { timeout: 10_000 });

				// Wait briefly for Zoom to register the deeplink, then enter the
				// combined Join-and-wait-for-meeting loop below. Per Mini's PR #546
				// review: the previous one-shot Join click could miss if the
				// preview window came up late (race between zoommtg:// open and
				// our 2s timer). The Join click is now retried inside the
				// readiness loop, not done once up front.
				await new Promise(r => setTimeout(r, 2000));
			} // end: not already in meeting

			// Per Mini PR #546 round-2: dialIn previously ran a separate
			// host-joined wait BEFORE the readiness/Join-retry loop. If the
			// preview still needed a Join click, that wait timed out and dialIn
			// fired against an unjoined meeting. Now the readiness/Join-retry
			// loop runs FIRST and gates the dialIn block on zoomReady.
			console.log(`${ts()} [Summon] Waiting for Zoom meeting window (retry Join click each second)...`);
			let zoomReady = false;
			let joinAttempts = 0;
			for (let i = 0; i < 60; i++) {
				try {
					const winNames = execSync(`osascript -e 'tell application "System Events" to return name of every window of process "zoom.us"'`, { timeout: 3_000 }).toString().trim();
					if (winNames.includes('Zoom Meeting') || winNames.includes('zoom share') || winNames.includes('floating video')) { zoomReady = true; break; }
					try {
						const joinResult = execSync(`osascript -e '
							tell application "zoom.us" to activate
							tell application "System Events"
								tell process "zoom.us"
									repeat with w in windows
										try
											set joinBtns to (buttons of w whose description is "Join")
											if (count of joinBtns) > 0 then
												click item 1 of joinBtns
												return "clicked"
											end if
										end try
									end repeat
								end tell
							end tell
							return "no_button"
						'`, { timeout: 3_000 }).toString().trim();
						if (joinResult === 'clicked') {
							joinAttempts++;
							console.log(`${ts()} [Summon] Join click (attempt ${joinAttempts}) — waiting for meeting window...`);
						}
					} catch {}
				} catch {}
				await new Promise(r => setTimeout(r, 1000));
			}
			console.log(`${ts()} [Summon] zoomReady=${zoomReady} (${joinAttempts} Join click(s))`);

			if (dialIn && zoomReady) {
				console.log(`${ts()} [Summon] Waiting 3s for Zoom server to register host before phone dial...`);
				await new Promise(r => setTimeout(r, 3000));
			}

			// Phone dial-in only when explicitly requested AND meeting actually
			// opened. Gated on zoomReady per Mini PR #546 round-2 #1.
			let phoneJoined = false;
			if (dialIn && zoomReady) try {
				const ping = await fetch(`http://localhost:${getPhonePort()}/health`, { signal: AbortSignal.timeout(2000) });
				if (ping.ok) {
					console.log(`${ts()} [Summon] Phone server available — dialing into meeting for voice`);
					const res = await fetch(`http://localhost:${getPhonePort()}/meeting`, {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ meetingId: cleanId, passcode: pwd, platform: 'zoom' }),
					});
					const data = await res.json() as { callSid?: string; error?: string };
					if (data.callSid) {
						phoneJoined = true;
						console.log(`${ts()} [Summon] Phone call placed: ${data.callSid} — voice agent stays connected until phone joins`);
						// Mute Zoom mic + speaker so voice agent doesn't pick up Zoom audio.
						// execFileSync (no shell) — the AppleScript comment below contains
						// "doesn't bleed", and an apostrophe inside a shell-single-quoted
						// `osascript -e '...'` argument silently ends the quoted string,
						// which masked this whole block as a "Zoom mute failed" log line
						// for as long as the comment has been there. Same bug class as
						// PR #527.
						const muteScript = `
tell application "System Events"
	tell process "zoom.us"
		keystroke "a" using {command down, shift down}
	end tell
end tell
set volume output volume 0`;
						try {
							execFileSync('/usr/bin/osascript', ['-e', muteScript], { timeout: 5_000 });
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

			// Audio dialog handling removed — it causes screen share drops.
			// Phone audio is handled by the Twilio connection, not by Zoom's audio dialog.

			let shareStarted = false;
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
					shareStarted = true;
					console.log(`${ts()} [Summon] Screen share started`);
					// Audio dialog handling removed — it steals focus from Zoom's
					// screen share, causing it to drop 2-5s after starting (975b8dd).
					// Rely on Zoom's "Automatically join computer audio" setting instead.
					// Mute is handled via Cmd+Shift+A hotkey below.
				} catch (err) {
					console.log(`${ts()} [Summon] Screen share failed: ${err}`);
				}
			} else if (shareScreen) {
				console.log(`${ts()} [Summon] Zoom meeting window not detected after 60s — skipping screen share`);
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

			// Close the zoom.us tab that Chrome opened during join (prevents scroll
			// targeting the wrong tab and reduces user confusion)
			try {
				execSync(`osascript -e '
					tell application "Google Chrome"
						repeat with w in windows
							set tabCount to count of tabs of w
							repeat with i from tabCount to 1 by -1
								set t to tab i of w
								if URL of t contains "zoom.us" then
									close t
								end if
							end repeat
						end repeat
					end tell
				'`, { timeout: 5_000 });
				console.log(`${ts()} [Summon] Closed zoom.us tab(s) in Chrome`);
			} catch {
				console.log(`${ts()} [Summon] No zoom.us tabs to close`);
			}

			// Honest status — per Mini's PR #546 review the old hardcoded
			// "joined with screen sharing" claim was misleading when zoomReady or
			// share fired up false. status reflects what actually happened.
			const status = !zoomReady ? 'meeting_window_not_detected'
				: (shareScreen && !shareStarted) ? 'joined_share_failed'
				: 'summoned';
			let instruction: string;
			if (!zoomReady) {
				instruction = `Zoom meeting window did not appear within the timeout (${joinAttempts} Join click attempts). Tell the user the meeting may not have opened — ask them to check Zoom and re-summon if needed.`;
			} else if (shareScreen && !shareStarted) {
				instruction = 'Joined the Zoom meeting but screen share AppleScript failed. Tell the user the meeting is up but the screen is not shared yet.';
			} else if (phoneJoined) {
				instruction = (shareStarted ? 'Screen is shared and ' : '') + 'Sutando is dialing in via phone. Voice stays connected.';
			} else if (shareScreen) {
				instruction = 'Zoom meeting joined with screen sharing and computer audio. Voice stays connected.';
			} else {
				instruction = 'Zoom meeting joined (no screen share requested).';
			}
			return {
				status,
				meetingId: cleanId,
				screenShare: shareScreen,
				zoomReady,
				shareStarted,
				phoneAgent: phoneJoined,
				instruction,
			};
		} catch (err) {
			return { error: `Summon failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

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
		const pwd = passcode ?? getZoomPasscode();
		const cleanId = (meetingId ?? getZoomPMI()).replace(/\D/g, '');
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
				// Same shell-injection fix as the summonTool join path above —
				// pwd is arbitrary caller input. URL-encode + execFileSync.
				if (zoomRunning) {
					const webUrl = `https://zoom.us/j/${cleanId}${pwd ? `?pwd=${encodeURIComponent(pwd)}` : ''}`;
					execFileSync('open', [webUrl], { timeout: 10_000 });
				} else {
					let zoomUrl = `zoommtg://zoom.us/join?confno=${cleanId}`;
					if (pwd) zoomUrl += `&pwd=${encodeURIComponent(pwd)}`;
					execFileSync('open', [zoomUrl], { timeout: 10_000 });
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

			// Audio dialog handling removed — causes screen share drops.
			// When joining via phone, Twilio handles audio. When joining without phone,
			// Zoom auto-joins computer audio without manual dialog interaction.

			return { status: 'joined', meetingId: cleanId, method: 'computer_audio', instruction: 'Joined Zoom with computer audio. No screen sharing.' };
		} catch (err) {
			return { error: `join_zoom failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

/**
 * Manifest entry point — `loadSkillManifestTools()` in src/inline-tools.ts
 * dynamic-imports this module and merges `tools` into the agent tool table
 * (#976 conformance).  The individual named exports above are kept for skills
 * that import a tool directly to override or extend Zoom behaviour (e.g.
 * an external plugin's dismiss override).
 */
export const tools: ToolDefinition[] = [summonTool, dismissTool, joinZoomTool];
