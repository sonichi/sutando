#!/usr/bin/env node
/**
 * Composer read-back guard — qingyun blocker 1 on #2133.
 *
 * WHY THIS EXISTS: the publish path used the button's *enabled* state as its only
 * evidence that the composer held the right text. Enabled proves NON-EMPTY, not EXACT.
 * A dropped keystroke, focus loss, or non-keyboard Unicode insertion could publish
 * something other than what was asked for — irreversibly, to a public account. The
 * dry-run path was equally blind: it echoed `wouldPost: arg`, the text we *requested*,
 * never what the composer actually contained, so a dry-run could "pass" while the real
 * composer held mangled text.
 *
 * These tests import the PRODUCTION comparison from composer-text.mjs — the same module
 * x-post-browser.mjs imports. They deliberately do NOT re-implement normalization: a test
 * that mirrors the logic it checks can pass while the shipped path regresses — the exact
 * defect flagged on #1414.
 *
 * Run: node tests/x-post-composer-readback.test.mjs
 */
import { normalizeComposerText, composerMatches } from '../skills/x-twitter/composer-text.mjs';

let failures = 0;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  ok   ' : '  FAIL ') + name + (!cond && detail ? ` — ${detail}` : ''));
  if (!cond) failures++;
};

console.log('composer read-back guard');

// --- must MATCH: benign editor-side transformations -----------------------------
check('identical text matches', composerMatches('hello world', 'hello world'));
check('zero-width space between words is NOT silently accepted',
  composerMatches('hello world', 'hello​world') === false,
  'ZWSP joins the words — meaning changed, must mismatch');
check('leading/trailing whitespace tolerated', composerMatches('hello', '  hello  '));
check('CRLF vs LF tolerated', composerMatches('a\nb', 'a\r\nb'));
check('trailing per-line spaces tolerated', composerMatches('a\nb', 'a   \nb'));
check('BOM stripped', composerMatches('hi', '﻿hi'));

// --- emoji + CJK (explicitly requested in the review) ---------------------------
check('emoji round-trips', composerMatches('ship it 🚀', 'ship it 🚀'));
check('multi-codepoint emoji (ZWJ family) round-trips',
  composerMatches('👨‍👩‍👧‍👦 family', '👨‍👩‍👧‍👦 family'));
check('skin-tone modifier round-trips', composerMatches('👋🏽 hi', '👋🏽 hi'));
check('CJK round-trips', composerMatches('发布测试', '发布测试'));
check('mixed CJK + emoji + latin round-trips', composerMatches('发布 ship 🚀', '发布 ship 🚀'));
check('NFD-decomposed accent normalizes to NFC and matches',
  composerMatches('café', 'café'),
  'X can emit decomposed forms; NFC normalization must reconcile them');
check('CJK precomposed vs decomposed reconciled',
  composerMatches('ガ', 'ガ'), 'ガ vs KA + dakuten');

// --- must MISMATCH: the failures this guard exists to catch ---------------------
check('truncated text is caught', !composerMatches('hello world', 'hello wor'));
check('dropped leading char is caught', !composerMatches('hello', 'ello'));
check('extra trailing char is caught', !composerMatches('hello', 'hellox'));
check('empty composer is caught', !composerMatches('hello', ''));
check('wholly different text is caught', !composerMatches('post A', 'post B'));
check('emoji dropped by non-keyboard insertion is caught',
  !composerMatches('ship it 🚀', 'ship it'));
check('CJK partially typed is caught', !composerMatches('发布测试', '发布'));
check('case difference is caught', !composerMatches('Hello', 'hello'));

// --- normalize is total: never throws on odd input ------------------------------
check('normalize handles null', normalizeComposerText(null) === '');
check('normalize handles undefined', normalizeComposerText(undefined) === '');
check('normalize handles number', normalizeComposerText(42) === '42');

console.log();
if (failures) {
  console.log(`FAILED (${failures})`);
  process.exit(1);
}
console.log('composer read-back guard holds: exact-match required, editor noise tolerated.');
