// The task-watch Mod: runs the session's task watcher, starts a turn with each task it announces, and names owner
// tasks that arrive mid-turn after the next tool result.

import type { EngineInterface, Register, ToolCallResult } from 'claude-code'
import { MIN_CLAUDE_VERSION } from './version'

const PLUGIN = 'task-watch'
const MOD_DIR = '/src/agent/claude/task-watch-mod'
const HELD_POLL_MS = 30_000
// The watcher prints QUEUE after a second process answers; a Monitor's 200 ms batch usually carries both.
const QUEUE_WAIT_MS = 1_500
const BACKOFF_BASE_MS = 5_000
const BACKOFF_MAX_MS = 300_000
const HEALTHY_RUN_MS = 600_000
const MAX_EARLY_EXITS = 8
const RETRY_SUBMIT_MS = 5_000
/** How long one submit may stay unsettled before its announcement is taken back and sent again. */
export const SUBMIT_TIMEOUT_MS = 30_000
/** How long after a submit resolves a main turn may take to start before the submit counts as not delivered. */
export const TURN_START_WAIT_MS = 15_000
const RESEND_MAX_MS = 60_000
// task_disposition.py is asked about each held task right before a submit: this long per answer, this many at once.
const DISPOSITION_TIMEOUT_MS = 10_000
const DISPOSITION_AT_ONCE = 4
const DISPOSITIONS = ['pending', 'claimed', 'answered', 'archived', 'missing']
// Mid-turn, each task's access_tier is read with the repo's own header parser: one line per path, empty when unreadable.
const TIER_TIMEOUT_MS = 5_000
const TIER_PY = [
  'import sys',
  'sys.path.insert(0, sys.argv[1])',
  'from local_task_protocol import canonical_access_tier, parse_task_headers',
  'for p in sys.argv[2:]:',
  '    try:',
  "        with open(p, encoding='utf-8', errors='replace') as f:",
  "            print(canonical_access_tier(parse_task_headers(f.read(262144)).headers.get('access_tier')))",
  '    except Exception:',
  "        print('')",
].join('\n')
// The engine's own refusal after the person pressed Stop, as a `{ drop }` reason. Claude Code 2.1.322 words
// it "the session was stopped and nobody has sent input since"; its types say "a Stop stands".
const STOP_DROP_RE = /\bstop/i
// The delivery trail, under the workspace: what the watcher said, what was submitted, and how each submit ended.
const TRAIL_FILE = 'logs/task-watch-mod.log'
const TRAIL_LINE_MAX = 2_000
const APPEND_SH = 'f="$1"; shift; mkdir -p "$(dirname "$f")" && for l in "$@"; do printf \'%s %s\\n\' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$l"; done >> "$f"'
// watch-tasks-stream.sh exits 0 when it hands off (the inbox is held, or it was told to stop) and 64 on a usage refusal.
const EXIT_HANDOFF = 0
const EXIT_USAGE = 64

const TASK_RE = /^TASK_FILE: (.+)$/
const QUEUE_RE = /^QUEUE: [0-9]{1,6} pending after this$/
const NAME_RE = /^[A-Za-z0-9._~+@:-]{1,255}$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/

/** Whether dotted release `have` is at least `min`; a `-dev` or other suffix is ignored. */
export function versionAtLeast(have: string, min: string): boolean {
  const parse = (v: string) => (v.match(/^\s*(\d+(?:\.\d+)*)/)?.[1] ?? '').split('.').filter(Boolean).map(Number)
  const a = parse(have)
  const b = parse(min)
  if (!a.length || !b.length) return false
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? 0
    const y = b[i] ?? 0
    if (x !== y) return x > y
  }
  return true
}

/** The repo checkout this Mod ships in, from its plugin root; null when it is not where the repo puts it. */
export function repoFromPluginRoot(root: string): string | null {
  const r = root.replace(/\/+$/, '')
  return r.endsWith(MOD_DIR) && r.length > MOD_DIR.length ? r.slice(0, -MOD_DIR.length) : null
}

/** True for a watcher announcement whose name is a plain file name, or an absolute path ending in one. */
export function isTaskLine(line: string): boolean {
  const m = TASK_RE.exec(line)
  if (!m || line.length > 4096 || CONTROL_RE.test(line)) return false
  const name = m[1]
  const slash = name.lastIndexOf('/')
  if (slash >= 0 && !name.startsWith('/')) return false
  return NAME_RE.test(name.slice(slash + 1))
}

/** The complete lines in `buffer`, and the unfinished tail. */
export function takeLines(buffer: string): { lines: string[]; rest: string } {
  const parts = buffer.split('\n')
  const rest = parts.pop() ?? ''
  return { lines: parts.map((l) => l.replace(/\r$/, '')), rest }
}

/** The wait before restart number `failures` (1-based): doubling from 5 s, capped at 5 min. */
export function backoffMs(failures: number): number {
  return Math.min(BACKOFF_BASE_MS * 2 ** Math.max(0, failures - 1), BACKOFF_MAX_MS)
}

/** The names of the tasks a message announces, in order, each once. */
export function taskNames(text: string): string[] {
  const names = text.split('\n').map((l) => TASK_RE.exec(l)?.[1]).filter((n): n is string => n !== undefined)
  return [...new Set(names)]
}

/**
 * `text` without the tasks in `handled`: unchanged when it names none of them, null when nothing is left.
 * Otherwise its QUEUE lines are written again, because the watcher counted tasks that are now done: each
 * task left gets the number of other tasks left in this message, and no line at 0, as the watcher prints it.
 */
export function withoutHandled(text: string, handled: ReadonlySet<string>): string | null {
  const tasks = text.split('\n').filter((l) => TASK_RE.test(l))
  const kept = tasks.filter((l) => !handled.has(TASK_RE.exec(l)?.[1] ?? ''))
  if (kept.length === tasks.length) return text
  if (!kept.length) return null
  const others = kept.length - 1
  return kept.map((l) => (others ? `${l}\nQUEUE: ${others} pending after this` : l)).join('\n')
}

/**
 * Which of `names` are owner tasks, from the tier helper's output (one tier per name, in order). Only an exact
 * `owner` counts: a missing line, any other word, or a helper that failed (null) leaves the task for the idle path.
 */
export function ownerTasks(names: readonly string[], tiers: string | null): string[] {
  if (tiers === null) return []
  const lines = tiers.split('\n')
  return names.filter((_, i) => lines[i]?.trim() === 'owner')
}

/** The line the model reads after a tool result when owner tasks arrived during its turn. */
export function midTurnNotice(names: readonly string[]): string {
  return [
    'New task(s) arrived while you were working (task-watch):',
    ...names.map((n) => `TASK_FILE: ${n}`),
    'Read each one. If it is quick, handle it fully now; otherwise say what you will do and when, then resume your current work.',
  ].join('\n')
}

/**
 * A tool result with `ask`'s line added to its context. A denied call, no line, or anything thrown on the way
 * returns `result` untouched: the Mod never fails or alters a tool call.
 */
export async function withTaskContext<R extends ToolCallResult>(result: R, ask: () => Promise<string | null>): Promise<R> {
  try {
    if (result.deny !== undefined) return result
    const line = await ask()
    if (!line) return result
    return { ...result, context: [...(result.context ?? []), line] }
  } catch {
    return result
  }
}

/**
 * The watcher's stdout turned into the text a Monitor notification carries: each TASK_FILE line, with the
 * QUEUE line that follows it. Every other line is returned to the caller to log and is never submitted.
 */
export class Announcements {
  private open: string | null = null
  private ready: string[] = []
  feed(line: string): 'task' | 'queue' | 'other' {
    if (isTaskLine(line)) {
      this.close()
      this.open = line
      return 'task'
    }
    if (this.open !== null && QUEUE_RE.test(line)) {
      this.ready.push(`${this.open}\n${line}`)
      this.open = null
      return 'queue'
    }
    this.close()
    return 'other'
  }
  hasOpen(): boolean {
    return this.open !== null
  }
  close(): void {
    if (this.open !== null) this.ready.push(this.open)
    this.open = null
  }
  /** Everything ready, as one message, or null; the caller puts it back with `unshift` if it could not send it. */
  take(): string | null {
    if (!this.ready.length) return null
    const text = this.ready.join('\n')
    this.ready = []
    return text
  }
  unshift(text: string): void {
    this.ready.unshift(text)
  }
  /** Takes back the text `unshift` put at the front, if it is still there. */
  shift(text: string): void {
    if (this.ready[0] === text) this.ready.shift()
  }
  /** How many tasks are held: every TASK_FILE line not yet taken. */
  count(): number {
    const held = this.ready.reduce((n, text) => n + text.split('\n').filter((l) => TASK_RE.test(l)).length, 0)
    return held + (this.open !== null ? 1 : 0)
  }
}

type Plan = { argv: string[]; inbox: string; py: string }
export type End = { code: number | null; signal: string | null }
export type Piece = { stream: string; text: string }
export type Child = { next: () => Promise<IteratorResult<Piece, End>>; return: (v?: unknown) => Promise<unknown> }

/** What the watch needs from the engine; register() builds it from `$`, the tests from fakes. */
export type Host = {
  /** A command's stdout when it exits 0, else null; `timeoutMs` shortens the 20 s it is given. */
  run: (argv: string[], timeoutMs?: number) => Promise<string | null>
  spawn: (argv: string[], cwd: string) => Child
  sleep: (ms: number) => Promise<void>
  now: () => Promise<number>
  /** Resolves once the engine answered: with the engine's reason when the prompt did not enter, else nothing. */
  submit: (text: string) => Promise<string | null | void>
  /** Appends the lines, each with the time, to a file; best-effort, never rejects. */
  record: (file: string, lines: string[]) => Promise<void>
  log: (text: string, debug?: boolean) => Promise<void>
  /** The Mod's one status line under the prompt; undefined removes it. Never throws. */
  status: (text: string | undefined) => void
  /** A toast under the Mod's name. Never throws. */
  toast: (text: string) => void
}

// A promise left running rejects when the module unloads under it; that is the expected end, not an error.
function detach(p: Promise<unknown>): void {
  p.catch(() => undefined)
}

/** One session's watcher: spawned, read, restarted, and stopped with the session. */
export class TaskWatch {
  stopped = false
  private turnRunning = false
  private child: Child | null = null
  private openToken = 0
  private ann = new Announcements()
  private sending = false
  private sent: Promise<void> = Promise.resolve()
  private submits = 0
  private mainTurns = 0
  private idleWaiters: (() => void)[] = []
  private starts: string[] = []
  private startWaiters: (() => void)[] = []
  private doubted: string | null = null
  private paused = false
  private toasted = false
  private shown = 0
  private unstarted = 0
  private trail: string | null = null
  private py: string | null = null
  private ws: string | null = null
  private inbox: string | null = null
  private checkBroken = false
  private midBusy = false
  // Held tasks found not to be owner tasks: they wait for the idle path and are not asked about again mid-turn.
  private idleOnly = new Set<string>()
  private notes: string[] = []
  private noting = false
  constructor(private host: Host, private repo: string, private submitTimeoutMs = SUBMIT_TIMEOUT_MS, private turnStartWaitMs = TURN_START_WAIT_MS) {}

  // turn.start and turn.complete also fire for each subagent's loop, with its `agentId`; only the main
  // loop's (no `agentId`) say whether the session is idle, so a subagent's are ignored in all three calls.
  turnStart(agentId?: string, text = ''): void {
    if (agentId !== undefined) return
    if (this.paused) {
      // Nothing is submitted while a Stop stands, so this main turn is the person's own input: the Stop is over.
      this.paused = false
      this.shown = 0
      this.unstarted = 0
      this.host.status(undefined)
      this.note(`the person sent input: the Stop is over; ${this.ann.count()} held task(s) go out when this turn ends`)
    }
    this.turnRunning = true
    this.mainTurns++
    this.starts.push(text)
    for (const wake of this.startWaiters.splice(0)) wake()
  }

  turnIdle(agentId?: string): void {
    if (agentId !== undefined) return
    this.turnRunning = false
    for (const wake of this.idleWaiters.splice(0)) wake()
  }

  /**
   * The person pressed Stop (the `session.stop` event, or a submit the engine refused for it). By the engine's
   * design no plugin input enters until the person sends input, so nothing is submitted or resent meanwhile:
   * announcements are held in order, and the wait is shown. The next main turn.start ends it.
   */
  stopStands(why: string): void {
    if (this.paused || this.stopped) return
    this.paused = true
    this.toasted = false
    this.note(`a Stop stands (${why}): holding announcements, no submit until the person sends input`)
    this.showWaiting()
  }

  /** While a Stop stands, or submits start no turn: the status line with the count, and one toast per wait. */
  private showWaiting(inFlight = 0): void {
    const n = this.paused || this.unstarted > 0 ? this.ann.count() + inFlight : 0
    if (!n || n === this.shown) return
    this.shown = n
    const line = `${n} task${n === 1 ? '' : 's'} waiting — type anything to resume`
    this.host.status(line)
    this.note(`${n} waiting, shown on the status line`)
    if (this.toasted) return
    this.toasted = true
    this.host.toast(line)
  }

  /** Submits start turns again: the wait is over and its status line goes. */
  private started(): void {
    this.unstarted = 0
    if (this.paused || !this.shown) return
    this.shown = 0
    this.toasted = false
    this.host.status(undefined)
  }

  /** Nothing was left to submit: with nothing else held either, the wait is over and its status line goes. */
  private nothingLeft(): void {
    this.note('nothing left to announce: every held task was already handled; no submit')
    if (this.ann.count()) {
      this.showWaiting()
      return
    }
    this.unstarted = 0
    if (!this.shown) return
    this.shown = 0
    this.toasted = false
    this.host.status(undefined)
  }

  /**
   * What is left of `text` once the tasks that are no longer pending are taken out. Asked right before each
   * submit, not when the watcher announces: a task that arrives mid-turn is pending then, and the Stop hook
   * has usually handed it to the model, which answered it, before the turn ends. src/task_disposition.py owns
   * the answer; this never looks for a result itself. Any word but `pending` (answered, archived, claimed,
   * missing) means there is nothing to announce. A check that fails, times out or prints anything else keeps
   * its task, which is announced as before; once a whole round of checks fails the rest are not asked.
   */
  private async stillPending(text: string): Promise<string | null> {
    const py = this.py
    const ws = this.ws
    if (!py || !ws) return text
    const names = taskNames(text)
    const handled = new Set<string>()
    let failed = 0
    for (let i = 0; i < names.length; i += DISPOSITION_AT_ONCE) {
      const round = names.slice(i, i + DISPOSITION_AT_ONCE)
      const words = await Promise.all(round.map(async (name) => {
        try {
          return ((await this.host.run([py, `${this.repo}/src/task_disposition.py`, '--workspace', ws, '--', name], DISPOSITION_TIMEOUT_MS)) ?? '').trim()
        } catch {
          return ''
        }
      }))
      let roundFailed = 0
      words.forEach((word, at) => {
        if (!DISPOSITIONS.includes(word)) {
          roundFailed++
          this.note(`task_disposition gave no answer for ${round[at]}; announced anyway`)
        } else if (word !== 'pending') {
          handled.add(round[at])
          this.note(`not announced: ${round[at]} is ${word}`)
        }
      })
      failed += roundFailed
      if (roundFailed === round.length && i + DISPOSITION_AT_ONCE < names.length) {
        this.note(`task_disposition is not answering; the other ${names.length - i - round.length} task(s) are announced unchecked`)
        break
      }
    }
    if (failed && !this.checkBroken) await this.host.log(`could not check ${failed} task(s) with task_disposition.py; announcing them anyway`)
    this.checkBroken = failed > 0
    return withoutHandled(text, handled)
  }

  /**
   * The context line for a main-loop tool call while a main turn runs, or null. Held owner tasks still pending
   * (stillPending) are taken out of the buffer and named once, so the submit at turn end does not send them
   * again; any other task stays held for that submit. No tool call runs while a Stop stands, and a subagent's
   * calls (`agentId`) never get one. One check at a time: a concurrent call finds nothing to do.
   */
  async midTurn(agentId?: string): Promise<string | null> {
    if (agentId !== undefined || !this.turnRunning || this.paused || this.stopped || this.midBusy) return null
    const py = this.py
    const inbox = this.inbox
    if (!py || !inbox) return null
    const held = this.ann.take()
    if (!held) return null
    const names = taskNames(held)
    this.idleOnly = new Set(names.filter((n) => this.idleOnly.has(n)))
    const fresh = names.filter((n) => !this.idleOnly.has(n))
    if (!fresh.length) {
      this.ann.unshift(held)
      return null
    }
    this.midBusy = true
    let back: string | null = held
    try {
      const paths = fresh.map((n) => (n.startsWith('/') ? n : `${inbox}/${n}`))
      const owners = ownerTasks(fresh, await this.host.run([py, '-I', '-c', TIER_PY, `${this.repo}/src`, ...paths], TIER_TIMEOUT_MS))
      for (const n of fresh) {
        if (owners.includes(n)) continue
        this.idleOnly.add(n)
        this.note(`kept for the turn's end: ${n} is not an owner task (or its tier could not be read)`)
      }
      if (!owners.length) return null
      const own = new Set(owners)
      const ownText = withoutHandled(held, new Set(names.filter((n) => !own.has(n))))
      const pending = ownText === null ? null : await this.stillPending(ownText)
      back = withoutHandled(held, own)
      if (pending === null) return null
      const sent = taskNames(pending)
      this.note(`delivered mid-turn via tool result: ${sent.join(' ')}`)
      return midTurnNotice(sent)
    } finally {
      if (back !== null) this.ann.unshift(back)
      this.midBusy = false
      if (this.shown && !this.ann.count()) {
        this.shown = 0
        this.toasted = false
        this.host.status(undefined)
      } else if (this.shown) this.showWaiting()
    }
  }

  /** Sends what waited for the turn to end; a hook must not await it, a submit can take its whole timeout. */
  flush(agentId?: string): Promise<void> {
    return agentId === undefined ? this.deliver() : Promise.resolve()
  }

  /** One line of the delivery trail, written in order and never awaited: a slow disk cannot hold a delivery. */
  private note(line: string): void {
    if (!this.trail) return
    this.notes.push(line.replace(/[\u0000-\u001f\u007f]+/g, ' ').slice(0, TRAIL_LINE_MAX))
    if (this.noting) return
    this.noting = true
    detach((async () => {
      try {
        while (this.notes.length && this.trail) await this.host.record(this.trail, this.notes.splice(0))
      } finally {
        this.noting = false
      }
    })())
  }

  /** Kills the child this watch spawned, and nothing else; after /clear or a resume the process still owes its inbox a watcher. */
  end(reason: string): void {
    if (reason === 'clear' || reason === 'resume') return
    this.stopped = true
    if (this.shown) this.host.status(undefined)
    // A send waiting for the main turn to end is released, so no promise outlives the watch.
    for (const wake of [...this.idleWaiters.splice(0), ...this.startWaiters.splice(0)]) wake()
    const child = this.child
    this.child = null
    if (child) detach(child.return(undefined))
  }

  /** Starts sending what is ready, unless a send is already under way; resolves when that send is over. */
  private deliver(): Promise<void> {
    if (!this.sending) this.sent = this.drain()
    return this.sent
  }

  /** Of the main turns started since the last submit was issued: one with this text, only others, or none. */
  private startSeen(text: string): 'ours' | 'other' | 'none' {
    if (this.starts.some((t) => t === '' || t.includes(text))) return 'ours'
    return this.starts.length ? 'other' : 'none'
  }

  /** Waits, up to the confirm window, for a main turn to start after a submit resolved. */
  private async turnStarted(text: string): Promise<'ours' | 'other' | 'none'> {
    const expired = this.host.sleep(this.turnStartWaitMs).then(() => true)
    for (;;) {
      const seen = this.startSeen(text)
      if (seen !== 'none' || this.stopped) return seen
      const woke = new Promise<boolean>((wake) => { this.startWaiters.push(() => wake(false)) })
      if (await Promise.race([expired, woke])) return this.startSeen(text)
    }
  }

  /**
   * Submits what is ready, one message at a time, in order. A submit that throws, or that has not settled
   * within the timeout, keeps its text: it goes back to the front after a wait and is sent again. A timed-out
   * submit that settles during that wait did deliver, and is not sent again; one that settles later than that
   * delivers its text a second time, which is the price of never losing one.
   *
   * Submits are only issued while no main turn runs, and the timeout only counts idle time: the engine holds
   * a submitted prompt until the session is idle, so one overtaken by a main turn is waiting, not stuck.
   *
   * A submit that resolves is not yet a delivery: after an interrupt the engine answers a submit and starts
   * no turn. The text is dropped only once a main turn starts with it (its `turn.start` text holds the
   * submitted text, or is empty). Refused by the engine, or with no main turn in the window, it goes back and
   * is sent again, waiting 5 s doubling to 60 s, for as long as it takes. A main turn with other text (the
   * person typed first) confirms nothing: the text is sent once more, and a second such turn is then taken
   * as delivery, so an engine that words its turns differently costs one repeat per announcement, not a loop.
   *
   * None of that runs while a Stop stands (stopStands): the loop ends, and whatever it holds goes back unsent.
   *
   * Everything taken from the buffer goes through stillPending first, so a first send, a resend and what a
   * Stop held are all checked at the moment they would be submitted.
   */
  private async drain(): Promise<void> {
    this.sending = true
    let timeouts = 0
    try {
      while (!this.stopped && !this.turnRunning && !this.paused) {
        const held = this.ann.take()
        if (!held) return
        const text = await this.stillPending(held)
        if (this.stopped) return
        if (text === null) {
          this.nothingLeft()
          continue
        }
        if (this.turnRunning || this.paused) {
          // A main turn began, or a Stop landed, during the check: back in front, checked again at the next flush.
          this.ann.unshift(text)
          this.showWaiting()
          continue
        }
        if (text !== held) this.showWaiting(taskNames(text).length)
        const id = ++this.submits
        let timedOut = false
        let landedLate = false
        this.starts = []
        this.note(`submit #${id} start: ${text.replace(/\n/g, ' | ')}`)
        const settled = (async () => this.host.submit(text))().then(
          (drop) => {
            const dropped = typeof drop === 'string' ? drop : null
            if (timedOut && dropped === null) {
              landedLate = true
              this.note(`submit #${id} settled after its timeout: delivered late`)
            }
            return { error: null as string | null, dropped }
          },
          (err) => {
            const error = err instanceof Error ? err.message : String(err)
            if (timedOut) this.note(`submit #${id} failed after its timeout (${error})`)
            return { error, dropped: null as string | null }
          },
        )
        let result: { error: string | null; dropped: string | null } | null = null
        for (;;) {
          const turnsBefore = this.mainTurns
          result = await Promise.race([settled, this.host.sleep(this.submitTimeoutMs).then(() => null)])
          if (result || this.stopped || (!this.turnRunning && this.mainTurns === turnsBefore)) break
          this.note(`submit #${id} still pending behind a main turn; not a timeout`)
          if (this.turnRunning) result = await Promise.race([settled, new Promise<null>((wake) => { this.idleWaiters.push(() => wake(null)) })])
          if (result || this.stopped) break
        }
        if (!result && this.stopped) return
        if (result && result.error === null) {
          timeouts = 0
          let why = `the engine did not take it (${result.dropped})`
          if (result.dropped !== null && STOP_DROP_RE.test(result.dropped)) this.stopStands(`the engine refused submit #${id}: ${result.dropped}`)
          if (result.dropped === null) {
            this.note(`submit #${id} ok`)
            const seen = await this.turnStarted(text)
            if (this.stopped) return
            if (seen === 'ours' || (seen === 'other' && this.doubted === text)) {
              this.started()
              this.doubted = null
              this.note(seen === 'ours' ? `submit #${id} confirmed: a main turn started with it` : `submit #${id} taken as delivered: a second main turn started with other text`)
              continue
            }
            if (seen === 'other') {
              this.doubted = text
              this.note(`submit #${id} resolved but the main turn that started has other text (${this.starts.map((t) => t.length).join(',')} chars); sending once more`)
              this.ann.unshift(text)
              continue
            }
            why = 'submit resolved but no turn started'
          }
          if (this.paused) {
            this.note(`submit #${id} did not start a turn; held, not resent`)
            this.ann.unshift(text)
            this.showWaiting()
            continue
          }
          const again = Math.min(backoffMs(++this.unstarted), RESEND_MAX_MS)
          this.note(`submit #${id}: ${why}; resending in ${again / 1000}s`)
          await this.host.log(`${why}; resending in ${again / 1000}s`)
          // Back in the buffer for the wait, so it is counted on the status line; in front, so order holds.
          this.ann.unshift(text)
          this.showWaiting()
          await this.host.sleep(again)
          if (result.dropped === null && this.startSeen(text) === 'ours') {
            this.ann.shift(text)
            this.started()
            this.note(`submit #${id} confirmed late: a main turn started with it; not sent again`)
          }
          continue
        }
        let wait = RETRY_SUBMIT_MS
        if (result) {
          this.note(`submit #${id} failed (${result.error}); kept, retrying in ${wait / 1000}s`)
          await this.host.log(`could not start a turn (${result.error}); retrying`)
        } else {
          timedOut = true
          wait = backoffMs(++timeouts)
          this.note(`submit #${id} timeout after ${this.submitTimeoutMs}ms; kept, retrying in ${wait / 1000}s`)
          await this.host.log(`a turn did not start within ${this.submitTimeoutMs / 1000}s (submit #${id}); retrying in ${wait / 1000}s`)
        }
        await this.host.sleep(wait)
        if (landedLate) {
          timeouts = 0
          this.note(`submit #${id} is not sent again: it was delivered late`)
          continue
        }
        this.ann.unshift(text)
      }
    } finally {
      this.sending = false
    }
  }

  /** The watcher's argv, from watcher_rearm.py, the owner of which inbox and command a session owes. */
  private async plan(): Promise<Plan | string> {
    const config = `${this.repo}/scripts/sutando-config.sh`
    const py = (await this.host.run(['bash', config, 'python-bin']))?.trim()
    if (!py) return 'no Python interpreter resolved'
    const ws = (await this.host.run(['bash', config, 'workspace']))?.trim()
    if (!ws) return 'no workspace resolved'
    this.trail = `${ws.replace(/\/+$/, '')}/${TRAIL_FILE}`
    this.py = py
    this.ws = ws
    const raw = await this.host.run([py, `${this.repo}/src/watcher_rearm.py`, 'watch-argv', '--repo', this.repo, '--workspace', ws])
    const argv = (raw ?? '').split('\n').filter((l) => l.length > 0)
    const at = argv.indexOf('--inbox')
    const inbox = at < 0 ? undefined : argv[at + 1]
    if (argv.length < 2 || !inbox) return 'watcher_rearm.py named no watcher command'
    this.inbox = inbox.replace(/\/+$/, '')
    return { argv, inbox, py }
  }

  /** yes, no or unknown: is a session-role watcher (ready or not) already on this inbox? */
  private async held(plan: Plan): Promise<string> {
    const r = await this.host.run([plan.py, `${this.repo}/src/watcher_identity.py`, 'role-present', 'session', '--inbox', plan.inbox])
    const word = (r ?? '').split(/\s+/)[0]
    return word === 'yes' || word === 'no' ? word : 'unknown'
  }

  private scheduleClose(): void {
    const token = ++this.openToken
    detach((async () => {
      await this.host.sleep(QUEUE_WAIT_MS)
      if (token !== this.openToken || !this.ann.hasOpen()) return
      this.ann.close()
      await this.deliver()
    })())
  }

  private async once(plan: Plan): Promise<End> {
    const child = this.host.spawn(plan.argv, this.repo)
    this.child = child
    let buffer = ''
    let lastErr = ''
    try {
      for (;;) {
        const r = await child.next()
        if (r.done) {
          this.note(`watcher ended (${r.value?.signal ?? `exit ${r.value?.code}`})`)
          if (lastErr) await this.host.log(`watcher said: ${lastErr}`, true)
          return (r.value ?? { code: null, signal: null }) as End
        }
        if (r.value.stream !== 'stdout') {
          lastErr = r.value.text.trim().split('\n').pop() ?? lastErr
          for (const said of r.value.text.split('\n')) if (said.trim()) this.note(`watcher stderr: ${said.trim()}`)
          continue
        }
        const taken = takeLines(buffer + r.value.text)
        buffer = taken.rest
        for (const line of taken.lines) {
          const kind = this.ann.feed(line)
          if (kind !== 'other') this.note(`watcher announced: ${line}`)
          if (kind !== 'other') this.showWaiting()
          if (kind === 'task') this.scheduleClose()
          else if (kind === 'other' && line.trim()) await this.host.log(line, !line.startsWith('WATCHER_HELD:'))
        }
        // Not awaited: a submit that does not settle must not stop this loop reading the watcher.
        detach(this.deliver())
      }
    } catch (err) {
      return { code: null, signal: `spawn failed: ${err instanceof Error ? err.message : String(err)}` }
    } finally {
      if (this.child === child) this.child = null
      this.ann.close()
      detach(this.deliver())
    }
  }

  async run(): Promise<void> {
    let failures = 0
    let plan: Plan | null = null
    while (!this.stopped) {
      if (!plan) {
        const p = await this.plan()
        if (typeof p === 'string') {
          failures++
          if (failures > MAX_EARLY_EXITS) return void (await this.host.log(`giving up: ${p}; the Stop hook asks for a Monitor watcher instead`))
          await this.host.log(`${p}; retrying`, true)
          await this.host.sleep(backoffMs(failures))
          continue
        }
        plan = p
      }
      if ((await this.held(plan)) === 'yes') {
        await this.host.sleep(HELD_POLL_MS)
        continue
      }
      if (this.stopped) return
      const startedAt = await this.host.now()
      await this.host.log(`watching ${plan.inbox}`, true)
      this.note(`watcher starting on ${plan.inbox}`)
      const end = await this.once(plan)
      if (this.stopped) return
      if (end.code === EXIT_USAGE) return void (await this.host.log(`the watcher refused its arguments (exit ${EXIT_USAGE}); not restarting`))
      if (end.code === EXIT_HANDOFF) {
        failures = 0
        await this.host.log('the watcher handed off (exit 0); restarting only once no session watcher holds the inbox', true)
        await this.host.sleep(HELD_POLL_MS)
        continue
      }
      failures = (await this.host.now()) - startedAt >= HEALTHY_RUN_MS ? 1 : failures + 1
      const why = end.signal ?? `exit ${end.code}`
      if (failures > MAX_EARLY_EXITS) {
        return void (await this.host.log(`the watcher ended early ${failures} times (last: ${why}); giving up, the Stop hook asks for a Monitor watcher instead`))
      }
      await this.host.log(`the watcher ended (${why}); restarting in ${backoffMs(failures) / 1000}s`)
      await this.host.sleep(backoffMs(failures))
    }
  }
}

function hostOf($: EngineInterface): Host {
  const log = async (text: string, debug = false) => {
    try {
      await $.ui.log(`${PLUGIN}: ${text}`, debug ? { to: 'debug' } : {})
    } catch {
      // A log line that cannot be drawn must not stop the watcher.
    }
  }
  return {
    run: async (argv, timeoutMs = 20_000) => {
      try {
        const r = await $.process.run(argv, { timeoutMs })
        return r.exitCode === 0 ? String(r.stdout ?? '') : null
      } catch {
        return null
      }
    },
    spawn: (argv, cwd) => $.process.spawn({ argv, cwd }) as unknown as Child,
    sleep: (ms) => $.clock.sleep(ms),
    now: () => $.clock.now(),
    submit: async (text) => {
      const entered = await $.prompt.submit({ text })
      return typeof entered?.drop === 'string' ? entered.drop : null
    },
    record: async (file, lines) => {
      try {
        await $.process.run(['bash', '-c', APPEND_SH, 'bash', file, ...lines], { timeoutMs: 10_000 })
      } catch {
        // The trail is best-effort: a line that cannot be written must not stop the watcher.
      }
    },
    log,
    status: (text) => {
      try {
        $.ui.status(text)
      } catch {
        // A status line that cannot be drawn must not stop the watcher.
      }
    },
    toast: (text) => {
      try {
        $.ui.toast(text, { timeoutMs: 15_000 })
      } catch {
        // Nor a toast.
      }
    },
  }
}

// Module state: a reload starts it over, and the unload kills the child the old module spawned.
let watch: TaskWatch | null = null

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (watch && !watch.stopped) return started
    if ((await $.env.get('SUTANDO_TASK_WATCH_MOD')) !== '1') return started
    const host = hostOf($)
    let have = 'unknown'
    try {
      const v = await $.session.version()
      have = String(v.base ?? v.version)
    } catch {
      // An engine without session.version predates every version this Mod was verified on.
    }
    if (!versionAtLeast(have, MIN_CLAUDE_VERSION)) {
      await host.log(`Claude Code ${have} is older than ${MIN_CLAUDE_VERSION}; not watching (start the watcher with a Monitor)`)
      return started
    }
    const repo = repoFromPluginRoot($.plugin.root)
    if (!repo) {
      await host.log(`loaded from ${$.plugin.root}, not from a checkout's ${MOD_DIR.slice(1)}; not watching`)
      return started
    }
    const w = new TaskWatch(host, repo)
    watch = w
    detach(w.run().finally(() => { w.stopped = true }))
    return started
  })

  on('turn.start', async ($, e, next) => {
    watch?.turnStart(e.agentId, typeof e.text === 'string' ? e.text : '')
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // Cleared before next(e): if anything beneath throws, held announcements must not wait forever.
    watch?.turnIdle(e.agentId)
    const done = await next(e)
    // Not awaited: this hook must return even when the submit it starts never settles.
    if (watch) detach(watch.flush(e.agentId))
    return done
  })

  // A main loop's tool call carries the owner tasks that arrived during its turn; see TaskWatch.midTurn.
  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    const w = watch
    if (!w || e.agentId !== undefined) return result
    return withTaskContext(result, () => w.midTurn(e.agentId))
  }).catch(($, e, next) => next(e)) // A hook that fails or overruns: next replays the tool's own result, untouched.

  // session.stop is not hooked here: on an engine without that event (2.1.322 has none) naming it stops this
  // whole module loading. The hold starts from the `{ drop }` a refused submit answers with; see stopStands.

  on('session.end', async ($, e, next) => {
    watch?.end(String(e.reason))
    return next(e)
  })
}
