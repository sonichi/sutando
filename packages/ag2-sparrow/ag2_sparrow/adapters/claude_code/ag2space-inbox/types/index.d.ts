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
}

declare module 'claude-code' {
  interface PluginState {
    'ag2space-inbox': { offers: KeptOffer[] }
  }
}
