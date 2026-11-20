// The task-watch Mod: runs the session's task watcher and starts a turn with each task it announces.

import type { EngineInterface, Register } from 'claude-code'
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
}

type Plan = { argv: string[]; inbox: string; py: string }
export type End = { code: number | null; signal: string | null }
export type Piece = { stream: string; text: string }
export type Child = { next: () => Promise<IteratorResult<Piece, End>>; return: (v?: unknown) => Promise<unknown> }

/** What the watch needs from the engine; register() builds it from `$`, the tests from fakes. */
export type Host = {
  /** A command's stdout when it exits 0, else null. */
  run: (argv: string[]) => Promise<string | null>
  spawn: (argv: string[], cwd: string) => Child
  sleep: (ms: number) => Promise<void>
  now: () => Promise<number>
  submit: (text: string) => Promise<void>
  log: (text: string, debug?: boolean) => Promise<void>
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
  constructor(private host: Host, private repo: string) {}

  turnStart(): void {
    this.turnRunning = true
  }

  turnIdle(): void {
    this.turnRunning = false
  }

  /** Sends what waited for the turn to end. */
  flush(): Promise<void> {
    return this.deliver()
  }

  /** Kills the child this watch spawned, and nothing else; after /clear or a resume the process still owes its inbox a watcher. */
  end(reason: string): void {
    if (reason === 'clear' || reason === 'resume') return
    this.stopped = true
    const child = this.child
    this.child = null
    if (child) detach(child.return(undefined))
  }

  private async deliver(): Promise<void> {
    if (this.turnRunning) return
    const text = this.ann.take()
    if (!text) return
    try {
      await this.host.submit(text)
    } catch (err) {
      this.ann.unshift(text)
      await this.host.log(`could not start a turn (${err instanceof Error ? err.message : String(err)}); retrying`)
      await this.host.sleep(RETRY_SUBMIT_MS)
      if (!this.stopped) await this.deliver()
    }
  }

  /** The watcher's argv, from watcher_rearm.py, the owner of which inbox and command a session owes. */
  private async plan(): Promise<Plan | string> {
    const config = `${this.repo}/scripts/sutando-config.sh`
    const py = (await this.host.run(['bash', config, 'python-bin']))?.trim()
    if (!py) return 'no Python interpreter resolved'
    const ws = (await this.host.run(['bash', config, 'workspace']))?.trim()
    if (!ws) return 'no workspace resolved'
    const raw = await this.host.run([py, `${this.repo}/src/watcher_rearm.py`, 'watch-argv', '--repo', this.repo, '--workspace', ws])
    const argv = (raw ?? '').split('\n').filter((l) => l.length > 0)
    const at = argv.indexOf('--inbox')
    if (argv.length < 2 || at < 0 || !argv[at + 1]) return 'watcher_rearm.py named no watcher command'
    return { argv, inbox: argv[at + 1], py }
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
          if (lastErr) await this.host.log(`watcher said: ${lastErr}`, true)
          return (r.value ?? { code: null, signal: null }) as End
        }
        if (r.value.stream !== 'stdout') {
          lastErr = r.value.text.trim().split('\n').pop() ?? lastErr
          continue
        }
        const taken = takeLines(buffer + r.value.text)
        buffer = taken.rest
        for (const line of taken.lines) {
          const kind = this.ann.feed(line)
          if (kind === 'task') this.scheduleClose()
          else if (kind === 'other' && line.trim()) await this.host.log(line, !line.startsWith('WATCHER_HELD:'))
        }
        await this.deliver()
      }
    } catch (err) {
      return { code: null, signal: `spawn failed: ${err instanceof Error ? err.message : String(err)}` }
    } finally {
      if (this.child === child) this.child = null
      this.ann.close()
      await this.deliver()
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
    run: async (argv) => {
      try {
        const r = await $.process.run(argv, { timeoutMs: 20_000 })
        return r.exitCode === 0 ? String(r.stdout ?? '') : null
      } catch {
        return null
      }
    },
    spawn: (argv, cwd) => $.process.spawn({ argv, cwd }) as unknown as Child,
    sleep: (ms) => $.clock.sleep(ms),
    now: () => $.clock.now(),
    submit: async (text) => { await $.prompt.submit({ text }) },
    log,
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
    watch?.turnStart()
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // Cleared before next(e): if anything beneath throws, held announcements must not wait forever.
    watch?.turnIdle()
    const done = await next(e)
    await watch?.flush()
    return done
  })

  on('session.end', async ($, e, next) => {
    watch?.end(String(e.reason))
    return next(e)
  })
}
