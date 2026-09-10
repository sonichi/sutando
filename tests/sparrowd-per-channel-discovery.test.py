#!/usr/bin/env python3
"""sparrowd supervises one gateway bridge per RELAY channel.

Before this, `worker_specs()` returned a single hardcoded bridge, so a channel
could hold a valid token and still receive nothing — measured on this host,
`local-ag2space` had a resolvable env and no process. The two rules that make
the discovery safe rather than merely automatic:

  * only RELAY channels — selection is delegated to `channel_env_resolve`,
    which picks by a usable REMOTE_TASK_TOKEN, so discord/slack/telegram are
    excluded without sparrowd naming them;
  * the instance name is the one ALREADY IN USE (`dev`, `local`), not the
    directory name — renaming a lane moves its `task-<inst>~...` namespace and
    its status file, orphaning work in flight.

Run: python3 tests/sparrowd-per-channel-discovery.test.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import sparrowd  # noqa: E402


def _channel(base: Path, name: str, *, token="tok-0123456789abcdef", fname=".env",
             var="REMOTE_TASK_TOKEN"):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(
        f"REMOTE_TASK_URL=https://gw.invalid/relay\n{var}={token}\n", encoding="utf-8")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "channels"
        self.base.mkdir(parents=True)


class TestSelection(Base):
    def test_a_relay_channel_is_discovered(self):
        _channel(self.base, "ag2space")
        self.assertEqual(sparrowd.relay_channels(self.base), ["ag2space"])

    def test_a_non_relay_channel_is_excluded(self):
        """discord/slack hold a different token — never a gateway bridge."""
        _channel(self.base, "discord", var="DISCORD_BOT_TOKEN")
        self.assertEqual(sparrowd.relay_channels(self.base), [])

    def test_an_empty_token_does_not_qualify(self):
        _channel(self.base, "ag2space", token="")
        self.assertEqual(sparrowd.relay_channels(self.base), [])

    def test_a_channel_dir_with_no_env_is_skipped(self):
        (self.base / "teams").mkdir()
        self.assertEqual(sparrowd.relay_channels(self.base), [])

    def test_primary_sorts_first(self):
        for n in ("local-ag2space", "dev-ag2space", "ag2space"):
            _channel(self.base, n)
        self.assertEqual(sparrowd.relay_channels(self.base)[0], "ag2space")

    def test_absent_channels_dir_is_not_an_error(self):
        self.assertEqual(sparrowd.relay_channels(self.base / "nope"), [])


class TestInstanceNaming(Base):
    def test_primary_stays_unsuffixed(self):
        """Naming prod's lane would move task-<inst>~... and its status file."""
        self.assertEqual(sparrowd.instance_for("ag2space"), "")

    def test_uses_the_name_already_in_use_not_the_dir(self):
        self.assertEqual(sparrowd.instance_for("dev-ag2space"), "dev")
        self.assertEqual(sparrowd.instance_for("local-ag2space"), "local")

    def test_a_channel_without_the_suffix_keeps_its_name(self):
        self.assertEqual(sparrowd.instance_for("acme"), "acme")

    def test_instances_are_unique_across_channels(self):
        names = ["ag2space", "dev-ag2space", "local-ag2space", "acme"]
        got = [sparrowd.instance_for(n) for n in names]
        self.assertEqual(len(set(got)), len(got), got)


class TestSpecs(Base):
    def test_one_spec_per_relay_channel(self):
        for n in ("ag2space", "dev-ag2space", "discord"):
            var = "DISCORD_BOT_TOKEN" if n == "discord" else "REMOTE_TASK_TOKEN"
            _channel(self.base, n, var=var)
        specs = sparrowd.worker_specs(self.base)
        self.assertEqual([s.name for s in specs],
                         ["gateway-ag2space", "gateway-dev-ag2space"])

    def test_each_spec_carries_its_channel_and_instance(self):
        _channel(self.base, "dev-ag2space")
        s = sparrowd.worker_specs(self.base)[0]
        self.assertEqual(s.env["REMOTE_TASK_CHANNEL_DIR"], "dev-ag2space")
        self.assertEqual(s.env["GATEWAY_INSTANCE"], "dev")

    def test_every_spec_runs_the_real_bridge(self):
        _channel(self.base, "ag2space")
        s = sparrowd.worker_specs(self.base)[0]
        self.assertTrue(s.argv[-1].endswith("remote-gateway-bridge.py"))
        self.assertTrue(Path(s.argv[-1]).is_file(), s.argv[-1])

    def test_worker_names_are_unique(self):
        for n in ("ag2space", "dev-ag2space", "local-ag2space"):
            _channel(self.base, n)
        names = [s.name for s in sparrowd.worker_specs(self.base)]
        self.assertEqual(len(set(names)), len(names), names)

    def test_no_channels_means_no_workers(self):
        self.assertEqual(sparrowd.worker_specs(self.base), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
