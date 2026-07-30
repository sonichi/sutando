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
  return String(t ?? '')
    .normalize('NFC')                  // X can emit decomposed forms for accents/CJK
    .replace(/\u200B|\uFEFF/g, '')     // zero-width space / BOM the editor injects
    .replace(/\r\n?/g, '\n')           // CRLF -> LF
    .replace(/[ \t]+$/gm, '')          // trailing spaces per line
    .trim();
}

export function composerMatches(requested, actual) {
  return normalizeComposerText(requested) === normalizeComposerText(actual);
}
