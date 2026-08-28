// --- composer read-back guard (qingyun blocker 1, #2133) ------------------------
// The enabled state of the publish button proves the composer is NON-EMPTY, not that
// it holds the EXACT requested text. Focus loss, a dropped keystroke, or non-keyboard
// Unicode insertion can therefore publish the wrong thing — and the dry-run path was
// equally blind: it reported `wouldPost: arg`, the text we ASKED for, never what the
// composer actually contains. Both paths now read the composer back and fail CLOSED
// on any mismatch.
//
// Exported pure so it is unit-testable without a browser — the comparison is where the
// bugs live (normalization), and a test that re-implements it would prove nothing.
export function normalizeComposerText(t) {
  // ONLY transformations provably injected by the editor (qingyun blocker 1, #2133).
  //
  // The previous version ended with `.trim()` and stripped per-line trailing spaces,
  // so composerMatches(' hello', 'hello') and composerMatches('hello ', 'hello') both
  // returned TRUE — i.e. the guard would green-light publishing text with the user's
  // requested edge whitespace silently removed. Posting is IRREVERSIBLE, so a guard
  // that tolerates lost significant characters is worse than no guard: it launders a
  // mismatch as a match.
  //
  // Whitespace the USER asked for is significant and is now preserved on both sides.
  // If X's composer turns out to inject its own edge whitespace, the guard fails
  // CLOSED (refuses to post) rather than silently accepting a diff — which is the
  // correct direction for an irreversible action, and matches this module's stated
  // fail-closed intent. Fix that case with an observed, documented artifact rule; do
  // not widen the normalizer back out to a blanket trim.
  return String(t ?? '')
    .normalize('NFC')                  // X can emit decomposed forms for accents/CJK
    .replace(/\u200B|\uFEFF/g, '')     // zero-width space / BOM the editor injects
    .replace(/\r\n?/g, '\n');          // CRLF -> LF (contenteditable line endings)
}

export function composerMatches(requested, actual) {
  return normalizeComposerText(requested) === normalizeComposerText(actual);
}
