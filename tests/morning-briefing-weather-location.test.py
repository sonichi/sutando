#!/usr/bin/env python3
"""Weather requires a valid owner-selected location; no real network calls."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
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
                 if k not in ("WEATHER_LAT", "WEATHER_LON", "WEATHER_UNIT")}
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True), \
                patch("sutando_config.load_config", return_value={}):
            return self.mod.get_weather()

    def test_missing_or_invalid_location_skips_network(self):
        for cfg in ({}, {"WEATHER_LAT": "33"}, {"WEATHER_LON": "10"},
                    {"WEATHER_LAT": "bad", "WEATHER_LON": "10"},
                    {"WEATHER_LAT": "91", "WEATHER_LON": "10"},
                    {"WEATHER_LAT": "0", "WEATHER_LON": "181"},
                    {"WEATHER_LAT": "nan", "WEATHER_LON": "10"},
                    {"WEATHER_LAT": "0", "WEATHER_LON": "inf"}):
            with self.subTest(cfg=cfg):
                self.captured.clear()
                self.assertIsNone(self._weather(cfg))
                self.assertEqual(self.captured, {})

    def test_zero_coordinates_are_valid(self):
        self.assertIn("59", self._weather({"WEATHER_LAT": "0", "WEATHER_LON": "0"}))
        self.assertIn("latitude=0.0&longitude=0.0", self.captured["url"])

    def test_unknown_location_asks_in_narrative(self):
        narrative = self.mod.synthesize(None, [], [], [], [], [],
                                        weather_location_known=False)
        self.assertIn("What city or town should I use for your weather forecast?", narrative)
        self.assertNotIn("San Francisco", narrative)
        self.assertNotIn("Everything looks clean", narrative)

    def test_network_failure_does_not_ask_for_known_location(self):
        narrative = self.mod.synthesize(None, [], [], [], [], [],
                                        weather_location_known=True)
        self.assertNotIn("What city", narrative)

    def test_main_writes_location_question_without_weather_request(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            stack.enter_context(patch.object(self.mod, "RESULTS_DIR", root / "results"))
            stack.enter_context(patch.object(self.mod, "STATE_DIR", root / "state"))
            stack.enter_context(patch("sutando_config.config_get", return_value=None))
            stack.enter_context(patch("telemetry.feature_used"))
            network = stack.enter_context(patch.object(self.mod, "urlopen"))
            for name in ("get_calendar_events", "get_reminders", "get_overnight_discord",
                         "get_pending_questions", "get_health_issues"):
                stack.enter_context(patch.object(self.mod, name, return_value=[]))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.mod.main()
            results = list((root / "results").glob("*.txt"))
            self.assertEqual(len(results), 1)
            body = results[0].read_text()
            self.assertTrue(body.startswith("[dm-only]"))
            self.assertIn("What city or town should I use for your weather forecast?", body)
            self.assertIn("Your calendar is clear today", body)
            network.assert_not_called()

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


class TestWeatherUnit(unittest.TestCase):
    """WEATHER_UNIT was previously not configurable: temperature_unit was a
    fahrenheit literal in the Open-Meteo query, so any owner outside a
    Fahrenheit-reporting country saw the wrong scale even after WEATHER_LAT/
    WEATHER_LON were set correctly for their own location."""

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
                 if k not in ("WEATHER_LAT", "WEATHER_LON", "WEATHER_UNIT")}
        clean.update({"WEATHER_LAT": "33.45", "WEATHER_LON": "-112.07"})
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True), \
                patch("sutando_config.load_config", return_value={}):
            return self.mod.get_weather()

    def test_default_stays_fahrenheit(self):
        """Unset WEATHER_UNIT must not change behavior for existing installs."""
        w = self._weather({})
        self.assertIn("temperature_unit=fahrenheit", self.captured["url"])
        self.assertIn("°F", w)

    def test_celsius_changes_query_and_symbol(self):
        w = self._weather({"WEATHER_UNIT": "celsius"})
        self.assertIn("temperature_unit=celsius", self.captured["url"])
        self.assertIn("°C", w)
        self.assertNotIn("°F", w)

    def test_unit_is_case_insensitive(self):
        w = self._weather({"WEATHER_UNIT": "Celsius"})
        self.assertIn("temperature_unit=celsius", self.captured["url"])
        self.assertIn("°C", w)

    def test_unrecognized_unit_falls_back_to_fahrenheit(self):
        w = self._weather({"WEATHER_UNIT": "kelvin"})
        self.assertIn("temperature_unit=fahrenheit", self.captured["url"])
        self.assertIn("°F", w)


class DiagnosticAgreesWithRendering(unittest.TestCase):
    """The operator line and the sentence it describes must read one source.

    `get_weather()` resolves the location through `config_get`, which reads the
    config stanza BEFORE os.environ. An env-only check therefore answers a
    different question, and on the documented way to configure this — the
    config file — it reported "default location" over a correctly configured
    install, which trains the reader to ignore the one warning that matters.
    """

    def setUp(self):
        self.mod = _load()

    def _configured(self, cfg):
        import sutando_config
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("WEATHER_LAT", "WEATHER_LON")}
        with patch.dict(os.environ, clean, clear=True), \
                patch.object(sutando_config, "config_get",
                             lambda k, d=None: cfg.get(k, d)):
            return self.mod.weather_location_configured()

    def test_config_file_only_counts_as_configured(self):
        # The discriminating case: env is empty, the config stanza is not.
        self.assertTrue(self._configured({"WEATHER_LAT": "33.4255",
                                          "WEATHER_LON": "-111.9400"}))

    def test_nothing_set_is_unconfigured(self):
        self.assertFalse(self._configured({}))

    def test_a_half_set_location_is_unconfigured(self):
        self.assertFalse(self._configured({"WEATHER_LAT": "33.4255"}))

    def test_env_alone_still_counts(self):
        """config_get falls back to os.environ, so the legacy path is intact."""
        with patch.dict(os.environ,
                        {"WEATHER_LAT": "33.4255", "WEATHER_LON": "-111.9400"}):
            self.assertTrue(self.mod.weather_location_configured())


class DiagnosticAtTheCallSite(unittest.TestCase):
    """Drives main()'s own line, not the helper it calls.

    A suite that only exercises `weather_location_configured()` verifies the
    fix where it was introduced and leaves it unverified where it was observed:
    reverting just the call site to an os.environ read keeps that suite green.
    """

    class _Stop(Exception):
        """Raised from the next gatherer to end main() after the weather block."""

    def _weather_block_output(self, cfg):
        mod = _load()
        import sutando_config
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("WEATHER_LAT", "WEATHER_LON")}
        buf = io.StringIO()
        with patch.dict(os.environ, clean, clear=True), \
                patch.object(sutando_config, "config_get",
                             lambda k, d=None: cfg.get(k, d)), \
                patch.object(mod, "get_weather",
                             lambda: "70\u00b0F and clear, high of 75, low of 60"), \
                patch.object(mod, "get_calendar_events", side_effect=self._Stop), \
                patch.object(sys, "argv", ["morning-briefing.py", "--force"]), \
                contextlib.redirect_stdout(buf):
            with self.assertRaises(self._Stop):
                mod.main()
        return buf.getvalue()

    def test_config_file_only_does_not_print_the_default_location_line(self):
        out = self._weather_block_output({"WEATHER_LAT": "33.4255",
                                          "WEATHER_LON": "-111.9400"})
        self.assertIn("weather: 70", out, "the block under test did not run")
        self.assertNotIn("location unknown", out)

    def test_unconfigured_prints_location_unknown(self):
        # The positive control: without it, the assertion above passes for a
        # line that was simply never reached.
        out = self._weather_block_output({})
        self.assertIn("weather: 70", out)
        self.assertIn("location unknown", out)


if __name__ == "__main__":
    unittest.main()
