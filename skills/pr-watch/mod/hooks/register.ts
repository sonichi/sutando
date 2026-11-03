import type { Register } from 'claude-code'
import { RUNNER, STATE_DIR } from './paths'

const DEDUPE_MS = 60_000
const RESPAWN_MS = 30_000
// pr_watch.py run: 3 means gh or its webhook extension is missing, 4 that another runner holds the lock.
const NO_RESPAWN = new Set([3, 4])

const TEMPLATE_MARK = 'PR_WATCH_PROMPT_TEMPLATE '

/** The template a `PR_WATCH_PROMPT_TEMPLATE <json>` line carries, or undefined when it carries none. */
export function parseTemplate(line: string): string | undefined {
  try {
    const t: unknown = JSON.parse(line.slice(TEMPLATE_MARK.length))
    return typeof t === 'string' ? t : undefined
  } catch {
    return undefined
  }
}

/** The complete event, note and template lines in `buffer`, and the unfinished tail; `template` is the last one sent. */
export function takeLines(buffer: string): { events: string[]; notes: string[]; template?: string; rest: string } {
  const parts = buffer.split('\n')
  const rest = parts.pop() ?? ''
  const lines = parts.map((l) => l.trim())
  const templates = lines.filter((l) => l.startsWith(TEMPLATE_MARK)).map(parseTemplate).filter((t) => t !== undefined)
  return {
    events: lines.filter((l) => l.startsWith('PR_EVENT ')),
    notes: lines.filter((l) => l.startsWith('PR_WATCH_NOTE ')).map((l) => l.slice('PR_WATCH_NOTE '.length)),
    template: templates.length ? templates[templates.length - 1] : undefined,
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

/** One message for any number of event lines, from the runner's template; the bare lines before one arrives. */
export function messageFor(template: unknown, lines: readonly string[]): string {
  const event = lines.join('\n')
  if (typeof template !== 'string' || !template.trim()) return event
  return template.includes('{event}') ? template.split('{event}').join(event) : `[pr-watch] ${event}\n${template}`
}

/** The message for the waiting lines, or null when none wait; the caller clears them once delivered. */
export function takeEvents(waiting: readonly string[], template: unknown): string | null {
  return waiting.length ? messageFor(template, waiting) : null
}

type CallResult = { deny?: unknown; context?: readonly string[]; [key: string]: unknown }

/** `r` with `text` appended to what the model reads after the tool, or null when `r` is a deny. */
export function withContext<R extends CallResult>(r: R, text: string): R | null {
  if (typeof r.deny === 'string') return null
  return { ...r, context: [...(r.context ?? []), text] }
}

// Module state: a reload starts these over, and session.start respawns the runner that fills them.
let pending: string[] = []
let turnRunning = false
let template: string | undefined

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
          const taken = takeLines(buffer + piece.text)
          buffer = taken.rest
          if (taken.template !== undefined) template = taken.template
          for (const n of taken.notes) $.ui.log(`pr-watch: ${n}`)
          for (const line of taken.events) {
            if (!dedupe.fresh(line, await $.clock.now())) continue
            pending.push(line)
            if (turnRunning) continue
            const text = takeEvents(pending, template)
            pending = []
            if (text) void $.prompt.submit({ text })
          }
        }
        $.ui.log(`pr-watch: runner exited (${end?.code ?? 'signal'})`, { to: 'debug' })
        if (end && end.code !== null && NO_RESPAWN.has(end.code)) return
        await $.clock.sleep(RESPAWN_MS)
      }
    })()
    return started
  })

  on('turn.start', async ($, e, next) => {
    turnRunning = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // Cleared before next(e): if anything beneath throws, held events must not wait forever.
    turnRunning = false
    const done = await next(e)
    const text = takeEvents(pending, template)
    pending = []
    if (text) void $.prompt.submit({ text })
    return done
  })

  // A prompt submitted mid-turn waits for idle, so events ride on the main loop's next tool result instead.
  on('tool.call', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId || !pending.length) return r
    const text = takeEvents(pending, template)
    const out = text ? withContext(r, text) : null
    if (!out) return r
    pending = []
    return out
  }).catch(($, e, next) => (next.called ? next(e) : { deny: 'pr-watch: hook failed before running the tool' }))
}
