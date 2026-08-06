/**
 * Cross-platform OS abstraction layer.
 *
 * Sutando was originally built for macOS. The legacy code uses `osascript`,
 * `pbcopy/pbpaste`, `screencapture`, `pgrep`, `pkill`, `lsof`, etc. directly.
 * Rather than rewrite every call site, callers now delegate to the helpers
 * below — they branch on `process.platform` and pick the right backend.
 *
 * On macOS (`darwin`) all helpers reproduce the historic behavior verbatim.
 * On Windows (`win32`) helpers fall through to PowerShell-driven equivalents.
 * On other platforms the helpers return a clear "unsupported" error rather
 * than silently failing — keeps the failure mode visible.
 *
 * AppleScript-driven automation (Chrome, QuickTime, System Events keystrokes
 * with App-specific targeting) cannot be ported 1-for-1 and is gated at the
 * tool level (see inline-tools.ts) — the helper layer doesn't try to fake it.
 */

import { execSync, execFileSync, spawnSync } from 'node:child_process';
import { mkdirSync, existsSync } from 'node:fs';
import { dirname } from 'node:path';

export type SupportedPlatform = 'darwin' | 'win32' | 'linux';

export function currentPlatform(): SupportedPlatform | 'other' {
	const p = process.platform;
	if (p === 'darwin' || p === 'win32' || p === 'linux') return p;
	return 'other';
}

export const isWindows = (): boolean => process.platform === 'win32';
export const isMacOS = (): boolean => process.platform === 'darwin';
export const isLinux = (): boolean => process.platform === 'linux';

// ---------- Notifications ----------

export function notify(message: string, title = 'Sutando'): void {
	try {
		if (isMacOS()) {
			execFileSync('/usr/bin/osascript', [
				'-e',
				`display notification "${message.replace(/"/g, '\\"')}" with title "${title.replace(/"/g, '\\"')}"`,
			], { timeout: 2_000 });
			return;
		}
		if (isWindows()) {
			const safeMsg = message.replace(/'/g, "''");
			const safeTitle = title.replace(/'/g, "''");
			const script =
				`Add-Type -AssemblyName System.Windows.Forms; ` +
				`$n = New-Object System.Windows.Forms.NotifyIcon; ` +
				`$n.Icon = [System.Drawing.SystemIcons]::Information; ` +
				`$n.BalloonTipTitle = '${safeTitle}'; ` +
				`$n.BalloonTipText = '${safeMsg}'; ` +
				`$n.Visible = $true; ` +
				`$n.ShowBalloonTip(3000); ` +
				`Start-Sleep -Milliseconds 3500; ` +
				`$n.Dispose();`;
			spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
				timeout: 5_000,
				windowsHide: true,
			});
			return;
		}
		// Linux best-effort
		try { execFileSync('notify-send', [title, message], { timeout: 2_000 }); } catch {}
	} catch {
		// Notifications are advisory — never throw.
	}
}

// ---------- Clipboard ----------

export function clipboardRead(): string {
	if (isMacOS()) {
		return execSync('pbpaste', { encoding: 'utf-8', timeout: 2_000 });
	}
	if (isWindows()) {
		const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', 'Get-Clipboard'], {
			timeout: 3_000,
			encoding: 'utf-8',
			windowsHide: true,
		});
		return r.stdout || '';
	}
	try { return execSync('xclip -selection clipboard -o', { encoding: 'utf-8', timeout: 2_000 }); } catch { return ''; }
}

export function clipboardWrite(text: string): void {
	if (isMacOS()) {
		execSync('pbcopy', { input: text, encoding: 'utf-8', timeout: 2_000 });
		return;
	}
	if (isWindows()) {
		// Pipe through STDIN via $input automatic variable. Plain `Set-Clipboard`
		// (without `-Value`) ignores stdin, so the empirically-confirmed idiom
		// is `$input | Set-Clipboard`. Avoids command-line length limits + quoting.
		spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', '$input | Set-Clipboard'], {
			input: text,
			encoding: 'utf-8',
			timeout: 3_000,
			windowsHide: true,
		});
		return;
	}
	try { execSync('xclip -selection clipboard', { input: text, encoding: 'utf-8', timeout: 2_000 }); } catch {}
}

// ---------- Process listing / killing ----------

/**
 * Returns true iff any running process command line matches `pattern`.
 * `pattern` is a literal substring on macOS/Linux (pgrep -f) and a
 * case-insensitive substring on Windows (Get-CimInstance Win32_Process).
 */
export function isProcessRunning(pattern: string): boolean {
	if (isMacOS() || isLinux()) {
		const r = spawnSync('pgrep', ['-f', pattern], { timeout: 3_000 });
		return r.status === 0;
	}
	if (isWindows()) {
		const safe = pattern.replace(/'/g, "''");
		const script =
			`Get-CimInstance Win32_Process | Where-Object { ` +
			`$_.CommandLine -and $_.CommandLine.ToLower().Contains('${safe.toLowerCase()}') } | ` +
			`Select-Object -First 1 ProcessId`;
		const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
			timeout: 5_000,
			encoding: 'utf-8',
			windowsHide: true,
		});
		return (r.stdout || '').includes('ProcessId');
	}
	return false;
}

/**
 * Kill any process whose command line matches `pattern`. Best-effort — never
 * throws. Pair with `isProcessRunning` if you need to confirm the kill landed.
 */
export function killProcess(pattern: string): void {
	if (isMacOS() || isLinux()) {
		try { spawnSync('pkill', ['-f', pattern], { timeout: 3_000 }); } catch {}
		return;
	}
	if (isWindows()) {
		const safe = pattern.replace(/'/g, "''");
		const script =
			`Get-CimInstance Win32_Process | Where-Object { ` +
			`$_.CommandLine -and $_.CommandLine.ToLower().Contains('${safe.toLowerCase()}') } | ` +
			`ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`;
		try {
			spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
				timeout: 5_000,
				windowsHide: true,
			});
		} catch {}
	}
}

// ---------- Port-in-use check ----------

export function isPortInUse(port: number): boolean {
	if (isMacOS() || isLinux()) {
		const r = spawnSync('lsof', ['-i', `:${port}`], { timeout: 3_000 });
		return r.status === 0;
	}
	if (isWindows()) {
		// netstat is universally available; -ano includes PID + state.
		const r = spawnSync('netstat', ['-ano'], { timeout: 5_000, encoding: 'utf-8', windowsHide: true });
		const needle = `:${port} `;
		return (r.stdout || '').split('\n').some(line => line.includes(needle) && line.toUpperCase().includes('LISTENING'));
	}
	return false;
}

// ---------- Screen capture ----------

/**
 * Capture the entire primary display to `outPath`. Returns true on success.
 * `format` is 'png' or 'jpg'. The Windows backend uses System.Drawing via
 * PowerShell; the macOS backend uses /usr/sbin/screencapture.
 */
export function captureScreen(outPath: string, format: 'png' | 'jpg' = 'png'): boolean {
	try {
		mkdirSync(dirname(outPath), { recursive: true });
	} catch {}
	if (isMacOS()) {
		const typeFlag = format === 'jpg' ? 'jpg' : 'png';
		const r = spawnSync('screencapture', ['-x', '-t', typeFlag, outPath], { timeout: 5_000 });
		return r.status === 0 && existsSync(outPath);
	}
	if (isWindows()) {
		const fmt = format === 'jpg' ? 'Jpeg' : 'Png';
		const safe = outPath.replace(/'/g, "''");
		const script =
			`Add-Type -AssemblyName System.Windows.Forms; ` +
			`Add-Type -AssemblyName System.Drawing; ` +
			`$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; ` +
			`$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; ` +
			`$g = [System.Drawing.Graphics]::FromImage($bmp); ` +
			`$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); ` +
			`$bmp.Save('${safe}', [System.Drawing.Imaging.ImageFormat]::${fmt}); ` +
			`$g.Dispose(); $bmp.Dispose();`;
		const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
			timeout: 10_000,
			windowsHide: true,
		});
		return r.status === 0 && existsSync(outPath);
	}
	return false;
}

// ---------- Open a file/URL with the default handler ----------

export function openWithDefault(target: string): void {
	if (isMacOS()) {
		execFileSync('open', [target], { timeout: 5_000 });
		return;
	}
	if (isWindows()) {
		// `start` is a cmd.exe builtin. The empty "" preserves the title slot
		// so a quoted path doesn't get interpreted as the window title.
		spawnSync('cmd.exe', ['/c', 'start', '""', target], { timeout: 5_000, windowsHide: true });
		return;
	}
	try { spawnSync('xdg-open', [target], { timeout: 5_000 }); } catch {}
}

// ---------- Mark a file is on macOS only (for tool gating) ----------

/**
 * Returns an `{error}` object suitable for returning from a Sutando tool
 * when the tool only works on macOS. Use at the top of an `execute()` body.
 *
 *   if (!isMacOS()) return macOSOnlyError('switch_app');
 *
 * Keeps the failure message uniform across the tool surface.
 */
export function macOSOnlyError(toolName: string): { error: string } {
	return {
		error:
			`${toolName} is only available on macOS — it uses AppleScript/System Events. ` +
			`Running on ${process.platform}; ask the user to perform this action manually.`,
	};
}
