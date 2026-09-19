// Minimal ESLint baseline for the TypeScript sources.
//
// Deliberately narrow: the non-type-checked recommended set only. It catches
// real defects (unused vars, unreachable code, dead branches) without the
// whole-project type-graph cost of the type-checked presets — which is also
// what promise-misuse rules (no-misused-promises, no-floating-promises) need,
// so those are NOT covered here. No formatter either — tsc already covers
// types, and formatting is out of scope for this baseline.
//
// Scope is all first-party TypeScript: src/, skills/, tests/. The rule set is
// shared — the idioms the relaxations exist for (fail-open catch, deliberate
// double-escaping, optional-dependency @ts-ignore) appear in all three trees.

import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: ['node_modules/**', 'packages/**', 'workspace/**', 'dist/**'],
  },
  {
    files: ['src/**/*.ts', 'skills/**/*.ts', 'tests/**/*.ts'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    linterOptions: {
      // The tree already carries hand-written eslint-disable comments (no-console,
      // ban-types) for rules this baseline doesn't enable. Don't report them as
      // unused — `--fix` would strip comments that encode author intent.
      reportUnusedDisableDirectives: 'off',
    },
    rules: {
      // Leading-underscore args/vars are an intentional "unused on purpose"
      // marker throughout src/ — honour it rather than rewriting call sites.
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],

      // `try { bestEffort(); } catch {}` is the deliberate fail-open idiom in
      // src/ — optional subprocess calls and cleanup that must never break the
      // caller. 84 hits at baseline, essentially all intentional.
      'no-empty': ['error', { allowEmptyCatch: true }],

      // Ratcheted: ERROR everywhere by default, downgraded to a warning only for
      // the files that already carry `any` (the allowlist at the bottom of this
      // file). 142 of 161 files are clean, so this locks in the clean majority —
      // a new `any` in any of them, or in any NEW file, fails the build.
      //
      // Deliberately not a mass retype: the remaining 114 sit at untyped SDK and
      // IPC boundaries (bodhi VoiceSession, Gemini payloads, fetch stubs).
      // Replacing them properly is a typing exercise per call site, not a
      // find-and-replace, and does not belong in a lint PR.
      '@typescript-eslint/no-explicit-any': 'error',

      // Both flag correct, deliberate code: ANSI/control-character stripping in
      // inline-tools.ts and task-bridge.ts, and an emoji-aware character class
      // in browser-tools.ts. The regexes are right; the rules are just noisy here.
      'no-control-regex': 'off',
      'no-misleading-character-class': 'off',

      // MUST stay off. web-client.ts / inline-tools.ts build browser JS inside
      // template literals, where a backslash is eaten by the template parser —
      // so regexes there are DELIBERATELY double-escaped (see the "single \ is
      // eaten by the template literal parser" note at web-client.ts:1267).
      // ESLint reads those as plain JS and calls them useless; auto-fixing them
      // rewrites /\s+/ to /s+/ in the shipped browser code. Silent breakage.
      'no-useless-escape': 'off',

      // Changing these alters runtime behaviour (a lazy require, and error
      // `cause` chaining). Out of scope for a lint-introduction PR — warn now,
      // fix deliberately later.
      '@typescript-eslint/no-require-imports': 'warn',
      'preserve-caught-error': 'warn',

      // `@ts-ignore` is required (not merely preferred) for the optional-dependency
      // imports: with the package installed, `@ts-expect-error` fails as an unused
      // directive — see the note at cartesia-tts.ts:18. Keep the rule's real value
      // by still demanding a written justification on every suppression.
      '@typescript-eslint/ban-ts-comment': [
        'error',
        { 'ts-ignore': 'allow-with-description', minimumDescriptionLength: 10 },
      ],
    },
  },

  // --- Plain JavaScript ------------------------------------------------------
  // Build/utility scripts and the Electron overlay app. These are NOT compiled
  // by tsc (the tsconfigs are .ts-only), so before this block they had no static
  // checking of any kind — not even the unused-import and undefined-variable
  // checks the TypeScript tree gets for free.
  //
  // typescript-eslint is deliberately not applied here: these are real .js/.mjs
  // files, so the base ESLint rules are the correct tool. That means the base
  // `no-unused-vars` (not the @typescript-eslint/ variant) and, crucially,
  // `no-undef` — which is off for TS (tsc owns it) but is the main value here.
  {
    files: ['**/*.mjs', '**/*.js'],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: { ...globals.node },
    },
    rules: {
      'no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      'no-empty': ['error', { allowEmptyCatch: true }],
      'no-useless-escape': 'off',
    },
  },

  // Electron main process + preload are CommonJS (`require`, `module.exports`),
  // not ESM — parsing them as modules makes `require` an undefined global.
  {
    files: ['skills/overlay-apps/app/main.js', 'skills/overlay-apps/app/preload.js', 'skills/overlay-apps/app/control-server.js'],
    languageOptions: { sourceType: 'commonjs', globals: { ...globals.node } },
  },

  // Renderer-process scripts run in a browser context: `document`, `window`,
  // and the `overlay` bridge that preload.js exposes via contextBridge.
  {
    files: ['skills/overlay-apps/app/stats-renderer.js', 'skills/overlay-apps/app/stats.js'],
    languageOptions: { sourceType: 'script', globals: { ...globals.browser } },
  },
  // --- no-explicit-any ratchet allowlist --------------------------------------
  // THIS LIST ONLY SHRINKS. It is the set of files that already contained `any`
  // when the rule was promoted to an error; for them the rule stays a warning so
  // the build is not held hostage to a boundary-typing refactor.
  //
  // Everywhere else `no-explicit-any` is an ERROR — so a new `any` cannot enter
  // a clean file, and a brand-new file starts out held to the strict rule.
  //
  // To clear an entry: type the call sites in that file, then delete its line.
  // Do not add entries. If a new file needs `any` at a genuine boundary, use a
  // scoped `// eslint-disable-next-line @typescript-eslint/no-explicit-any` with
  // a reason, so the exception is visible at the call site rather than here.
  //
  // Counts are at the time of writing (114 total) and are documentation, not
  // enforcement — the linter does not check them.
  {
    files: [
      'src/browser-tools.ts',                                          // 3
      'src/cartesia-stt-provider.ts',                                  // 1
      'src/observability/claude/jsonl-tail.ts',                        // 2
      'src/recording-tools.ts',                                        // 8
      'src/voice-agent.ts',                                            // 16
      'src/web-client.ts',                                             // 5
      'src/web-voice-transport.ts',                                    // 4
      'tests/active-artifact.test.ts',                                 // 6
      'tests/agent-state-endpoint.test.ts',                            // 1
      'tests/agent/claude/cli/build-core-settings.test.ts',            // 1
      'tests/cartesia-stt-provider.test.ts',                           // 18
      'tests/get-core-status-tool.test.ts',                            // 1
      'tests/observability/claude/hooks/build-hook-settings.test.ts',  // 3
      'tests/reachability-endpoints.test.ts',                          // 8
      'tests/result-channel-key.test.ts',                              // 1
      'tests/screen-companion-vision-query.test.ts',                   // 18
      'tests/screen-companion-work-retained.test.ts',                  // 3
      'tests/web-voice-transport.test.ts',                             // 1
    ],
    rules: { '@typescript-eslint/no-explicit-any': 'warn' },
  },
);
