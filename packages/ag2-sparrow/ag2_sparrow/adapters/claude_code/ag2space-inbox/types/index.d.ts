// What the plugin keeps in the session's state: its offers in flight, so a plugin reload inside a live
// session (module variables start over; `$.state` stays) finds them again. See hooks/register.ts `restore`.
export type KeptOffer = {
  file: string
  rid: string
  attempt: number
  msg: string
  hash: string
  token: string
  turnId: string
  phase: string
  stagedText?: string
  // The submit is known to have reached the session's queue.
  submitted?: boolean
  // Its turn started and the confirmation is not through yet (the CLI asked for a retry).
  confirming?: { turnId: string; hash: string; completed?: string }
  // Intake stopped waiting for it (its turn did not start, or its confirmation did not get through): its
  // turn, should it still start, is confirmed; else the next start answers it.
  unconfirmed?: boolean
  // When intake stops waiting for it, in ms since the epoch: absolute, so a reload does not start the wait over.
  waitUntil?: number
  // A confirmed turn taken back after a reload: when it is closed if its end is never seen, in ms since
  // the epoch. Set at the first reload that takes it back and kept, so a later reload does not extend it.
  endBy?: number
}

declare module 'claude-code' {
  interface PluginState {
    'ag2space-inbox': { offers: KeptOffer[] }
  }
}
