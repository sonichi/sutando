"""The agent profile names its host by the stable label, not the network-dependent hostname.

`socket.gethostname()` on macOS follows the network (a DHCP lease turns
`Marks-MacBook-Pro` into `Marks-MBP.localdomain`), so a host_id taken from it
renames the same Mac as it moves between networks. The profile uses the same
label the rest of Sutando keys its per-host files on, and falls back to the short
hostname only where that label is unavailable (the package running standalone).
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
    os.environ["AGENT_MXID"] = "@host-id-test:ag2.space"
    os.environ["REMOTE_TASK_URL"] = "https://gw.example/relay"
    os.environ["REMOTE_TASK_TOKEN"] = "dummy-secret"
    from ag2_sparrow import remote_gateway_bridge as bridge
    return importlib.reload(bridge)


def test_profile_host_id_is_the_stable_label_not_the_drifting_hostname():
    with tempfile.TemporaryDirectory() as td:
        m = _module(Path(td))
        with mock.patch.object(m, "_stable_host_label", return_value="Marks-MacBook-Pro"), \
                mock.patch("socket.gethostname", return_value="Marks-MBP.localdomain"):
            profile = m._build_agent_profile({})
    assert profile["host"] == {"host_id": "Marks-MacBook-Pro", "kind": "local"}


def test_the_stable_label_does_not_depend_on_src_already_being_importable():
    src = str(PKG.parents[1] / "src")
    with tempfile.TemporaryDirectory() as td:
        m = _module(Path(td))
        path = [p for p in sys.path if os.path.abspath(p) != src]
        with mock.patch.object(sys, "path", path), \
                mock.patch.dict(sys.modules), \
                mock.patch.dict(os.environ, {"SUTANDO_HOST_LABEL": "Stable-Label"}), \
                mock.patch("socket.gethostname", return_value="Drift-MBP.localdomain"):
            sys.modules.pop("util_paths", None)
            profile = m._build_agent_profile({})
    assert profile["host"]["host_id"] == "Stable-Label"


def test_an_unreadable_host_still_builds_a_profile():
    with tempfile.TemporaryDirectory() as td:
        m = _module(Path(td))
        with mock.patch.dict(sys.modules, {"util_paths": None}), \
                mock.patch("socket.gethostname", side_effect=OSError("no host")):
            profile = m._build_agent_profile({})
    assert profile["host"] == {"host_id": "unknown-host", "kind": "local"}


def test_standalone_label_falls_back_to_the_short_hostname():
    from ag2_sparrow import workspace_lock
    with mock.patch.dict(sys.modules, {"util_paths": None}), \
            mock.patch("socket.gethostname", return_value="Marks-MBP.localdomain"):
        assert workspace_lock._host_label() == "Marks-MBP"


if __name__ == "__main__":
    test_profile_host_id_is_the_stable_label_not_the_drifting_hostname()
    test_the_stable_label_does_not_depend_on_src_already_being_importable()
    test_an_unreadable_host_still_builds_a_profile()
    test_standalone_label_falls_back_to_the_short_hostname()
    print("ALL PASS test_agent_profile_host_id")
