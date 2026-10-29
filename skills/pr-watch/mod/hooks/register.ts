import type { Register } from 'claude-code'
import { RUNNER, STATE_DIR } from './paths'

const DEDUPE_MS = 60_000
const RESPAWN_MS = 30_000
// pr_watch.py run: 3 means gh or its webhook extension is missing, 4 that another runner holds the lock.
const NO_RESPAWN = new Set([3, 4])

export const DEFAULT_PROMPT =
  '[pr-watch] {event}\n' +
  'Standing ask from the owner: keep watching these pull requests. If the head moved, re-review at the new head. ' +
  'If a review was requested from the owner, review it. Note merges and closes. ' +
  'Tell the owner only when something needs them; otherwise finish quietly.'

/** The complete `PR_EVENT` and `PR_WATCH_NOTE` lines in `buffer`, and the unfinished tail. */
export function takeLines(buffer: string): { events: string[]; notes: string[]; rest: string } {
  const parts = buffer.split('\n')
  const rest = parts.pop() ?? ''
  const lines = parts.map((l) => l.trim())
  return {
    events: lines.filter((l) => l.startsWith('PR_EVENT ')),
    notes: lines.filter((l) => l.startsWith('PR_WATCH_NOTE ')).map((l) => l.slice('PR_WATCH_NOTE '.length)),
    rest,
  }
}

/** Drops a line already seen within the window; GitHub redelivers and forwarders can overlap a restart. */
export class Dedupe {
  private seen = new Map<string, number>()
  constructor(private windowMs: number = DEDUPE_MS) {}
  fresh(line: string, now: number): boolean {
    for (const [k, t] of this.seen) if (t <= now - this.windowMs) this.seen.delete(k)
    if (this.seen.has(line)) return false
    this.seen.set(line, now)
    return true
  }
}

export function promptFor(template: unknown, line: string): string {
  const t = typeof template === 'string' && template.trim() ? template : DEFAULT_PROMPT
  return t.includes('{event}') ? t.split('{event}').join(line) : `[pr-watch] ${line}\n${t}`
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!RUNNER || !STATE_DIR) return started
    // The runner lives as long as this module: a reload or the session's end kills it, and the next load respawns it.
    void (async () => {
      const dedupe = new Dedupe()
      for (;;) {
        let buffer = ''
        const child = $.process.spawn({ argv: ['python3', RUNNER, '--state-dir', STATE_DIR, 'run'] })
        const it = child[Symbol.asyncIterator]()
        let end: { code: number | null } | undefined
        for (;;) {
          const r = await it.next()
          if (r.done) { end = r.value as { code: number | null }; break }
          const piece = r.value
          if (piece.stream !== 'stdout') continue
          const { events, notes, rest } = takeLines(buffer + piece.text)
          buffer = rest
          for (const n of notes) $.ui.log(`pr-watch: ${n}`)
          for (const line of events) {
            if (!dedupe.fresh(line, await $.clock.now())) continue
            let template: unknown
            try {
              template = JSON.parse(await $.fs.read(`${STATE_DIR}/config.json`)).prompt
            } catch {
              template = undefined
            }
            void $.prompt.submit({ text: promptFor(template, line) })
          }
        }
        $.ui.log(`pr-watch: runner exited (${end?.code ?? 'signal'})`, { to: 'debug' })
        if (end && end.code !== null && NO_RESPAWN.has(end.code)) return
        await $.clock.sleep(RESPAWN_MS)
      }
    })()
    return started
  })
}
