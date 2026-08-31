"""Presence gate: a peer FLEET agent must not set owner-presence, and its task
authority must remain bounded by the broker-attested access tier.

Regression: `_tier_for()` falls through to LOCAL_TIER="owner" for any unlisted
sender on a tierMap-less node, so a peer agent's room post (a) read as owner-tier
AND (b) overwrote `last-owner-activity.json`, poisoning the proactive-loop's
"owner active N min ago" signal (and the core-supervisor escalation target).

The presence gate consults the /v1/agents fleet directory and returns early for
peers. Independently, `_tier_for` feeds the task's access_tier and must not let a
local owner default promote a broker-attested team or guest task. Team retains
bounded workspace-write delegation without receiving unrestricted owner access.
"""
import importlib
import json
import os
import tempfile
import pathlib


OWNER = "@qingyun:ag2.space"          # the human owner — NOT in /v1/agents
PEER = "@qingyun-air.agent:ag2.space"  # a fleet agent — IN /v1/agents


def _load():
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    os.environ["REMOTE_TASK_TIER"] = "owner"   # tierMap-less node → LOCAL_TIER=owner
    # Repo root is parents[1] from tests/; the package lives under packages/.
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages" / "ag2-sparrow"))
    m = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    m = importlib.reload(m)
    # deterministic fleet directory
    m._req = lambda method, path, *a, **k: (
        {"agents": [{"id": PEER}, {"id": "@sutando-bassil:ag2.space"}]}
        if path == "/v1/agents" else {}
    )
    m._fleet_agents_cache["ts"] = 0.0
    m._fleet_agents_cache["ids"] = set()
    return m


def _task(uid):
    return {"user_id": uid, "task": "[AG2Space @x] coordinate on the pin", "source": "ag2space"}


def _tmp_owner_file(m):
    d = tempfile.mkdtemp()
    m.OWNER_ACTIVITY_FILE = pathlib.Path(d) / "last-owner-activity.json"
    return m.OWNER_ACTIVITY_FILE


def test_fleet_agent_ids_fetches_and_caches():
    m = _load()
    ids = m._fleet_agent_ids()
    assert PEER in ids and "@sutando-bassil:ag2.space" in ids
    assert OWNER not in ids
    # cached: a later _req that raises must not disturb the cached set
    m._req = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    assert m._fleet_agent_ids() == ids  # served from cache


def test_fleet_agent_ids_fail_open_empty_on_error_before_first_fetch():
    m = _load()
    m._req = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    m._fleet_agents_cache["ts"] = 0.0
    m._fleet_agents_cache["ids"] = set()
    assert m._fleet_agent_ids() == set()  # never invents ids → never mistakes owner for peer


def test_peer_sender_does_not_write_owner_activity():
    m = _load()
    f = _tmp_owner_file(m)
    # consumer 1 (presence): peer resolves to owner tier, but must NOT record
    m._write_owner_activity(_task(PEER), sender_tier="owner")
    assert not f.exists(), "peer agent poisoned owner-presence"


def test_owner_sender_still_writes_owner_activity():
    m = _load()
    f = _tmp_owner_file(m)
    m._write_owner_activity(_task(OWNER), sender_tier="owner")
    assert f.exists(), "genuine owner activity was swallowed"
    assert json.loads(f.read_text())["channel"] == "ag2space"


def test_peer_task_authority_follows_broker_attestation():
    m = _load()
    # consumer 2 (task authority): local owner is only a cap. The authenticated
    # broker decides whether a peer has useful team access or read-only guest
    # access; only an attested owner remains unrestricted.
    assert m._tier_for(PEER, "team") == "team"
    assert m._tier_for(PEER, "guest") == "guest"
    assert m._tier_for(OWNER, "owner") == "owner"


def test_fail_open_records_peer_when_directory_unknown():
    m = _load()
    f = _tmp_owner_file(m)
    # directory fetch fails and cache empty → fail-open: record (never swallow).
    m._req = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    m._fleet_agents_cache["ts"] = 0.0
    m._fleet_agents_cache["ids"] = set()
    m._write_owner_activity(_task(PEER), sender_tier="owner")
    assert f.exists(), "fail-open must record rather than risk swallowing owner activity"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:
            fails += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{'PASS' if not fails else 'FAILED'} ({len(fns)-fails}/{len(fns)})")
    sys.exit(1 if fails else 0)
