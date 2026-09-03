#!/usr/bin/env python3
"""Tests that get_weather() names the location when it falls back to the default.

`get_weather()` hardcodes San Francisco (37.77, -122.42) and overrides it only
from WEATHER_LAT/WEATHER_LON. On any install that has not set those, the
briefing stated San Francisco's conditions as the owner's own weather, in the
same words it uses when the coordinates ARE the owner's — the configured and
unconfigured renderings were byte-identical, so nothing in the output could
distinguish them.

The fallback now names itself. A configured install is unaffected.

No real network runs here.
"""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"

PAYLOAD = {
    "current": {"temperature_2m": 59.4, "weather_code": 3},
    "daily": {
        "temperature_2m_max": [73.1],
        "temperature_2m_min": [57.2],
        "precipitation_probability_max": [5],
    },
}


def _load():
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(PAYLOAD).encode()


class TestWeatherLocation(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.captured = {}

        def _fake_urlopen(url, *a, **k):
            self.captured["url"] = url
            return _Resp()

        patcher = patch.object(self.mod, "urlopen", _fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _weather(self, env):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("WEATHER_LAT", "WEATHER_LON")}
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True):
            return self.mod.get_weather()

    def test_unconfigured_names_the_default_location(self):
        """The owner must be able to tell this is not their weather."""
        w = self._weather({})
        self.assertIn("San Francisco", w)
        # The reading itself is still reported.
        self.assertIn("59", w)
        self.assertIn("high of 73", w)

    def test_spoken_string_carries_no_env_var_remedy(self):
        """This sentence is spoken aloud (module header: "voice speaks it").

        A voice user would hear "set weather underscore lat slash weather
        underscore lon" mid-forecast. The label belongs in speech; the remedy
        belongs in the log line, so keep the variable names out of the string.
        """
        w = self._weather({})
        self.assertNotIn("WEATHER_LAT", w)
        self.assertNotIn("WEATHER_LON", w)
        self.assertNotIn("_", w)

    def test_configured_is_not_annotated(self):
        """A owner-set location is theirs; adding a caveat would be noise."""
        w = self._weather({"WEATHER_LAT": "33.45", "WEATHER_LON": "-112.07"})
        self.assertNotIn("San Francisco", w)
        self.assertNotIn("default location", w)
        self.assertIn("59", w)

    def test_configured_coordinates_are_the_ones_queried(self):
        """Guards the annotation against being driven off the wrong signal."""
        self._weather({"WEATHER_LAT": "33.45", "WEATHER_LON": "-112.07"})
        self.assertIn("latitude=33.45", self.captured["url"])
        self.assertIn("longitude=-112.07", self.captured["url"])

    def test_the_two_renderings_are_distinguishable(self):
        """The defect was that they were identical; pin that they differ."""
        self.assertNotEqual(
            self._weather({}),
            self._weather({"WEATHER_LAT": "33.45", "WEATHER_LON": "-112.07"}),
        )


if __name__ == "__main__":
    unittest.main()
