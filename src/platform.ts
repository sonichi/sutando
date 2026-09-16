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

import { execSync, execFile, execFileSync, spawnSync } from 'node:child_process';
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

export function activateWindowsApp(app: string, scriptPath: string, signal?: AbortSignal): Promise<{ status: 'switched'; app: string }> {
	return new Promise((resolve, reject) => {
		execFile('pwsh', ['-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-File', scriptPath, '-App', app], {
			timeout: 15_000, encoding: 'utf8', windowsHide: true, signal,
		}, (error, stdout, stderr) => {
			let response: unknown;
			try {
				response = JSON.parse(stdout.trim());
			} catch {
				reject(new Error(stderr.trim() || error?.message || 'Windows app launcher returned invalid JSON.'));
				return;
			}
			if (typeof response !== 'object' || response === null) {
				reject(new Error('Windows app launcher returned an invalid response.'));
				return;
			}
			if ('error' in response && typeof response.error === 'string') {
				reject(new Error(response.error));
				return;
			}
			if (error) {
				reject(new Error(stderr.trim() || error.message));
				return;
			}
			if (!('status' in response) || response.status !== 'switched'
				|| !('foreground_verified' in response) || response.foreground_verified !== true
				|| !('app' in response) || typeof response.app !== 'string' || !response.app.trim()) {
				reject(new Error('Windows did not verify the requested app in the foreground.'));
				return;
			}
			resolve({ status: 'switched', app: response.app });
		});
	});
}

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
		const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); [Console]::Out.Write((Get-Clipboard -Raw))'], {
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
		// Read stdin as UTF-8 without normalizing newlines or interpolating text.
		spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', '[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); Set-Clipboard -Value ([Console]::In.ReadToEnd())'], {
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
		// Keep the target out of shell source, including cmd metacharacters and expansions.
		const script = '$ErrorActionPreference = "Stop"; ' +
			'$start = [System.Diagnostics.ProcessStartInfo]::new(); ' +
			'$start.FileName = $env:SUTANDO_OPEN_TARGET; $start.UseShellExecute = $true; ' +
			'[System.Diagnostics.Process]::Start($start) | Out-Null';
		execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
			timeout: 5_000, windowsHide: true, env: { ...process.env, SUTANDO_OPEN_TARGET: target },
		});
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
/**
 * Downscale an image so its longest side is at most `maxDim`, writing a JPEG to
 * `dest`. Returns whether `dest` was produced; callers keep the original otherwise.
 */
export function resizeImage(src: string, dest: string, maxDim: number, timeoutMs = 4_000): boolean {
	try {
		if (isMacOS()) {
			execFileSync('sips', ['-Z', String(maxDim), '-s', 'format', 'jpeg', src, '--out', dest], { timeout: timeoutMs, stdio: 'ignore' });
		} else if (isWindows()) {
			// Paths travel in the environment: PowerShell reads U+2018-U+201B as quotes too.
			const script = '$ErrorActionPreference = "Stop"; Add-Type -AssemblyName System.Drawing; ' +
				'$src = [System.Drawing.Image]::FromFile($env:SUTANDO_RESIZE_INPUT); try { ' +
				'$limit = [int]$env:SUTANDO_RESIZE_MAXDIM; ' +
				'$scale = [Math]::Min(1.0, $limit / [double][Math]::Max($src.Width, $src.Height)); ' +
				'$w = [Math]::Max(1, [int][Math]::Floor($src.Width * $scale)); ' +
				'$h = [Math]::Max(1, [int][Math]::Floor($src.Height * $scale)); ' +
				'$dst = [System.Drawing.Bitmap]::new($w, $h); $g = [System.Drawing.Graphics]::FromImage($dst); ' +
				'$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic; ' +
				'$g.DrawImage($src, 0, 0, $w, $h); ' +
				'$dst.Save($env:SUTANDO_RESIZE_OUTPUT, [System.Drawing.Imaging.ImageFormat]::Jpeg); ' +
				'$g.Dispose(); $dst.Dispose() } finally { $src.Dispose() }';
			execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
				timeout: Math.max(timeoutMs, 10_000), windowsHide: true, stdio: 'ignore',
				env: { ...process.env, SUTANDO_RESIZE_INPUT: src, SUTANDO_RESIZE_OUTPUT: dest, SUTANDO_RESIZE_MAXDIM: String(maxDim) },
			});
		} else {
			return false;
		}
	} catch {
		return false;
	}
	return existsSync(dest);
}

export function macOSOnlyError(toolName: string): { error: string } {
	return {
		error:
			`${toolName} is only available on macOS — it uses AppleScript/System Events. ` +
			`Running on ${process.platform}; ask the user to perform this action manually.`,
	};
}
