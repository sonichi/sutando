# Bee channel — user flows for both runtime modes

Owner ask (2026-08-06): for each mode — local side, server side — design the
right user flow: what does the user actually have to do?

The watcher is one artifact (`ag2_sparrow/bee_watcher.py`, console entry point
`sutando-bee-watcher`); the mode is config-selected. The flows below are the
user-facing halves of those two configs. The same connect → credential →
enable → disconnect skeleton is intended to be reused verbatim by every future
integration (Mentra, Even, Plaud) — this doc doubles as that template's first
instance.

## Local mode (privacy-first: Bee credential never leaves the user's machine)

What the user must do **today**:

1. `bee login` (Bee's own CLI; browser sign-in) — starts Bee's authenticated
   local proxy on 127.0.0.1.
2. `pip install ag2-sparrow`.
3. Run `sutando-bee-watcher --bee-proxy-url http://127.0.0.1:<port>` with a
   sink choice (`--bee-sink local` for the fully-OSS file bridge, or `inbox`
   for the shared EventInbox → ambient-taskify path).
4. Done — captures arrive as `access_tier: ambient` tasks; replies go out the
   owner's existing channels.

Known friction (the work list for the target UX):

- (a) The user should not have to find the proxy port — autodetect it.
- (b) Nothing supervises the watcher — it should install under
  launchd/startup so it survives reboots, like the other bridges.
- (c) Three steps should be one.

**Target UX — `sutando bee connect`**: one command that runs/verifies
`bee login`, autodetects the proxy, installs + starts the supervised watcher,
and proves the loop end-to-end with a "say something to your Bee → it shows up
here" test capture. `sutando bee disconnect` uninstalls cleanly.

## Server side (always-on: we custody the user's Bee cloud token)

What the user must do (target — and it is genuinely just this):

1. In their agent DM: "connect my Bee" → the agent replies with where to get
   the Bee token and the exact one-liner to send.
2. `vault set BEE_API_TOKEN <token>` in the DM — the bridge intercepts it
   (never touches chat history or disk), and provisioning places it in the
   cloud secret store scoped to *their* agent.
3. Say "enable" — server side does the rest: the hosted watcher (headless
   `BEE_API_BASE`+`BEE_API_TOKEN` mode, no local proxy) subscribes to Bee
   cloud, events flow to their agent, results land in their "Bee" DM room
   (the backend#444 fallback-room design).

**Revocation must be first-class**: "disconnect Bee" = token deleted + watcher
stopped + confirmation message. That is the custody contract that makes step 2
acceptable.

## Product decisions this surfaces (owner's)

| Decision | Options | Current lean |
| --- | --- | --- |
| Multi-tenancy | One shared watcher deployment holding N users' tokens (cheap, bigger blast radius) vs per-user pod (isolated, more infra) | Per-user pod for the pilot; design config so multi-tenant is a later optimization, not a rewrite |
| Custody consent | Where/how to say "we'll store your Bee token server-side to run this always-on; disconnect anytime" | One plain sentence in the "connect" reply — no legalese |
| Flow skeleton | Bespoke per integration vs one reusable connect → `vault set` → enable → disconnect skeleton | Reusable skeleton; Bee is instance #1, Mentra/Even/Plaud reuse it verbatim |

## The trust story (applies to both modes)

Enabling this never grants the device any authority. Captures are
*observations*: the framework stamps every promoted task
`access_tier: ambient` (sandboxed; the consumer adds the in-band
observation block), so anything privileged still surfaces to the owner for
approval instead of executing. That is the one-sentence answer for
non-technical users too.
