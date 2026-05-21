import Foundation

// LaunchAgent installer.
//
// Reads `com.sutando.*.plist.template` files from the .app bundle's
// `Resources/LaunchAgents/` directory, substitutes placeholders to point at
// the bundled runtime + the user's `SUTANDO_HOME`, and writes them to
// `$SUTANDO_HOME/LaunchAgents/`. Then runs `launchctl bootstrap` so launchd
// picks them up.
//
// Lifecycle: plists live OUTSIDE `~/Library/LaunchAgents/` on purpose. The
// system auto-loads anything in that directory at user login, which conflicts
// with our model where services are bound to the app process — they start when
// Sutando.app opens, stop when it quits. Writing to a non-auto-loaded path
// means launchd only runs them when we explicitly `bootstrap`. Plists left
// behind from older Sutando versions in `~/Library/LaunchAgents/` are
// `bootout`'d and deleted by `migrateLegacyPlists()` on first launch.
//
// Placeholders match `app/LaunchAgents/README.md`:
//   {{REPO_DIR}}       — bundled repo source root
//   {{NPX_BIN}}        — path to npx (bundled or system)
//   {{TSX_BIN}}        — path to tsx (bundled or system; not currently used in templates)
//   {{NODE_BIN}}       — path to node binary
//   {{NODE_BIN_DIR}}   — directory containing node + npx (added to PATH)
//   {{PYTHON_BIN}}     — path to python3 (bundled or system)
//   {{SUTANDO_HOME}}   — ~/Library/Application Support/Sutando
//   {{LOG_DIR}}        — {{SUTANDO_HOME}}/logs

enum LaunchAgentError: Error {
    case bundleResourcesMissing
    case launchctlFailed(label: String, status: Int32, output: String)
}

/// Result of an install pass. `installed` is the set of agents successfully
/// loaded into launchd. `skippedDisabled` is the set of agents whose
/// templates carry `Disabled=true` — we write the plist (so the user can
/// flip it later via launchctl) but never call `launchctl bootstrap`,
/// which errors with code 5 on Disabled plists. `failed` is everything
/// else that errored. The installer collects rather than throws so a
/// single broken plist doesn't block the rest.
struct LaunchAgentInstallSummary {
    var installed: [String] = []
    var skippedDisabled: [String] = []
    var failed: [(label: String, message: String)] = []

    var isClean: Bool { failed.isEmpty }
}

struct LaunchAgentPaths {
    let repoDir: String
    let sutandoHome: String
    let runtimeBinDir: String?  // nil → use system fallbacks
}

class LaunchAgentInstaller {

    /// Default paths derived from the running .app bundle and HOME.
    /// `repoDir`: `Bundle.main.resourcePath/repo` if bundled, else the dev
    /// repo (resolved by main.swift's `workspace`).
    /// `sutandoHome`: `~/Library/Application Support/Sutando`.
    /// `runtimeBinDir`: `Bundle.main.resourcePath/runtime/bin` if it exists,
    /// otherwise nil (fall back to system paths).
    static func defaultPaths(workspace: String) -> LaunchAgentPaths {
        let resourcePath = Bundle.main.resourcePath
        let bundledRepo = resourcePath.map { $0 + "/repo" }
        let repoDir: String
        if let r = bundledRepo, FileManager.default.fileExists(atPath: r + "/CLAUDE.md") {
            repoDir = r
        } else {
            repoDir = workspace
        }
        let runtimeBin = resourcePath.map { $0 + "/runtime/bin" }
        let runtimeBinDir = runtimeBin.flatMap { dir in
            FileManager.default.fileExists(atPath: dir + "/node") ? dir : nil
        }
        let home = (NSHomeDirectory() as NSString).appendingPathComponent(".sutando/workspace")
        return LaunchAgentPaths(repoDir: repoDir, sutandoHome: home, runtimeBinDir: runtimeBinDir)
    }

    /// Resolve a placeholder map. Falls back to common system paths when
    /// the bundled runtime isn't present (so the installer works even
    /// before Phase 1.5 ships the bundled Node/Python).
    func placeholders(_ p: LaunchAgentPaths) -> [String: String] {
        let runtimeBin = p.runtimeBinDir
        func bundleOrSystem(_ name: String, systemFallbacks: [String]) -> String {
            if let dir = runtimeBin {
                let bundled = dir + "/" + name
                if FileManager.default.fileExists(atPath: bundled) { return bundled }
            }
            for path in systemFallbacks where FileManager.default.fileExists(atPath: path) {
                return path
            }
            // Last-resort: bare command name. launchctl will rely on PATH.
            return name
        }
        let nodeBin = bundleOrSystem("node", systemFallbacks: ["/opt/homebrew/bin/node", "/usr/local/bin/node"])
        let npxBin = bundleOrSystem("npx", systemFallbacks: ["/opt/homebrew/bin/npx", "/usr/local/bin/npx"])
        let tsxBin = bundleOrSystem("tsx", systemFallbacks: ["/opt/homebrew/bin/tsx", "/usr/local/bin/tsx"])
        let pythonBin = bundleOrSystem("python3", systemFallbacks: ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"])
        let tmuxBin = bundleOrSystem("tmux", systemFallbacks: ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"])
        let nodeBinDir = (nodeBin as NSString).deletingLastPathComponent
        let logDir = p.sutandoHome + "/logs"
        return [
            "{{REPO_DIR}}": p.repoDir,
            "{{NODE_BIN}}": nodeBin,
            "{{NPX_BIN}}": npxBin,
            "{{TSX_BIN}}": tsxBin,
            "{{NODE_BIN_DIR}}": nodeBinDir,
            "{{PYTHON_BIN}}": pythonBin,
            "{{TMUX_BIN}}": tmuxBin,
            "{{SUTANDO_WORKSPACE}}": p.sutandoHome,
            "{{LOG_DIR}}": logDir,
            // For PathState-gated KeepAlive (discord/telegram bridges
            // wait on ~/.claude/channels/<name>/.env before running).
            // launchd doesn't expand $HOME inside plist values, so we
            // resolve it at install time.
            "{{HOME}}": NSHomeDirectory(),
        ]
    }

    /// Render a template by simple string substitution.
    func render(_ template: String, placeholders: [String: String]) -> String {
        var out = template
        for (key, value) in placeholders {
            out = out.replacingOccurrences(of: key, with: value)
        }
        return out
    }

    /// Where this app version writes rendered plists. NOT in
    /// `~/Library/LaunchAgents/` — that directory auto-loads at user
    /// login, and the new lifecycle binds services to the app process.
    /// Plists land under `SUTANDO_HOME/LaunchAgents/`; launchctl loads
    /// them by path on app launch and unloads on app quit.
    var runDir: String {
        let home: String
        if let env = ProcessInfo.processInfo.environment["SUTANDO_WORKSPACE"], !env.isEmpty {
            home = (env as NSString).expandingTildeInPath
        } else {
            home = (NSHomeDirectory() as NSString)
                .appendingPathComponent(".sutando/workspace")
        }
        return home + "/LaunchAgents"
    }

    /// Legacy install directory. Older Sutando versions wrote plists
    /// here, which causes services to auto-load on user login. The new
    /// app moves away from this; `migrateLegacyPlists()` cleans up any
    /// stragglers on first launch of the new version.
    var legacyLaunchAgentsDir: String {
        (NSHomeDirectory() as NSString).appendingPathComponent("Library/LaunchAgents")
    }

    /// Where the templates live in the bundle. Returns nil when running
    /// from a raw binary (no `Resources/LaunchAgents/` available) — caller
    /// can fall back to `<repoDir>/app/LaunchAgents/` for the dev workflow.
    var bundleTemplatesDir: String? {
        guard let resourcePath = Bundle.main.resourcePath else { return nil }
        let dir = resourcePath + "/LaunchAgents"
        return FileManager.default.fileExists(atPath: dir) ? dir : nil
    }

    /// Install the LaunchAgent set. Reads templates, substitutes
    /// placeholders, writes plists to `runDir`
    /// (`$SUTANDO_HOME/LaunchAgents/`), and bootstraps each one that
    /// isn't marked `Disabled=true`. Continues past individual failures
    /// so one broken plist doesn't block the rest. Idempotent — every
    /// bootstrap is preceded by a bootout so re-installing replaces
    /// rather than collides.
    @discardableResult
    func install(paths: LaunchAgentPaths, templatesDirOverride: String? = nil) throws -> LaunchAgentInstallSummary {
        let templatesDir = templatesDirOverride ?? bundleTemplatesDir ?? (paths.repoDir + "/app/LaunchAgents")
        guard FileManager.default.fileExists(atPath: templatesDir) else {
            throw LaunchAgentError.bundleResourcesMissing
        }

        // Ensure run-dir (where rendered plists land) and SUTANDO_HOME/logs exist.
        try FileManager.default.createDirectory(atPath: runDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(atPath: paths.sutandoHome + "/logs", withIntermediateDirectories: true)

        // Install Sutando skills into ~/.claude/skills/. Skills are required
        // for the core-agent service: claude looks them up by name when it
        // sees `/proactive-loop` (or any other skill slash command), and
        // without them in `~/.claude/skills/` the core-agent crash-loops.
        // The install.sh in the bundled skills/ dir creates idempotent
        // symlinks, so re-running on every install is safe.
        let skillsScript = paths.repoDir + "/skills/install.sh"
        if FileManager.default.fileExists(atPath: skillsScript) {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/bash")
            proc.arguments = [skillsScript]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            try? proc.run()
            proc.waitUntilExit()
        }

        // Install Python skill deps (image-generation, make-viral-video,
        // discord-bridge). System python3 on macOS 15 is PEP 668-managed
        // ("externally-managed-environment") so plain `pip install --user`
        // refuses. `--break-system-packages` is the documented override.
        // pip itself is shipped with system python3 (invoked as `python3
        // -m pip`). Best-effort: skip silently if pip is unavailable so a
        // broken install of one skill doesn't block service bootstrap.
        // Idempotent (pip is a no-op on already-installed packages of the
        // same version) so we run it on every install.
        let phs = placeholders(paths)
        // Install skill deps into the SAME interpreter the service plists run
        // ({{PYTHON_BIN}} — the bundled runtime python if present, else the
        // resolved system python3). Hardcoding /usr/bin/python3 here installed
        // discord.py for the wrong interpreter, so discord-bridge — launched
        // with /opt/homebrew/bin/python3 — still crashed on `import discord`.
        installPythonSkillDeps(pythonBin: phs["{{PYTHON_BIN}}"] ?? "/usr/bin/python3")

        // Build the macos-use MCP server (~35s `xcrun swift build`) and
        // register it with Claude Code. Fire-and-forget on a background
        // thread so the install bootstrap doesn't stall while waiting on
        // the network clone + Swift compile. If xcode-select hasn't been
        // run, build.sh fails fast — that's surfaced via the skill's own
        // SKILL.md doc rather than blocking service install. Idempotent:
        // build.sh skips when the binary already exists, install-mcp.sh
        // no-ops when the MCP server is already registered.
        buildMacosUseAsync(repoDir: paths.repoDir)

        let files = try FileManager.default.contentsOfDirectory(atPath: templatesDir)
        var summary = LaunchAgentInstallSummary()
        // Labels we explicitly skip even if their template is present in
        // the bundle. Used by services that have moved off launchd
        // because TCC's responsible-process attribution made the
        // launchd path unworkable. See ScreenCaptureSupervisor.swift.
        let skipLabels: Set<String> = ["com.sutando.screen-capture"]
        // Retired labels: services whose template was removed (folded into
        // another process). Iterate up front, on every install, to bootout
        // + delete any stale plist a previous install left behind. Without
        // this, the old launchd job KeepAlive-loops against a missing entry
        // point and eats CPU.
        //
        //   com.sutando.web-client: folded into com.sutando.voice-agent via
        //     PR-A (src/web-server.ts now lives in the voice-agent process).
        //     Removed plist template + src/web-client.ts.
        let retiredLabels: [String] = ["com.sutando.web-client"]
        for label in retiredLabels {
            let uid = getuid()
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            proc.arguments = ["bootout", "gui/\(uid)/\(label)"]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            _ = try? proc.run()
            proc.waitUntilExit()
            let stalePlist = paths.sutandoHome + "/LaunchAgents/\(label).plist"
            try? FileManager.default.removeItem(atPath: stalePlist)
        }
        for file in files where file.hasSuffix(".plist.template") {
            let label = String(file.dropLast(".plist.template".count))
            if skipLabels.contains(label) {
                // Best-effort: bootout any leftover launchd job from a
                // previous install of this label so we don't fight over
                // its port. Same logic the supervisor runs at startup;
                // duplicating here covers the user who upgrades but
                // doesn't quit-and-relaunch immediately.
                let uid = getuid()
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
                proc.arguments = ["bootout", "gui/\(uid)/\(label)"]
                proc.standardOutput = FileHandle.nullDevice
                proc.standardError = FileHandle.nullDevice
                _ = try? proc.run()
                proc.waitUntilExit()
                let stalePlist = paths.sutandoHome + "/LaunchAgents/\(label).plist"
                try? FileManager.default.removeItem(atPath: stalePlist)
                continue
            }
            let templatePath = templatesDir + "/" + file
            guard let template = try? String(contentsOfFile: templatePath, encoding: .utf8) else {
                summary.failed.append((label, "could not read template at \(templatePath)"))
                continue
            }
            let rendered = render(template, placeholders: phs)
            let destPath = runDir + "/" + label + ".plist"
            do {
                try rendered.write(toFile: destPath, atomically: true, encoding: .utf8)
            } catch {
                summary.failed.append((label, "write failed: \(error.localizedDescription)"))
                continue
            }

            // Skip bootstrap for plists with Disabled=true. launchctl
            // returns code 5 ("Input/output error") on those, which used
            // to abort the entire install. Writing the plist still lets
            // the user opt in later via `launchctl enable + bootstrap`.
            if isDisabled(template: rendered) {
                summary.skippedDisabled.append(label)
                continue
            }

            do {
                try bootstrap(label: label, plistPath: destPath)
                summary.installed.append(label)
            } catch let LaunchAgentError.launchctlFailed(_, status, output) {
                let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
                summary.failed.append((label, "launchctl bootstrap \(status): \(trimmed)"))
            } catch {
                summary.failed.append((label, error.localizedDescription))
            }
        }
        return summary
    }

    /// Build the macos-use MCP server in the background so the install
    /// bootstrap returns immediately. Logs to
    /// `$SUTANDO_HOME/logs/macos-use-build.log` for post-mortem when the
    /// user later wonders why the skill isn't available.
    private func buildMacosUseAsync(repoDir: String) {
        let buildScript = repoDir + "/skills/macos-use/scripts/build.sh"
        let installScript = repoDir + "/skills/macos-use/scripts/install-mcp.sh"
        guard FileManager.default.fileExists(atPath: buildScript) else { return }
        let logDir = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".sutando/workspace/logs")
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        let logPath = logDir + "/macos-use-build.log"
        DispatchQueue.global(qos: .background).async {
            // build.sh shells out to git + xcrun. Both come from system
            // PATH on macOS 15, so a vanilla launchd PATH is enough.
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/bash")
            proc.arguments = [buildScript]
            if let log = FileHandle(forWritingAtPath: logPath) {
                log.seekToEndOfFile()
                proc.standardOutput = log
                proc.standardError = log
            } else if FileManager.default.createFile(atPath: logPath, contents: Data()),
                      let log = FileHandle(forWritingAtPath: logPath) {
                proc.standardOutput = log
                proc.standardError = log
            }
            try? proc.run()
            proc.waitUntilExit()
            if proc.terminationStatus == 0 && FileManager.default.fileExists(atPath: installScript) {
                let mcp = Process()
                mcp.executableURL = URL(fileURLWithPath: "/bin/bash")
                mcp.arguments = [installScript]
                mcp.standardOutput = FileHandle.nullDevice
                mcp.standardError = FileHandle.nullDevice
                try? mcp.run()
                mcp.waitUntilExit()
            }
        }
    }

    /// Install Python deps required by bundled skills. Fire-and-forget on
    /// a background thread because pip resolution + wheel install can
    /// take ~30s on a fresh Mac; no service plist imports these deps at
    /// boot, so a delayed install is fine. Logs to
    /// `$SUTANDO_HOME/logs/pip-skill-deps.log` for post-mortem.
    private func installPythonSkillDeps(pythonBin: String) {
        // Canonical list (verified by grepping non-stdlib imports across
        // skills/ + src/):
        //   - google-genai  → image-generation
        //   - Pillow         → image-generation + make-viral-video
        //   - discord.py     → discord-bridge
        //   - slack_bolt     → slack-bridge (Socket Mode client)
        // Other skills (telegram-bridge, openai-tts, x-twitter) either use
        // urllib or self-install on demand.
        let packages = ["google-genai", "Pillow", "discord.py", "slack_bolt"]
        let logDir = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".sutando/workspace/logs")
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        let logPath = logDir + "/pip-skill-deps.log"
        DispatchQueue.global(qos: .background).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: pythonBin)
            proc.arguments = ["-m", "pip", "install",
                              "--break-system-packages",
                              "--user",
                              "--disable-pip-version-check"] + packages
            if !FileManager.default.fileExists(atPath: logPath) {
                FileManager.default.createFile(atPath: logPath, contents: Data())
            }
            if let log = FileHandle(forWritingAtPath: logPath) {
                log.seekToEndOfFile()
                proc.standardOutput = log
                proc.standardError = log
            }
            try? proc.run()
            proc.waitUntilExit()
        }
    }

    /// Match `<key>Disabled</key>` followed by `<true/>` (allowing whitespace).
    private func isDisabled(template: String) -> Bool {
        guard let range = template.range(of: "<key>Disabled</key>") else { return false }
        let after = template[range.upperBound...]
        // Look for the next plist value tag — true or false.
        guard let truePos = after.range(of: "<true/>") else { return false }
        guard let falsePos = after.range(of: "<false/>") else { return true }
        return truePos.lowerBound < falsePos.lowerBound
    }

    /// Bootout + bootstrap an agent. Idempotent — bootout first to avoid
    /// "service already loaded" errors.
    ///
    /// **Bootout/bootstrap race**: `launchctl bootout` returns when
    /// launchd accepts the request, NOT when the service finishes
    /// tearing down. A subsequent `bootstrap` racing against a still-
    /// terminating service hits "Bootstrap failed: 37" (service in flux)
    /// for the slowest services — voice-agent (network
    /// teardown in their shutdown hooks). `waitUntilLabelStopped()` is
    /// called between the two to drain the bootout before bootstrap.
    private func bootstrap(label: String, plistPath: String) throws {
        let uid = String(getuid())

        // Bootout (silent if not loaded). Errors here are fine.
        let out = Process()
        out.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        out.arguments = ["bootout", "gui/\(uid)/\(label)"]
        out.standardOutput = FileHandle.nullDevice
        out.standardError = FileHandle.nullDevice
        try? out.run()
        out.waitUntilExit()

        // Drain — wait for launchd to actually unload the service before
        // bootstrapping again. Skips when never loaded (the fast path).
        waitUntilLabelStopped(label: label, timeoutSeconds: 10)

        // Bootstrap.
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        proc.arguments = ["bootstrap", "gui/\(uid)", plistPath]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        try proc.run()
        proc.waitUntilExit()
        if proc.terminationStatus != 0 {
            let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            throw LaunchAgentError.launchctlFailed(label: label, status: proc.terminationStatus, output: output)
        }
    }

    /// Poll `launchctl print gui/<uid>/<label>` until it exits non-zero
    /// (service is fully unloaded) or the timeout expires. Non-blocking
    /// to the launchctl process — uses short sleeps between checks.
    /// Returns true if the service stopped within the timeout, false
    /// otherwise (caller can still proceed; bootstrap will surface the
    /// real error if there is one).
    @discardableResult
    private func waitUntilLabelStopped(label: String, timeoutSeconds: Double) -> Bool {
        let uid = String(getuid())
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        let pollInterval: UInt32 = 100_000  // 100ms
        while Date() < deadline {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            proc.arguments = ["print", "gui/\(uid)/\(label)"]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            do {
                try proc.run()
            } catch {
                return true  // can't probe; let bootstrap try
            }
            proc.waitUntilExit()
            if proc.terminationStatus != 0 {
                return true  // service is gone
            }
            usleep(pollInterval)
        }
        return false  // timed out — still loaded
    }

    /// Bootout all com.sutando.* agents and remove their plists. Walks
    /// BOTH the new runDir and the legacy `~/Library/LaunchAgents/` —
    /// covers users upgrading from older versions whose plists may still
    /// live in the legacy directory.
    @discardableResult
    func uninstall() -> [String] {
        var removed: [String] = []
        for dir in [runDir, legacyLaunchAgentsDir] {
            removed.append(contentsOf: booteachLabel(in: dir, deletePlist: true))
        }
        // The core-agent wrapper (run-core-agent.sh) starts a detached tmux
        // server on /tmp/sutando-tmux.sock. tmux daemonizes out of the
        // wrapper's process tree, so unloading the launchd job leaves the
        // session running. Kill it explicitly here.
        killCoreAgentTmux()
        return removed
    }

    /// Bootout all com.sutando.* in `runDir` but leave the rendered plists
    /// in place. Used at `applicationWillTerminate` so cmd+Q shuts down
    /// every service without permanently uninstalling anything — the
    /// next app launch re-bootstraps from the same plists in `runDir`.
    @discardableResult
    func stopAll() -> [String] {
        let stopped = booteachLabel(in: runDir, deletePlist: false)
        killCoreAgentTmux()
        return stopped
    }

    /// One-time cleanup of plists left behind by older Sutando versions.
    /// Older versions installed into `~/Library/LaunchAgents/`, which
    /// causes services to auto-load on user login. The new lifecycle
    /// owns service start/stop via the app process, so we bootout +
    /// delete any legacy plists on first launch. Idempotent.
    @discardableResult
    func migrateLegacyPlists() -> [String] {
        booteachLabel(in: legacyLaunchAgentsDir, deletePlist: true)
    }

    /// Helper: bootout every com.sutando.* plist in `dir`. When
    /// `deletePlist` is true, removes the plist after bootout. Silent on
    /// errors — bootout fails routinely (e.g. service not loaded) and
    /// that's fine. Waits for each service to fully unload before
    /// returning (see `waitUntilLabelStopped`); a follow-up bootstrap
    /// against the same label would otherwise race a still-terminating
    /// service.
    @discardableResult
    private func booteachLabel(in dir: String, deletePlist: Bool) -> [String] {
        let uid = String(getuid())
        let files = (try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []
        var processed: [String] = []
        for file in files where file.hasPrefix("com.sutando.") && file.hasSuffix(".plist") {
            let label = String(file.dropLast(".plist".count))
            let plistPath = dir + "/" + file
            let out = Process()
            out.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            out.arguments = ["bootout", "gui/\(uid)/\(label)"]
            out.standardOutput = FileHandle.nullDevice
            out.standardError = FileHandle.nullDevice
            try? out.run()
            out.waitUntilExit()
            waitUntilLabelStopped(label: label, timeoutSeconds: 10)
            if deletePlist {
                try? FileManager.default.removeItem(atPath: plistPath)
            }
            processed.append(label)
        }
        return processed
    }

    private func killCoreAgentTmux() {
        let socket = "/tmp/sutando-tmux.sock"
        guard FileManager.default.fileExists(atPath: socket) else { return }
        let candidates = [
            (Bundle.main.resourcePath ?? "") + "/runtime/bin/tmux",
            "/opt/homebrew/bin/tmux",
            "/usr/local/bin/tmux",
            "/usr/bin/tmux",
        ]
        if let tmux = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: tmux)
            proc.arguments = ["-S", socket, "kill-server"]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            try? proc.run()
            proc.waitUntilExit()
        }
        try? FileManager.default.removeItem(atPath: socket)
    }

    /// Query loaded com.sutando.* agents. Returns labels for which
    /// `launchctl print gui/<uid>/<label>` exits 0. Walks both runDir
    /// and the legacy dir so the count is accurate during migration.
    func loadedLabels() -> [String] {
        let uid = String(getuid())
        var labels = Set<String>()
        for dir in [runDir, legacyLaunchAgentsDir] {
            let files = (try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []
            for file in files where file.hasPrefix("com.sutando.") && file.hasSuffix(".plist") {
                labels.insert(String(file.dropLast(".plist".count)))
            }
        }
        var loaded: [String] = []
        for label in labels {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            proc.arguments = ["print", "gui/\(uid)/\(label)"]
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            try? proc.run()
            proc.waitUntilExit()
            if proc.terminationStatus == 0 {
                loaded.append(label)
            }
        }
        return loaded
    }
}
