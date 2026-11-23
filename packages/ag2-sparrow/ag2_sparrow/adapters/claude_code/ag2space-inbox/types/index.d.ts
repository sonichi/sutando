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
}

declare module 'claude-code' {
  interface PluginState {
    'ag2space-inbox': { offers: KeptOffer[] }
  }
}
