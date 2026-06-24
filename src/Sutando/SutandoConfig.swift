// SutandoConfig.swift — Swift twin of src/sutando_config.{py,ts}.
//
// Canonical loader for sutando.config.json + sutando.config.local.json,
// shared by the macOS menubar app (Sutando.app). Same resolution order,
// deep-merge semantics, comment-stripping, and ${REPO_DIR} expansion
// as the Python and TypeScript twins — must agree byte-for-byte on the
// resolved workspace so menubar app + bridges + voice agent all land in
// the same directory.
//
// Resolution order (v0.8):
//   1. sutando.config.local.json (per-clone override, gitignored)
//   2. sutando.config.json (tracked defaults at repo root)
//   3. Baked-in default ({repoRoot}/workspace)
//
// $SUTANDO_WORKSPACE is no longer honored in production. If set, a one-time
// warning points at scripts/sutando-migrate.sh. SUTANDO_TEST_MODE=1 preserves
// the env override only for test fixtures, matching the Python/TS twins.
//
// Foundation-only — no extra deps. Swift 5+.

import Foundation

enum SutandoConfig {

    // ---------------------------------------------------------------------
    //  File discovery
    // ---------------------------------------------------------------------

    private static let configFilename = "sutando.config.json"
    private static let localFilename = "sutando.config.local.json"
    private static let hardcodedWorkspaceDefaultRel = "workspace"

    /// Walk upward from `start` until we find a directory containing
    /// `sutando.config.json`. Returns nil if not found within 6 hops.
    /// Anchors on the config file rather than `.git/` so app bundles +
    /// symlinked installs resolve correctly.
    private static func findRepoRoot(start: String) -> String? {
        var cur = (start as NSString).standardizingPath
        for _ in 0..<6 {
            let candidate = (cur as NSString).appendingPathComponent(configFilename)
            if FileManager.default.fileExists(atPath: candidate) {
                return cur
            }
            let parent = (cur as NSString).deletingLastPathComponent
            if parent == cur { return nil }  // filesystem root
            cur = parent
        }
        return nil
    }

    // ---------------------------------------------------------------------
    //  JSON loading + comment stripping
    // ---------------------------------------------------------------------

    /// Recursively drop dict keys whose name starts with `_`.
    /// Comment convention: `_comment`, `_note`, etc. are stripped before
    /// validation so the `.example` file can carry inline documentation
    /// without polluting the runtime schema.
    private static func stripComments(_ obj: Any) -> Any {
        if let dict = obj as? [String: Any] {
            var out: [String: Any] = [:]
            for (k, v) in dict where !k.hasPrefix("_") {
                out[k] = stripComments(v)
            }
            return out
        }
        if let arr = obj as? [Any] {
            return arr.map { stripComments($0) }
        }
        return obj
    }

    /// Read + parse a JSON file, strip comment keys, return the dict.
    /// Empty/missing file → empty dict. Parse error → throws.
    private static func loadJson(at path: String) throws -> [String: Any] {
        guard FileManager.default.fileExists(atPath: path) else { return [:] }
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        guard !data.isEmpty else { return [:] }
        guard let trimmed = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty else { return [:] }
        guard let trimmedData = trimmed.data(using: .utf8) else { return [:] }
        let parsed = try JSONSerialization.jsonObject(with: trimmedData, options: [])
        guard let dict = parsed as? [String: Any] else {
            throw NSError(
                domain: "SutandoConfig", code: 1,
                userInfo: [NSLocalizedDescriptionKey:
                    "sutando config: \(path) top-level must be a JSON object"]
            )
        }
        return stripComments(dict) as? [String: Any] ?? [:]
    }

    // ---------------------------------------------------------------------
    //  Deep merge + variable expansion
    // ---------------------------------------------------------------------

    /// Recursively merge `override` into `base`. Dicts merge; everything
    /// else (arrays, scalars) is REPLACED. Returns a new dict.
    private static func deepMerge(
        _ base: [String: Any], _ override: [String: Any]
    ) -> [String: Any] {
        var out = base
        for (k, v) in override {
            if let vDict = v as? [String: Any],
               let baseDict = out[k] as? [String: Any] {
                out[k] = deepMerge(baseDict, vDict)
            } else {
                out[k] = v
            }
        }
        return out
    }

    /// Expand `${REPO_DIR}` in every string value of the config tree.
    /// Other variables pass through untouched.
    private static func expandVars(_ obj: Any, repoDir: String) -> Any {
        let token = "${REPO_DIR}"
        if let s = obj as? String {
            return s.replacingOccurrences(of: token, with: repoDir)
        }
        if let dict = obj as? [String: Any] {
            var out: [String: Any] = [:]
            for (k, v) in dict { out[k] = expandVars(v, repoDir: repoDir) }
            return out
        }
        if let arr = obj as? [Any] {
            return arr.map { expandVars($0, repoDir: repoDir) }
        }
        return obj
    }

    // ---------------------------------------------------------------------
    //  Top-level loader (per-process cache)
    // ---------------------------------------------------------------------

    nonisolated(unsafe) private static var cache: [String: Any]?
    nonisolated(unsafe) private static var cacheRepoRoot: String?
    nonisolated(unsafe) private static var legacyEnvWarnPrinted = false
    nonisolated(unsafe) private static var dotenvDriftWarnPrinted = false

    /// Load + merge sutando config from disk. Memoized per-process.
    ///
    /// `repoRoot` is the directory containing `sutando.config.json`;
    /// defaults to `findRepoRoot(start:)` from the executable's parent
    /// (matching the historical AppDelegate.repoRoot CLAUDE.md walk-up).
    ///
    /// Throws on parse errors; missing files are tolerated.
    static func loadConfig(repoRoot explicitRoot: String? = nil) throws -> [String: Any] {
        if let c = cache, explicitRoot == nil || explicitRoot == cacheRepoRoot {
            return c
        }
        let root: String?
        if let r = explicitRoot {
            root = r
        } else {
            // Walk up from the executable, matching AppDelegate.repoRoot.
            let exe = URL(fileURLWithPath: CommandLine.arguments[0])
                .resolvingSymlinksInPath()
                .deletingLastPathComponent().path
            root = findRepoRoot(start: exe)
        }
        guard let r = root else {
            cache = [:]
            cacheRepoRoot = nil
            return [:]
        }
        let defaults = try loadJson(at: (r as NSString).appendingPathComponent(configFilename))
        let overrides = try loadJson(at: (r as NSString).appendingPathComponent(localFilename))
        let merged = deepMerge(defaults, overrides)
        let expanded = expandVars(merged, repoDir: r) as? [String: Any] ?? [:]
        cache = expanded
        cacheRepoRoot = r
        return expanded
    }

    /// Test-only: clear the per-process cache.
    static func resetCacheForTests() {
        cache = nil
        cacheRepoRoot = nil
        legacyEnvWarnPrinted = false
        dotenvDriftWarnPrinted = false
    }

    // ---------------------------------------------------------------------
    //  Public path resolvers
    // ---------------------------------------------------------------------

    /// Resolve the workspace directory per the canonical contract.
    /// Returns an absolute path. Does NOT create the directory.
    ///
    /// Order:
    ///   1. config workspace.path (deep-merged)
    ///   2. {repoRoot}/workspace baked-in default
    ///
    /// $SUTANDO_WORKSPACE is ignored in production as of v0.8 (warn once).
    /// SUTANDO_TEST_MODE=1 keeps the env override for tests only.
    static func resolveWorkspace(repoRoot explicitRoot: String? = nil) -> String {
        let env = ProcessInfo.processInfo.environment["SUTANDO_WORKSPACE"]?
            .trimmingCharacters(in: .whitespaces)
        if let env = env, !env.isEmpty {
            if ProcessInfo.processInfo.environment["SUTANDO_TEST_MODE"] == "1" {
                return (env as NSString).expandingTildeInPath
            }
            if !legacyEnvWarnPrinted {
                legacyEnvWarnPrinted = true
                FileHandle.standardError.write(Data((
                    "sutando config: $SUTANDO_WORKSPACE is set but NO LONGER HONORED " +
                    "(removed in v0.8). Workspace resolves only from " +
                    "sutando.config.{json,local.json} or the {repoRoot}/workspace " +
                    "baked-in default. If existing workspace data lives at the " +
                    "$SUTANDO_WORKSPACE path, run `bash scripts/sutando-migrate.sh " +
                    "--dry-run` then `--commit` to relocate. Unset the env to " +
                    "silence this warning.\n"
                ).utf8))
            }
        }

        let cfg = (try? loadConfig(repoRoot: explicitRoot)) ?? [:]
        let root = explicitRoot ?? cacheRepoRoot
        let resolved: String
        if let ws = cfg["workspace"] as? [String: Any],
           let path = ws["path"] as? String, !path.isEmpty {
            resolved = (path as NSString).expandingTildeInPath
        } else if let r = root {
            resolved = (r as NSString).appendingPathComponent(hardcodedWorkspaceDefaultRel)
        } else {
            // Last-ditch fallback for ad-hoc invocations outside a checkout.
            // Post-v0.8 (#1440 + Mini opinion-requested 2026-06-06), the legacy
            // `.sutando/workspace/` namespace is gone; use the unhidden
            // `~/sutando-workspace/` default instead so the deprecated
            // `.sutando/` alias doesn't live on indefinitely. Mirrors
            // `src/sutando_config.py`'s no-config-no-repo-root branch.
            resolved = NSHomeDirectory() + "/sutando-workspace"
        }

        // .env drift warning (mirrors the Python + TS twins)
        if !dotenvDriftWarnPrinted {
            dotenvDriftWarnPrinted = true
            if detectEnvWorkspaceInDotenv(repoRoot: explicitRoot) != nil {
                FileHandle.standardError.write(Data((
                    "sutando config: .env declares SUTANDO_WORKSPACE but the env var " +
                    "is no longer honored (removed in v0.8). Workspace resolves " +
                    "config-driven. Delete the .env line and, if needed, move the " +
                    "value to sutando.config.local.json under workspace.path.\n"
                ).utf8))
            }
        }

        return resolved
    }

    /// Scan the repo's `.env` for SUTANDO_WORKSPACE=. Best-effort.
    static func detectEnvWorkspaceInDotenv(repoRoot explicitRoot: String? = nil) -> String? {
        let root: String?
        if let r = explicitRoot { root = r }
        else {
            let exe = URL(fileURLWithPath: CommandLine.arguments[0])
                .resolvingSymlinksInPath()
                .deletingLastPathComponent().path
            root = findRepoRoot(start: exe)
        }
        guard let r = root else { return nil }
        let envFile = (r as NSString).appendingPathComponent(".env")
        guard FileManager.default.fileExists(atPath: envFile),
              let text = try? String(contentsOfFile: envFile, encoding: .utf8) else {
            return nil
        }
        for rawLine in text.split(separator: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("#") || !line.contains("=") { continue }
            let parts = line.split(separator: "=", maxSplits: 1).map(String.init)
            guard parts.count == 2, parts[0].trimmingCharacters(in: .whitespaces) == "SUTANDO_WORKSPACE" else {
                continue
            }
            var v = parts[1].trimmingCharacters(in: .whitespaces)
            if v.count >= 2,
               let first = v.first, let last = v.last,
               first == last, (first == "\"" || first == "'") {
                v = String(v.dropFirst().dropLast())
            }
            if v.isEmpty { return nil }
            return (v as NSString).expandingTildeInPath
        }
        return nil
    }
}
