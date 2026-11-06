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

// OBSERVED ARTIFACT (measured 2026-09-08 against the live composer, not inferred):
// X renders each EMPTY line as its own paragraph, so `innerText` emits two newlines
// where the request had one. Requested-vs-read-back, one run between two words:
//
//     requested \n x1 -> composer \n x1     (no artifact)
//     requested \n x2 -> composer \n x3
//     requested \n x3 -> composer \n x5
//     requested \n x4 -> composer \n x7     i.e. composer = 2n-1 for n >= 2
//
// So EVERY multi-paragraph post failed the guard and could not be published at all.
// The inverse is applied to the READ-BACK side only, and only to the odd run lengths
// the artifact can produce: m -> (m+1)/2 for odd m >= 3. An even run is not a shape
// this artifact generates, so it is left alone and still fails closed — per this
// module's rule that an unmeasured difference must refuse, never be laundered.
function undoEmptyLineDoubling(t) {
  // INTERIOR runs only — a non-newline on both sides. That is the boundary the
  // table above was measured at, and the edges do NOT follow the same law:
  // measured 2026-09-08, requested "\n\nAAA" reads back as "\n\n\n\nAAA" (four, not
  // the interior three), and a trailing run the composer drops outright. Widening
  // past the measured interior would launder an unmeasured difference, which is
  // the failure this module exists to prevent.
  return t.replace(/(?<=[^\n])\n{3,}(?=[^\n])/g, (run) =>
    run.length % 2 === 1 ? '\n'.repeat((run.length + 1) / 2) : run);
}

export function composerMatches(requested, actual) {
  const r = normalizeComposerText(requested);
  const a = normalizeComposerText(actual);
  // Asymmetric on purpose: the artifact is the editor's, so it is undone on the
  // editor's side. Never transform what the caller asked for.
  return r === a || r === undoEmptyLineDoubling(a);
}
