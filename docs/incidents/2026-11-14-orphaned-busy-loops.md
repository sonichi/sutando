# Incident: orphaned CPU busy-loops starve the credential proxy and stall the core (2026-11-14)

**Status:** core reachable again; task delivery still recovering when this was written (07:56 PST)
**Duration of full delivery outage:** 06:59 PST (last delivered result) → not yet resumed at 07:56 PST
**Host:** one 14-core macOS host running the live core, ~10 pool workers and the credential proxy

## Summary

At 07:02 PST a pool worker started 30 `node -e "for(;;){}"` processes on
purpose, to reproduce a timing-sensitive test failure under CPU load. The
command was meant to kill them afterwards with `pkill -f "for(;;){}"`. That
pattern is an extended regular expression, not a literal string, so it matched
nothing. The worker's own check (`pgrep -f "for(;;){}" | wc -l`) used the same
pattern, printed `0`, and reported the cleanup as successful.

The 30 loops became orphans (parent pid 1) and kept running. Load average
went from ~80–95 to 290–340 on a host that was already overloaded. The
credential proxy on `localhost:7846` could no longer read the OAuth token
from the Keychain or hold upstream connections. The core's API calls failed,
first with `502 Bad Gateway`, then `ECONNRESET`/`EPIPE`, then
`ECONNREFUSED` once its retries ran out. An attempt to restart the proxy
stalled because the launchd wrapper's `lsof` port check hung under the load.

The failure fed itself. The loops caused the proxy outage, and the outage
also stopped the worker that spawned the loops from ever cleaning them up.

## Impact

- **No results delivered after 06:59:37 PST.** Six owner and s.space tasks
  queued without replies. Before the incident the core was delivering 8–14
  results an hour overnight.
- **The live core** (same session, so conversation history is intact) could
  not reach the API from ~07:30 until 07:47. After that it was very slow:
  it had to reread a multi-day session after an earlier `/login` to a
  different organization, and the host was still recovering from the load.
- **Pool workers** sharing the proxy failed with the same errors. That
  includes the worker that started the loops.
- **The whole host** became unusable for other work (load > 20× core count).
- No data was lost and no credentials were exposed.

## Timeline (PST, 2026-11-14)

| Time | Event |
| --- | --- |
| 00:02 | Core account hits its weekly limit. Someone runs `/login` in the core to a different organization. Claude Code warns "can't use thinking from another organization." |
| 00:20–01:03 | Separate overload episode (Chrome, iOS Simulator, Docker VM, load peaking ~538). The proxy exits on startup with "No OAuth token found in macOS keychain" until its 7th launchd run listens at 01:03:31. |
| 01:05–06:59 | Degraded but working: 8–14 results/hour. The proxy logs a steady stream of Keychain-read failures (pass-through engaged) and `ECONNRESET` all night. Load ~80–95. |
| 06:59:37 | Last result delivered. |
| 07:02:11 | Pool worker starts `for i in $(seq 1 30); do node -e "for(;;){}" & done`, runs its test 6×, then `pkill -f "for(;;){}"`. The pkill matches nothing; the follow-up `pgrep … \| wc -l` prints `0`. |
| ~07:30 | Load 290–340. Core shows `502 Bad Gateway · Retrying`; proxy logs `read ECONNRESET` / `write EPIPE`. |
| 07:35 | Operator runs `launchctl kickstart -k` on the proxy. The wrapper hangs in `lsof -ti :7846`; nothing listens on 7846. |
| 07:37 | Core exhausts retries: `ECONNREFUSED · attempt 10/10`. |
| 07:41 | 30 orphaned `node -e for(;;){}` processes (ppid 1, started 07:02:11) identified as the dominant load. They are traced to the worker's transcript. |
| 07:44 | Owner kills the loops with an escaped pattern: `pkill -f 'node -e for\(;;\)\{\}'`. |
| 07:45–07:47 | Proxy kickstarted again; the wrapper's two `lsof` calls hang again and are killed; proxy listens at 07:47:41 and loads the token from the Keychain. |
| 07:48 | Core API calls succeed (`unified-status: allowed`, 23% of 7-day limit); its task watcher re-arms. |
| 07:56 | Load 104 and falling. Core is working but slow; no result delivered yet. |

## Root cause

**An agent's cleanup step failed silently, and its own check couldn't catch
the failure.**

`pkill -f` and `pgrep -f` treat their argument as an extended regular
expression. In `for(;;){}`, `(;;)` is a group matching `;;` and `{}` is not a
literal brace pair. So the pattern never matches the command line
`node -e for(;;){}`. Reproduced on the incident host with a harmless process
whose argv contains that text:

```console
$ python3 -c 'import time; time.sleep(60)' 'for(;;){}' &
$ pgrep -f 'for(;;){}'        | grep -c "^$!$"   # unescaped, as the worker ran it
0
$ pgrep -f 'for\(;;\)\{\}'    | grep -c "^$!$"   # escaped
1
```

The worker checked its cleanup with the same broken pattern, so the check
returned the same false "0 left" as the cleanup itself. The loops had no
time bound and ran at normal priority, so nothing else would ever have
stopped them.

## Contributing factors

1. **The host was already overloaded** before the loops started: Chrome
   (~117 processes), the iOS Simulator (~135), a Docker VM, ~30 postgres
   processes, Dropbox and Screen Studio, plus Sutando's own services. That
   left no headroom.
2. **The credential proxy is fragile under load:**
   - The launchd job runs with `ProcessType = Background`, so macOS throttles
     it first when the CPU is contended.
   - The Keychain read (`security find-generic-password`) uses a 5 s
     `execFileSync` timeout. Under load it times out, and the proxy falls back
     to passing the client credential through ("no keychain credential —
     passing client credential through"). At startup, the same timeout makes
     the proxy exit with "No OAuth token found."
   - The launchd wrapper's `kill_stale_holder` runs `lsof -ti :7846` with no
     timeout. Under load `lsof` hangs for minutes, so a restart cannot finish
     without someone stepping in.
3. **Every alerting path depends on the API it's alerting about.** The core
   and workers report problems by running agent turns, which need the proxy.
   No out-of-band check notified the owner that load was > 20× core count or
   that the proxy was down.
4. **The core session is large and was switched to another organization
   mid-session.** That slows recovery: once the API returns, the core first has
   to reread the session without its cached thinking.

## Resolution

1. Found the load source: `ps` showed 30 `node -e for(;;){}` processes with
   ppid 1, all started 07:02:11. Searching the agent transcripts for the
   command string led to the worker session that ran it.
2. The owner killed them with an escaped pattern:
   `pkill -f 'node -e for\(;;\)\{\}'`. (The operator session's own attempt
   was blocked by its permission policy, so the owner had to run it.)
3. Restarted the proxy with `launchctl kickstart -k
   gui/$(id -u)/com.sutando.credential-proxy` and killed the hung `lsof`
   calls so the wrapper could go on to start the proxy.
4. Confirmed the proxy was listening and had loaded the Keychain token, and
   that the core's next request succeeded and its task watcher re-armed. The
   core session was **not** restarted, so its conversation history is intact.

## How to avoid a repeat

Ordered by how directly each one would have prevented this incident.
Everything below is a **proposal**; none of it is implemented in the PR that
adds this report.

### 1. Agents clean up by PID, never by pattern

Any agent (core, worker or guest session) that starts background processes
must record their PIDs and kill exactly those:

```bash
pids=()
for i in $(seq 1 8); do node -e 'for(;;){}' & pids+=($!); done
trap 'kill "${pids[@]}" 2>/dev/null' EXIT      # runs even if the test step fails
# … run the test …
kill "${pids[@]}"; wait "${pids[@]}" 2>/dev/null
for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && echo "LEAK: $p"; done
```

If a pattern is unavoidable, use `pgrep -f -x` with a fully escaped regex,
and check the result by PID, not with the same pattern.

### 2. Load generators must stop on their own

Give every artificial load a time limit, so a failed cleanup still ends:

```bash
node -e 'const end = Date.now() + 120e3; while (Date.now() < end) {}' &
# or: timeout 120 node -e 'for(;;){}' &
```

Also run them under `nice -n 19` and cap their number at the core count. This
incident used 30 unbounded loops at normal priority on 14 cores.

### 3. Put the rules where workers read them

Add rules 1 and 2 to the pool worker's standing instructions, and to
`REVIEW.md` for scripts that spawn background processes. Agent sessions
reach for `pkill -f "<literal command>"` by habit, and that habit fails
silently whenever the command contains a regex metacharacter
(`( ) { } [ ] . * + ? |`).

### 4. Make the credential proxy survive load

- Drop `ProcessType = Background` from
  `src/launchd/com.sutando.credential-proxy.plist`. The proxy sits on the
  critical path of every agent request, so the OS should not deprioritise it.
- Bound the wrapper's port check (`timeout 5 lsof -nP -ti tcp:7846`). If the
  check times out, go ahead and start; the bind will report a real conflict.
- Retry the startup Keychain read a few times with backoff instead of
  exiting on the first 5 s timeout.

### 5. Add an out-of-band alarm

Add a check to the existing health check that runs without the model API.
It should send a macOS notification and write a pending-question entry when:

- 5-minute load / core count stays above ~4 for 10 minutes. The notification
  names the top five CPU consumers and any high-CPU processes with ppid 1
  that were started from an agent scratchpad or worktree; or
- `localhost:7846` refuses connections for more than 2 minutes.

### 6. Keep the core session recoverable

Avoid `/login` to a different organization inside the live core. Rotate the
core into a fresh session before it reaches multi-day size, so recovering
after an outage doesn't mean rereading the whole history.

## Runbook: "core shows 502 / ECONNREFUSED and load is very high"

1. `uptime` and `sysctl -n hw.ncpu`. If load is far above core count, find
   the cause before restarting anything; restarts stall under this load.
2. List runnable processes by command:
   `ps -Ao state,comm | awk '$1 ~ /^R/ {print $2}' | sort | uniq -c | sort -rn | head`.
   Check for orphans (`ppid 1`) with an agent-shaped command line.
3. Trace an orphan to its source by searching agent transcripts
   (`<workspace>/.claude-sutando/projects/*/*.jsonl`) for its command string.
4. Kill it by PID, or with a fully escaped pattern.
5. `launchctl kickstart -k gui/$(id -u)/com.sutando.credential-proxy`. If
   7846 isn't listening within a minute, look for a hung `lsof -ti :7846`
   under `credential-proxy-wrapper.sh` and kill it.
6. Check the proxy log for "OAuth token loaded from keychain," then watch the
   core pane for a successful turn and a re-armed task watcher. Restart the
   core itself only if it does not recover, and only from a terminal outside
   any Sutando tmux session.
