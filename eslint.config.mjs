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

      // `any` is worth tracking but not worth blocking on: it sits mostly at
      // the untyped SDK / IPC boundaries. Warn so the count is visible and can
      // ratchet down, without failing the build today.
      '@typescript-eslint/no-explicit-any': 'warn',

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
);
