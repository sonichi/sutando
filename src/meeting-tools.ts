/**
 * Meeting tools — Google Meet, phone call, and meeting ID lookup.
 * Zoom tools (summon, dismiss, join_zoom) live in skills/zoom/tools.ts.
 */

import { execSync } from 'node:child_process';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

// Lazy env reads — ESM hoists imports before dotenv runs, so reading at
// import time gets empty values. These getters read at call time instead.
const getPhonePort = () => Number(process.env.PHONE_PORT) || 3100;

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

// --- Contact lookup + phone call (inline, bypasses task bridge) ---

export const callContactTool: ToolDefinition = {
	name: 'call_contact',
	description:
		'Look up a phone number and call a contact. Searches macOS Contacts by name. Instant. ' +
		'Use for ANY contact lookup or phone call — "find Bob\'s number", "call Mary", "look up Susan\'s phone".',
	parameters: z.object({
		name: z.string().describe('Contact name to search for (e.g. "Bob", "Mary Smith")'),
		message: z.string().optional().describe('What to tell the person. They have no tools — include all details they might need.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { name, message } = args as { name: string; message?: string };
		try {
			// Ensure Contacts.app is running
			execSync('open -ga Contacts', { timeout: 5_000 });

			// Search contacts via AppleScript — use first name for fuzzy matching
			// (voice transcription often garbles last names, e.g. "Gmeets" vs "GMeet")
			const firstName = name.split(/\s+/)[0];
			const safeName = firstName.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
			const script = `tell application "Contacts"
	set output to ""
	set results to (every person whose name contains "${safeName}")
	if (count of results) > 10 then set results to items 1 thru 10 of results
	repeat with p in results
		set pName to name of p
		set pPhones to ""
		repeat with ph in phones of p
			set pPhones to pPhones & (value of ph) & ","
		end repeat
		set output to output & pName & "|||" & pPhones & "\\n"
	end repeat
	return output
end tell`;
			const raw = execSync(`osascript -e '${script.replace(/'/g, "'\\''")}'`, { timeout: 15_000 }).toString().trim();

			// Parse results
			const contacts: { name: string; phones: string[] }[] = [];
			for (const line of raw.split('\n')) {
				const trimmed = line.trim();
				if (!trimmed) continue;
				const parts = trimmed.split('|||');
				if (parts.length < 2) continue;
				const cName = parts[0].trim();
				const phones = parts[1].split(',').map(p => p.trim()).filter(Boolean);
				if (phones.length > 0) contacts.push({ name: cName, phones });
			}

			if (contacts.length === 0) {
				console.log(`${ts()} [CallContact] no contacts with phone found for "${name}"`);
				return { error: `No contacts with a phone number found for "${name}". Ask the user for the number or a different name.` };
			}

			if (contacts.length > 1) {
				console.log(`${ts()} [CallContact] multiple matches for "${name}": ${contacts.map(c => c.name).join(', ')}`);
				return {
					status: 'multiple_matches',
					matches: contacts.map(c => ({ name: c.name, phones: c.phones })),
					instruction: 'Multiple contacts found. Ask the user which one to call.',
				};
			}

			// Single match — look up and call
			const contact = contacts[0];
			const phone = contact.phones[0];

			const purpose = message || `Calling ${contact.name}`;

			console.log(`${ts()} [CallContact] calling ${contact.name}`);
			const res = await fetch(`http://localhost:${getPhonePort()}/call`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ to: phone, message: purpose }),
			});
			const data = await res.json() as { callSid?: string; status?: string; error?: string };

			if (!res.ok) {
				return { error: `Phone server error: ${data.error || res.statusText}` };
			}

			console.log(`${ts()} [CallContact] call started: ${data.callSid}, purpose: ${purpose}`);
			return { status: 'calling', contact: contact.name, callSid: data.callSid, messageSent: purpose };
		} catch (err) {
			return { error: `call_contact failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};
