"""The agent profile carries `host.placement` only for the exact "desktop" value.

The broker uses it for display, so an unset or unrecognised SUTANDO_PLACEMENT
must leave the key out rather than forward an arbitrary string.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))


def _module(base: Path):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ["AGENT_MXID"] = "@placement-test:ag2.space"
    os.environ["REMOTE_TASK_URL"] = "https://gw.example/relay"
    os.environ["REMOTE_TASK_TOKEN"] = "dummy-secret"
    from ag2_sparrow import remote_gateway_bridge as bridge
    return importlib.reload(bridge)


def _host(placement):
    with tempfile.TemporaryDirectory() as td:
        m = _module(Path(td))
        env = dict(os.environ)
        env.pop("SUTANDO_PLACEMENT", None)
        if placement is not None:
            env["SUTANDO_PLACEMENT"] = placement
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(m, "_profile_host_id", return_value="host-a"):
            return m._build_agent_profile({})["host"]


def test_unset_placement_omits_the_key():
    assert _host(None) == {"host_id": "host-a", "kind": "local"}


def test_desktop_placement_is_sent():
    assert _host("desktop") == {"host_id": "host-a", "kind": "local", "placement": "desktop"}


def test_placement_is_trimmed_and_lowercased():
    assert _host(" Desktop ")["placement"] == "desktop"


def test_any_other_value_is_omitted():
    for value in ("cloud", "", "  ", "desktop-ish", "DESK TOP", "<script>"):
        assert "placement" not in _host(value), value


if __name__ == "__main__":
    test_unset_placement_omits_the_key()
    test_desktop_placement_is_sent()
    test_placement_is_trimmed_and_lowercased()
    test_any_other_value_is_omitted()
    print("ALL PASS test_agent_profile_placement")
