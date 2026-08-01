/**
 * Resolve a python3 interpreter that will actually run.
 *
 * On macOS `/usr/bin/python3` is not python — it is the Xcode Command Line
 * Tools stub, one inode hardlinked across python3 / git / swift / swiftc /
 * clang / gcc / make. The file exists whether or not the tools are installed;
 * spawning it without them raises the modal "install command line developer
 * tools" dialog and returns nothing.
 *
 * So the two obvious probes are both wrong here:
 *   - `existsSync('/usr/bin/python3')` is true on every Mac and proves nothing.
 *   - spawning it to check is the failure mode itself.
 *
 * And hardcoding the absolute path is worse still: it pins the stub, so a user
 * who installs a real python (Homebrew, python.org, pyenv) keeps getting the
 * dialog because PATH never gets a say.
 *
 * Resolution order — the same cascade `scripts/sutando-config.sh` and
 * `src/agent/claude/cli/start-cli.sh` use for `$PY`, and the Swift twin in
 * `SutandoConfig.resolvePython`:
 *
 *   1. `$SUTANDO_PY`, exported by the desktop launcher.
 *   2. The bundle-vendored relocatable python beside the engine copy.
 *   3. A bare `python3`, resolved through PATH — but ONLY when
 *      `xcode-select -p` reports installed developer tools.
 *   4. null, meaning the caller must skip rather than prompt.
 *
 * Tier 3 is deliberately the bare name rather than an absolute path. It lets a
 * real interpreter earlier on PATH win, and it keeps the stub's path out of
 * this file entirely. The `xcode-select` gate is what makes it safe: with the
 * tools installed, whatever `python3` resolves to is a working interpreter
 * (including the system one); without them we cannot know, so we decline. A
 * host with a Homebrew python but no developer tools is not reachable in
 * practice — Homebrew's own installer requires the tools.
 */
import { execFileSync } from 'node:child_process';
import { accessSync, constants } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { findRepoRoot } from './sutando_config.js';

/** Raised when no runnable interpreter exists. */
export class PythonUnavailableError extends Error {
	constructor(message = 'no runnable python3 (no $SUTANDO_PY, no bundled runtime, no developer tools)') {
		super(message);
		this.name = 'PythonUnavailableError';
	}
}

/** True when `path` exists and is executable. */
export function isExecutableFile(path: string): boolean {
	try {
		accessSync(path, constants.X_OK);
		return true;
	} catch {
		return false;
	}
}

/**
 * True when `xcode-select -p` reports an installed developer directory.
 *
 * This is the only safe probe: `/usr/bin/xcode-select` is a real binary (its
 * own inode, link count 1) rather than one of the stubs, so asking it does not
 * raise the dialog. `src/migrate.sh` already gates on it the same way.
 * Injected for tests; any failure to probe is treated as "not installed",
 * which fails toward skipping rather than toward a modal dialog.
 */
export type ExecProbe = (file: string, args: string[], options: { stdio: 'ignore'; timeout: number }) => unknown;

const defaultProbe: ExecProbe = (file, args, options) => execFileSync(file, args, options);

export function developerToolsInstalled(run: ExecProbe = defaultProbe): boolean {
	try {
		run('/usr/bin/xcode-select', ['-p'], { stdio: 'ignore', timeout: 5_000 });
		return true;
	} catch {
		return false;
	}
}

/**
 * Candidate paths for the bundle-vendored relocatable python, most specific
 * first. Exported so the ordering is testable without a real bundle on disk —
 * same rationale as `ffmpegSubtitleCandidates` in `src/recording-tools.ts`.
 *
 * Two layouts are covered. `scripts/sutando-config.sh` documents the runtime as
 * a sibling of the engine checkout (`<repo>/../runtime/python/bin/python3`).
 * The desktop bundle instead ships node and the runtime side by side, which is
 * how `recording-tools.ts` finds its vendored ffmpeg — so the directory holding
 * the running node binary is checked too.
 */
export function bundledPythonCandidates(repoRoot: string | undefined, execPath: string): string[] {
	const out: string[] = [];
	// Engine-sibling layout, documented in scripts/sutando-config.sh:
	//   <repo>/../runtime/python/bin/python3
	if (repoRoot) out.push(join(dirname(repoRoot), 'runtime', 'python', 'bin', 'python3'));
	// Packaged layout, derived from the RUNTIME ROOT rather than from the node
	// binary's directory. The vendored tree is
	//   <Resources>/runtime/{bin/node, python/bin/python3}
	// so python is a sibling of node's `bin/`, i.e. one level ABOVE dirname(node).
	//
	// An earlier revision appended the full 'runtime/python/bin/python3' to both
	// dirname(execPath) and dirname(dirname(execPath)), which doubled the segment
	// and produced '<Resources>/runtime/bin/runtime/python/…' and
	// '<Resources>/runtime/runtime/python/…' — neither of which exists. The
	// resolver then fell through to the xcode-select tier and returned null on a
	// packaged, no-CLT host that HAD a working vendored python: exactly the
	// install this module is for. (Caught by @john-the-dev reviewing #2475; the
	// old test only asserted the candidates ENDED with the relative path, so it
	// blessed both malformed forms.)
	out.push(join(dirname(dirname(execPath)), 'python', 'bin', 'python3'));
	// …and the flatter variant where node sits directly in the runtime root.
	out.push(join(dirname(execPath), 'python', 'bin', 'python3'));
	return out;
}

/**
 * Pure selection step, given the inputs the impure layer gathered.
 *
 * Split out from `resolvePython` so every tier is testable without touching
 * the host's toolchain or PATH.
 */
export function selectPython(opts: {
	explicit?: string;
	bundled: string[];
	isExecutable: (p: string) => boolean;
	toolsInstalled: () => boolean;
}): string | null {
	const { explicit, bundled, isExecutable, toolsInstalled } = opts;
	if (explicit && isExecutable(explicit)) return explicit;
	const vendored = bundled.find(isExecutable);
	if (vendored) return vendored;
	// Bare name on purpose — see the module header. Never the absolute path.
	return toolsInstalled() ? 'python3' : null;
}

let cached: string | null | undefined;

/** Resolve an interpreter, or null. Cached — the answer cannot change within a process lifetime. */
export function resolvePython(): string | null {
	if (cached !== undefined) return cached;
	cached = selectPython({
		explicit: process.env.SUTANDO_PY,
		bundled: bundledPythonCandidates(
			findRepoRoot(dirname(fileURLToPath(import.meta.url))),
			process.execPath,
		),
		isExecutable: isExecutableFile,
		toolsInstalled: () => developerToolsInstalled(),
	});
	return cached;
}

/** Tests only — drop the memoised answer. */
export function resetCacheForTests(): void {
	cached = undefined;
}

/**
 * Resolve an interpreter or throw `PythonUnavailableError`.
 *
 * Call this INSIDE the caller's existing try-block. Both current call sites
 * already wrap their spawn in `try { … } catch {}`, so an unavailable
 * interpreter degrades through the handling they have rather than needing a
 * new branch — the same shape as `git_argv` in `src/git_binary.py`.
 */
export function requirePython(): string {
	const py = resolvePython();
	if (py === null) throw new PythonUnavailableError();
	return py;
}

/**
 * POSIX single-quote a string for safe interpolation into a shell command.
 *
 * Needed because one call site builds a shell string (`execSync`) rather than
 * an argv array, and a resolved interpreter path can contain spaces — the
 * bundled runtime lives under the app bundle, and `$SUTANDO_PY` is whatever the
 * launcher exported.
 */
export function shellQuote(value: string): string {
	return `'${value.replace(/'/g, `'\\''`)}'`;
}
