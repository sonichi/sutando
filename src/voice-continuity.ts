// voice-continuity — P7 D7.3 continuity helpers (Tranche A engine-side):
// the stale-repeat goodbye guard and the centralized conversation-clear.
// Both are policy the voice agent previously carried inline in three places;
// centralizing them is the fix for G-P7-8 (a clear path that empties bodhi's
// conversationContext without rebasing the transcript cursor makes the
// turn.end logger skip every item that accumulates after the clear).

/** State for the stale-repeat goodbye guard. */
export interface GoodbyeGuardState {
  lastText: string | null;
  userTurnsAtFire: number;
}

export function initialGoodbyeGuard(): GoodbyeGuardState {
  return { lastText: null, userTurnsAtFire: -1 };
}

/**
 * Should a strict-detected farewell fire session_end? A reconnect replay can
 * make the model repeat the SAME short farewell with no new real user turn —
 * that repeat must not re-fire the end-session machinery (the 2026-04-09
 * replay-contamination class; D7.3 Tranche A interim until bodhi's
 * epoch-fenced boundary lands). Pure: returns the decision plus next state.
 */
export function shouldFireGoodbye(
  state: GoodbyeGuardState,
  lastText: string,
  userTurnCount: number,
): { fire: boolean; next: GoodbyeGuardState } {
  if (state.lastText !== null && lastText === state.lastText && userTurnCount <= state.userTurnsAtFire) {
    return { fire: false, next: state };
  }
  return { fire: true, next: { lastText, userTurnsAtFire: userTurnCount } };
}

/**
 * Clear a dead session's Gemini resumption handle before a fresh reconnect
 * (field incident 2026-08-14): when Gemini invalidates a session it closes
 * 1008 "Requested entity was not found", bodhi transitions ACTIVE→CLOSED —
 * but nothing clears `transport.config.resumptionHandle`, and the CLOSED→
 * fresh-connect path replays it in `sessionResumption: { handle }`. Gemini
 * completes setup, then kills each new connection the same way (observed
 * survival staircase: 9 min → 18 s → 0.9 s). Clearing costs nothing here:
 * this path already rebuilds context via text injection, never via resume;
 * mid-session GoAway resume (the legitimate handle use) does not run
 * through it. Returns true when a stale handle was actually cleared.
 * Pin-workaround: belongs in bodhi's handleTransportClose (Tranche B).
 */
export function clearStaleResumptionHandle(session: unknown): boolean {
  let cleared = false;
  try {
    const s = session as {
      transport?: { config?: { resumptionHandle?: string } };
      sessionManager?: { clearResumptionHandle?: () => void };
    };
    if (s?.transport?.config?.resumptionHandle) {
      s.transport.config.resumptionHandle = undefined;
      cleared = true;
    }
    s?.sessionManager?.clearResumptionHandle?.();
  } catch {
    /* a missing seam must never break the reconnect path */
  }
  return cleared;
}

export interface ConversationClearHelper {
  /** The turn.end logger's cursor into conversationContext.items. */
  cursor: { index: number };
  /** Empty the items array IN PLACE (it is getter-backed — reassignment
   *  throws) and rebase the cursor with it. Returns items cleared. */
  clear(reason: string): number;
}

/**
 * The ONE way to empty bodhi's conversationContext (used by end_session, the
 * goodbye detector, and the sessionEnding turn.end sweep). Items and cursor
 * move together, always.
 */
export function createConversationClearHelper(
  getItems: () => unknown,
  log: (line: string) => void = () => {},
): ConversationClearHelper {
  const cursor = { index: 0 };
  return {
    cursor,
    clear(reason: string): number {
      let cleared = 0;
      try {
        const items = getItems();
        if (Array.isArray(items) && items.length > 0) {
          cleared = items.length;
          items.length = 0;
        }
      } catch (e) {
        log(`[clear-items] ${reason}: could not clear conversationContext: ${e}`);
      }
      // The cursor rebases even when the array was empty or unreadable — a
      // stale cursor against an emptied array is exactly the G-P7-8 bug.
      cursor.index = 0;
      if (cleared > 0) log(`[clear-items] ${reason}: cleared ${cleared} conversationContext items`);
      return cleared;
    },
  };
}
