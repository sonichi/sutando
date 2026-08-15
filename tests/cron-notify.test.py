#!/usr/bin/env python3
"""Unit tests for skills/schedule-crons/cron-notify.py — the pure decision/format
half of the cron-room → owner-active-channel ping (Track 13a) plus the CLI's
non-network paths (dry-run, suppression exits). Network delivery (_post_to_room)
is deliberately NOT exercised here — it rides the same gateway op:message path
every cron uses and is covered by live use."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "cron_notify", REPO / "skills" / "schedule-crons" / "cron-notify.py")
cn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cn)

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {extra}")
        FAILURES.append(name)


def main() -> int:
    # ── is_attention_worthy ──────────────────────────────────────────────
    check("routine is silent", not cn.is_attention_worthy("routine", "big news"))
    check("unknown kind is silent", not cn.is_attention_worthy("banana", "big news"))
    check("non-str kind is silent", not cn.is_attention_worthy(None, "x"))
    check("owner_action pings", cn.is_attention_worthy("owner_action", "need a decision"))
    check("digest with news pings", cn.is_attention_worthy("digest", "3 PRs merged"))
    check("digest with no news is downgraded",
          not cn.is_attention_worthy("digest", "Nothing new this pass"))
    check("error always pings — even a 'nothing new' error",
          cn.is_attention_worthy("error", "nothing new but the cron crashed"))
    check("empty summary digest still pings (no empty-signal match)",
          cn.is_attention_worthy("digest", ""))
    # Mixed summary: real news + an INCIDENTAL empty-signal clause must still
    # ping — the old `sig in s` substring gate silently swallowed these (the
    # owner_action case is the one that must never be swallowed).
    check("mixed digest: real news + incidental empty clause still pings",
          cn.is_attention_worthy("digest", "3 PRs merged; nothing new on the Slack storm"))
    check("mixed owner_action: real ask + 'no changes' aside still pings",
          cn.is_attention_worthy("owner_action", "approve #2446; no changes needed elsewhere"))
    check("whole-summary empty digest is still downgraded",
          not cn.is_attention_worthy("digest", "nothing new"))

    # ── deep_link ────────────────────────────────────────────────────────
    check("room-only link",
          cn.deep_link("!r:ag2.space") == "https://matrix.to/#/!r:ag2.space?via=ag2.space")
    check("room+event link",
          cn.deep_link("!r:ag2.space", "$e") == "https://matrix.to/#/!r:ag2.space/$e?via=ag2.space")
    check("empty via drops query",
          cn.deep_link("!r:ag2.space", via="") == "https://matrix.to/#/!r:ag2.space")

    # ── format_ping ──────────────────────────────────────────────────────
    p = cn.format_ping("pr-shepherd", "  two\n lines\t here ", "!r:ag2.space", "$e")
    check("whitespace collapsed", "two lines here" in p, p)
    check("ping carries cron name + link",
          p.startswith("⏰ pr-shepherd: ") and "matrix.to/#/!r:ag2.space/$e" in p, p)
    long = cn.format_ping("c", "x" * 300, "!r:ag2.space")
    head = long.split(" → ")[0]
    check("long summary truncated with ellipsis",
          head.endswith("…") and len(head) < 160, f"len={len(head)}")

    # ── should_ping_now / record_ping ────────────────────────────────────
    check("never-pinged cron passes", cn.should_ping_now({}, "c", now=10_000))
    check("recent ping is rate-limited",
          not cn.should_ping_now({"c": 9_000}, "c", now=10_000, min_interval_s=1800))
    check("old ping passes",
          cn.should_ping_now({"c": 1_000}, "c", now=10_000, min_interval_s=1800))
    check("exactly-at-interval passes",
          cn.should_ping_now({"c": 8_200}, "c", now=10_000, min_interval_s=1800))
    check("non-numeric stored value fails open", cn.should_ping_now({"c": "bogus"}, "c", 10_000))
    check("negative stored value fails open", cn.should_ping_now({"c": -5}, "c", 10_000))
    check("non-dict state fails open", cn.should_ping_now(None, "c", 10_000))
    st = cn.record_ping({}, "c", 123.9)
    check("record_ping stamps int", st == {"c": 123})
    check("record_ping tolerates non-dict", cn.record_ping(None, "c", 5) == {"c": 5})

    # ── gateway config + delivery (urllib mocked — no network) ──────────
    # _load_gateway now delegates to the canonical resolver (ensure-cron-room.py:
    # resolve_token): it reads process env AND <repo>/.env across all gateway
    # alias keys, honoring combined "url|secret" and split URL+token. It takes a
    # REPO DIR (reads <repo>/.env), repo-anchored regardless of cwd — not a .env
    # file path. Tests clear the process-env gateway keys so the running core's
    # real creds can't leak in and make a negative meaningless.
    import unittest.mock as um
    import os as _os
    _GW_KEYS = ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN",
                "GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL", "AG2_REMOTE_URL")

    def _clean_env(**over):
        e = {k: v for k, v in _os.environ.items() if k not in _GW_KEYS}
        e.update(over)
        return um.patch.dict(_os.environ, e, clear=True)

    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text("OTHER=1\nAG2_REMOTE_TOKEN='https://x.example/relay|sekret'\n")
        with _clean_env():
            base, secret = cn._load_gateway(d)
            check("_load_gateway parses combined url|secret from <repo>/.env",
                  base == "https://x.example/relay" and secret == "sekret", (base, secret))
        # split token via process env WINS over .env (the exact #2346 gap: the old
        # reader saw only AG2_REMOTE_TOKEN in .env and returned (None, None) here)
        with _clean_env(GATEWAY_URL="https://env.example", GATEWAY_TOKEN="s2"):
            check("_load_gateway split process-env wins over .env",
                  cn._load_gateway(d) == ("https://env.example", "s2"), cn._load_gateway(d))
        # repo-anchored: resolves the same from a non-repo cwd
        with _clean_env():
            _cwd0 = _os.getcwd()
            try:
                _os.chdir(tempfile.gettempdir())
                check("_load_gateway resolves <repo>/.env from a non-repo cwd",
                      cn._load_gateway(d) == ("https://x.example/relay", "sekret"),
                      cn._load_gateway(d))
            finally:
                _os.chdir(_cwd0)
        # bare token, no URL anywhere → no base, so nothing can be sent
        env.write_text("AG2_REMOTE_TOKEN=nopipe\n")
        with _clean_env():
            b2, _s2 = cn._load_gateway(d)
            check("_load_gateway bare token → no base (won't send)", b2 is None, (b2, _s2))
        # nothing configured anywhere → (None, None)
        with tempfile.TemporaryDirectory() as _empty, _clean_env():
            check("_load_gateway unconfigured → (None, None)",
                  cn._load_gateway(_empty) == (None, None))

        env.write_text("AG2_REMOTE_TOKEN=https://x.example/relay|sekret\n")
        resp = um.MagicMock()
        resp.read.return_value = json.dumps({"event_id": "$evt"}).encode()
        with um.patch.object(cn, "_load_gateway", return_value=("https://x.example/relay", "sekret")), \
             um.patch("urllib.request.urlopen", return_value=resp) as uo:
            eid = cn._post_to_room("!r:x", "hello", str(env))
            check("_post_to_room returns event_id on 200", eid == "$evt", eid)
            req = uo.call_args[0][0]
            check("_post_to_room targets <base>/v1/room",
                  req.full_url == "https://x.example/relay/v1/room", req.full_url)
        # The gateway can answer {"ok": true} with no event_id; the return must
        # still be truthy or the caller re-sends.
        resp_ok = um.MagicMock()
        resp_ok.read.return_value = json.dumps({"ok": True}).encode()
        with um.patch.object(cn, "_load_gateway", return_value=("https://x.example/relay", "sekret")), \
             um.patch("urllib.request.urlopen", return_value=resp_ok):
            r_ok = cn._post_to_room("!r:x", "hello", str(env))
            check("_post_to_room {\"ok\":true} → truthy (live gateway shape)", bool(r_ok), r_ok)
        resp_bare = um.MagicMock()
        resp_bare.read.return_value = b""
        with um.patch.object(cn, "_load_gateway", return_value=("https://x.example/relay", "sekret")), \
             um.patch("urllib.request.urlopen", return_value=resp_bare):
            r_bare = cn._post_to_room("!r:x", "hello", str(env))
            check("_post_to_room empty 2xx body → truthy (a 2xx IS delivery)", bool(r_bare), r_bare)
        import urllib.error as ue
        with um.patch.object(cn, "_load_gateway", return_value=("https://x.example/relay", "s")), \
             um.patch("urllib.request.urlopen", side_effect=ue.URLError("down")):
            check("_post_to_room URLError → None", cn._post_to_room("!r:x", "hi", str(env)) is None)
        with um.patch.object(cn, "_load_gateway", return_value=(None, None)):
            check("_post_to_room no gateway → None", cn._post_to_room("!r:x", "hi") is None)

    check("_load_state missing file → {}", cn._load_state("/nonexistent/state.json") == {})

    # ── CLI post paths (delivery mocked) ─────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        sf = Path(d) / "state.json"
        with um.patch.object(cn, "_post_to_room", return_value="$evt"):
            rc = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                          "--room", "!r:x", "--state-file", str(sf), "--now", "50000"])
            check("CLI: successful post exit 0", rc == 0)
            check("CLI: post stamps rate-limit state",
                  json.loads(sf.read_text()) == {"c": 50000}, sf.read_text())
        with um.patch.object(cn, "_post_to_room", return_value=None):
            # `--state-file` is NOT optional here. Without it this call falls
            # through to `_default_state_file()` and stamps the OPERATOR'S
            # workspace — the enclosing TemporaryDirectory isolates `sf`, not
            # the default path, and the sibling call above only looks isolated
            # because it passes the flag.
            rc = cn.main(["--cron", "c2", "--summary", "news", "--kind", "error",
                          "--room", "!r:x", "--state-file", str(sf)])
            check("CLI: failed post exit 2", rc == 2)

    # ── CLI non-network paths ────────────────────────────────────────────
    rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "routine", "--room", "!r:x"])
    check("CLI: routine kind suppressed exit 3", rc == 3)

    with tempfile.TemporaryDirectory() as d:
        sf = Path(d) / "state.json"
        sf.write_text(json.dumps({"c": 9_000}))
        rc = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                      "--room", "!r:x", "--state-file", str(sf), "--now", "10000"])
        check("CLI: rate-limited exit 3", rc == 3)
        rc = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                      "--room", "!r:x", "--state-file", str(sf), "--now", "20000",
                      "--dry-run"])
        check("CLI: dry-run posts nothing, exit 0", rc == 0)
        check("CLI: dry-run does not stamp state",
              json.loads(sf.read_text()) == {"c": 9_000})

    # ── #2346 review blocker: the DEFAULT invocation (no --state-file) must be
    # rate-limited too. Previously an empty default meant every process started
    # from {} and the cooldown never applied, so two process-equivalent default
    # calls both posted. Now the default resolves to a canonical workspace path;
    # here we point that at a temp file and prove the 2nd call is suppressed.
    with tempfile.TemporaryDirectory() as d:
        default_sf = Path(d) / "cron-notify-cooldown.json"
        posts = []
        with um.patch.object(cn, "_default_state_file", return_value=str(default_sf)), \
             um.patch.object(cn, "_post_to_room",
                             side_effect=lambda *a, **k: posts.append(a) or "$evt"):
            rc1 = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                           "--room", "!r:x", "--now", "10000"])
            rc2 = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                           "--room", "!r:x", "--now", "10001"])
        check("CLI default (no --state-file): 1st posts exit 0", rc1 == 0, rc1)
        check("CLI default (no --state-file): 2nd rate-limited exit 3", rc2 == 3, rc2)
        check("CLI default: exactly one post across two calls", len(posts) == 1, len(posts))
        check("CLI default: cooldown persisted to the workspace path",
              json.loads(default_sf.read_text()) == {"c": 10000}, default_sf.read_text())

    # ── review blocker 1: deep-link room is a DIFFERENT identity from the
    # delivery room. Linking a cron-room event under the destination room
    # produces a matrix.to URL that cannot resolve the event.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"
        posted = []
        cn._post_to_room = lambda room, body, repo=None: (
            posted.append((room, body)) or "$evt"
        )
        rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                      "--room", "!dest:x", "--link-room", "!cronroom:x",
                      "--event-id", "$e1", "--state-file", str(sf), "--now", "1"])
        check("link-room: exit 0", rc == 0)
        dest, body = posted[-1]
        check("link-room: DELIVERED to --room", dest == "!dest:x")
        check("link-room: LINK points at --link-room, not --room",
              "!cronroom:x/$e1" in body and "!dest:x/$e1" not in body)

        # default preserves room-local delivery
        posted.clear()
        sf.write_text("{}")
        cn.main(["--cron", "d", "--summary", "s", "--kind", "digest",
                 "--room", "!only:x", "--event-id", "$e2",
                 "--state-file", str(sf), "--now", "1"])
        check("link-room: defaults to --room when omitted",
              "!only:x/$e2" in posted[-1][1])

    # ── review blocker 2: cooldown must be RESERVED before delivery. Posting
    # first and persisting after means an unwritable state path reports success
    # with no cooldown recorded — the next fire duplicates the notification.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "sub" / "s.json"          # parent does not exist
        posted = []
        cn._post_to_room = lambda room, body, env_path=".env": (
            posted.append(room) or "$evt"
        )
        rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                      "--room", "!r:x", "--state-file", str(sf), "--now", "1"])
        check("unpersistable state: does NOT post", posted == [])
        check("unpersistable state: exits non-zero", rc == 2)

    # rollback: a failed send must release the reservation, or one transient
    # error mutes the cron for the whole cooldown window.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"
        sf.write_text("{}")
        cn._post_to_room = lambda room, body, env_path=".env": None   # send fails
        rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                      "--room", "!r:x", "--state-file", str(sf), "--now", "1"])
        check("failed send: exit 2", rc == 2)
        check("failed send: reservation rolled back (not muted)",
              json.loads(sf.read_text()).get("c") is None)

    # CONTROL: a SUCCESSFUL post must still record the cooldown.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"
        sf.write_text("{}")
        cn._post_to_room = lambda room, body, env_path=".env": "$evt"
        cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                 "--room", "!r:x", "--state-file", str(sf), "--now", "4242"])
        check("CONTROL: successful post records cooldown",
              json.loads(sf.read_text()).get("c") == 4242)

    # ── #2346 review (FAIL-CLOSED): if the exclusive lock can't be acquired but
    # the state file IS writable, we must REFUSE to post. Degrading to unlocked
    # (the old best-effort) restores the duplicate-notification race — john's
    # exact-head repro was a *directory* sitting at the `.lock` sidecar path,
    # which makes os.open fail while save_state_atomic would still succeed.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"          # parent writable → the save WOULD succeed
        _os.mkdir(str(sf) + ".lock")      # a directory at the sidecar path → os.open fails
        posted = []
        with um.patch.object(cn, "_post_to_room",
                             side_effect=lambda *a, **k: posted.append(a) or "$evt"):
            rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                          "--room", "!r:x", "--state-file", str(sf), "--now", "10000"])
        check("unlockable sidecar + writable state: REFUSES to post (fail-closed)",
              posted == [], posted)
        check("unlockable sidecar: exits non-zero", rc == 2, rc)
        check("unlockable sidecar: no reservation written without exclusivity",
              (not Path(sf).exists()) or json.loads(sf.read_text()) == {},
              sf.read_text() if Path(sf).exists() else "(no file)")

    # ── #2346 review: the MANAGED default path must create its own state/ parent
    # so a clean install (no state/ yet) can lock+post — otherwise fail-closed
    # refuses every default ping. Explicit --state-file parents stay fail-closed.
    import sys as _sys
    _srcp = str(REPO / "src")
    if _srcp not in _sys.path:
        _sys.path.insert(0, _srcp)
    import workspace_default as _wd
    with tempfile.TemporaryDirectory() as wsdir:
        with um.patch.object(_wd, "resolve_workspace", lambda: Path(wsdir)):
            statedir = Path(wsdir) / "state"
            check("default path: state/ absent before resolve", not statedir.exists())
            dp = cn._default_state_file()
            check("default path: creates state/ parent", statedir.is_dir(), dp)
            check("default path: resolves under <workspace>/state/",
                  dp == str(statedir / "cron-notify-cooldown.json"), dp)
        # end-to-end: a fresh workspace (state/ removed) still posts on the default
        import shutil as _shutil
        _shutil.rmtree(str(Path(wsdir) / "state"), ignore_errors=True)
        with um.patch.object(_wd, "resolve_workspace", lambda: Path(wsdir)), \
             um.patch.object(cn, "_post_to_room", return_value="$evt"):
            rc = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                          "--room", "!r:x", "--now", "10000"])
        check("default path: clean install (no state/) still posts, exit 0", rc == 0, rc)

    # ── #2346 review: a flock() failure AFTER os.open must fail closed AND close
    # the just-opened fd (no leak). john's repro forces the REAL _StateLock's
    # fcntl.flock to raise, then probes the captured descriptor.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"; sf.write_text("{}")
        posted = []
        captured = {}
        _real_open = _os.open

        def _capturing_open(*a, **k):
            fd = _real_open(*a, **k)
            captured["fd"] = fd
            return fd

        def _boom_flock(fd, op):
            raise OSError("simulated flock acquisition failure")

        with um.patch("os.open", _capturing_open), \
             um.patch("fcntl.flock", _boom_flock), \
             um.patch.object(cn, "_post_to_room",
                             side_effect=lambda *a, **k: posted.append(a) or "$evt"):
            rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                          "--room", "!r:x", "--state-file", str(sf), "--now", "10000"])
        check("flock failure: REFUSES to post (fail-closed)", posted == [], posted)
        check("flock failure: exit 2", rc == 2, rc)
        check("flock failure: no reservation written", json.loads(sf.read_text()) == {},
              sf.read_text())

        def _fd_open(fd):
            try:
                _os.fstat(fd)
                return True
            except OSError:
                return False
        check("flock failure: the opened fd was CLOSED (no leak)",
              "fd" in captured and not _fd_open(captured["fd"]), captured)

    # dry-run must also honor the rate-limit (covers the dry-run suppressed path).
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"; sf.write_text(json.dumps({"c": 9000}))
        rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                      "--room", "!r:x", "--state-file", str(sf), "--now", "10000",
                      "--dry-run"])
        check("dry-run + rate-limited: suppressed exit 3", rc == 3, rc)

    # save fails while the lock IS acquirable (state path is a directory, so
    # os.replace fails but the sidecar `.lock` opens fine) → refuse to post.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"; _os.mkdir(str(sf))   # state path is a directory
        posted = []
        with um.patch.object(cn, "_post_to_room",
                             side_effect=lambda *a, **k: posted.append(a) or "$evt"):
            rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                          "--room", "!r:x", "--state-file", str(sf), "--now", "10000"])
        check("save-fails (lock ok): REFUSES to post", posted == [], posted)
        check("save-fails (lock ok): exit 2", rc == 2, rc)

    # rollback tolerates a lock-acquisition failure (best-effort): reserve
    # succeeds, the send fails, and the rollback lock can't be acquired → the
    # reservation is KEPT (suppresses the next fire, never duplicates).
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"; sf.write_text("{}")
        enters = {"n": 0}

        class _LockOkThenFail:
            def __init__(self, path):
                pass

            def __enter__(self):
                enters["n"] += 1
                if enters["n"] >= 2:            # the rollback acquisition
                    raise OSError("simulated rollback lock unavailable")
                return self

            def __exit__(self, *a):
                return False

        with um.patch.object(cn, "_StateLock", _LockOkThenFail), \
             um.patch.object(cn, "_post_to_room", return_value=None):   # send fails
            rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                          "--room", "!r:x", "--state-file", str(sf), "--now", "7777"])
        check("rollback lock failure: exit 2", rc == 2, rc)
        check("rollback lock failure: reservation kept (best-effort)",
              json.loads(sf.read_text()).get("c") == 7777, sf.read_text())

    # failed send with a PRIOR stamp → rollback RESTORES the prior value (not
    # pop): the cron had pinged before, long enough ago to re-ping now.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"; sf.write_text(json.dumps({"c": 1000}))
        with um.patch.object(cn, "_post_to_room", return_value=None):   # send fails
            rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "digest",
                          "--room", "!r:x", "--state-file", str(sf), "--now", "10000"])
        check("failed send with prior: exit 2", rc == 2, rc)
        check("failed send with prior: rollback RESTORES prior (not pop)",
              json.loads(sf.read_text()).get("c") == 1000, sf.read_text())

    # ── #2346 review blocker (CONCURRENCY): load→check→reserve must be ONE
    # cross-process-exclusive transaction. john forced two same-cron processes to
    # both finish _load_state() before either reserved → both posted, defeating
    # the per-cron noise bound; and two DIFFERENT crons can clobber each other's
    # stamp in the shared file's read-modify-write. These run REAL threads
    # contending on the flock — flock is per-open-file-description, so each
    # thread's own os.open+flock serializes even in one process (CPython releases
    # the GIL around the blocking flock, so no deadlock). With the lock the
    # outcome is order-INDEPENDENT, so the assertions are deterministic; remove
    # the lock and these flap/fail. A barrier releases all workers together to
    # maximize contention.
    import threading

    # (a) same-cron overlap → EXACTLY ONE post, the rest rate-limited.
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"
        N = 8
        posts = []
        posts_lock = threading.Lock()

        def _counting_post(*a, **k):
            with posts_lock:
                posts.append(a)
            return "$evt"

        barrier = threading.Barrier(N)
        rcs = [None] * N

        def _worker(i):
            barrier.wait()  # all workers enter main() together
            rcs[i] = cn.main(["--cron", "overlap", "--summary", "news",
                              "--kind", "digest", "--room", "!r:x",
                              "--state-file", str(sf), "--min-interval", "1800",
                              "--now", "10000"])

        with um.patch.object(cn, "_post_to_room", side_effect=_counting_post):
            ts = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

        check("concurrent same-cron: exactly ONE post across N overlapping fires",
              len(posts) == 1, f"posts={len(posts)}")
        check("concurrent same-cron: one exit-0, the rest exit-3",
              rcs.count(0) == 1 and rcs.count(3) == N - 1, rcs)
        check("concurrent same-cron: state holds the single reservation",
              json.loads(sf.read_text()) == {"overlap": 10000}, sf.read_text())

    # (b) distinct crons → NO lost update: every reservation survives the shared
    # read-modify-write (without the lock, one save can clobber another's stamp).
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "s.json"
        crons = [f"cron{i}" for i in range(6)]
        postsD = []
        postsD_lock = threading.Lock()

        def _counting_postD(*a, **k):
            with postsD_lock:
                postsD.append(a)
            return "$evt"

        barrierD = threading.Barrier(len(crons))
        rcsD = {}

        def _workerD(name):
            barrierD.wait()
            rcsD[name] = cn.main(["--cron", name, "--summary", "news",
                                  "--kind", "digest", "--room", "!r:x",
                                  "--state-file", str(sf), "--now", "20000"])

        with um.patch.object(cn, "_post_to_room", side_effect=_counting_postD):
            ts = [threading.Thread(target=_workerD, args=(c,)) for c in crons]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

        check("concurrent distinct crons: all posted (none rate-limited)",
              all(rc == 0 for rc in rcsD.values()) and len(postsD) == len(crons),
              (rcsD, len(postsD)))
        check("concurrent distinct crons: NO clobber — every reservation survives",
              json.loads(sf.read_text()) == {c: 20000 for c in crons},
              sf.read_text())

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
