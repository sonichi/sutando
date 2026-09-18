#!/usr/bin/env python3
"""skills/image-generation/scripts/generate.py: one JSON line on stdout, the exit code per error,
the key from the gemini-image capability, the file under <workspace>/results/media, no SDK for
images. urlopen is monkeypatched; nothing here touches the network or the real workspace.

Run: python3 tests/image-generation-cli.test.py
"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "image-generation" / "scripts" / "generate.py"
spec = importlib.util.spec_from_file_location("generate", SCRIPT)
gen = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(gen)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def response(parts=None, finish=None, block=None):
    body = {"candidates": [{"content": {"parts": parts or []}}]}
    if finish:
        body["candidates"][0]["finishReason"] = finish
    if block:
        body["promptFeedback"] = {"blockReason": block}
    return body


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.calls = []
        # The workspace and the key are the two things the script resolves from the core tree.
        self.patches = [mock.patch.object(gen, "media_dir", lambda: self.ws / "results" / "media"),
                        mock.patch.object(gen, "load_env", lambda: None),
                        mock.patch.object(gen, "resolve_key", lambda: ("k-test", "managed"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def opener(self, body=None, error=None):
        def _open(req, timeout=None):
            self.calls.append(req)
            if error is not None:
                raise error
            return _Resp(json.dumps(body).encode())
        return _open

    def run_main(self, *argv, opener=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gen.main(list(argv), opener=opener)
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"stdout must be exactly one JSON line, got: {out.getvalue()!r}")
        return rc, json.loads(lines[0])


class Success(Base):
    def test_a_png_lands_under_results_media_and_the_line_says_ok(self):
        parts = [{"text": "Here you go"}, {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]
        rc, line = self.run_main("--prompt", "a teal mascot", opener=self.opener(response(parts)))
        self.assertEqual(rc, 0)
        self.assertTrue(line["ok"])
        path = Path(line["path"])
        self.assertEqual(path.parent, (self.ws / "results" / "media").resolve())
        self.assertTrue(path.name.startswith("generated-") and path.suffix == ".png")
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual((line["model"], line["note"]), (gen.DEFAULT_IMAGE_MODEL, "Here you go"))
        self.assertFalse(list((self.ws / "results" / "media").glob(".*.tmp")), "no temp file left")

    def test_the_request_is_rest_with_the_key_header_and_both_modalities(self):
        parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]
        self.run_main("--prompt", "x", "--model", "some-image-model", opener=self.opener(response(parts)))
        req = self.calls[0]
        self.assertEqual(req.full_url, gen.ENDPOINT.format(model="some-image-model"))
        self.assertEqual(req.get_header("X-goog-api-key"), "k-test")
        body = json.loads(req.data.decode())
        self.assertEqual(body["generationConfig"]["responseModalities"], ["IMAGE", "TEXT"])
        self.assertEqual(body["contents"][0]["parts"], [{"text": "x"}])

    def test_explicit_output_and_an_input_image_are_honoured(self):
        src = self.ws / "in.png"; src.write_bytes(PNG)
        out = self.ws / "out" / "pic.png"
        parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]
        rc, line = self.run_main("--prompt", "edit", "--input", str(src), "--output", str(out), opener=self.opener(response(parts)))
        self.assertEqual((rc, line["path"]), (0, str(out.resolve())))
        sent = json.loads(self.calls[0].data.decode())["contents"][0]["parts"]
        self.assertEqual(sent[0]["inlineData"]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(sent[0]["inlineData"]["data"]), PNG)
        self.assertEqual(sent[1], {"text": "edit"})

    def test_the_model_comes_from_env_then_the_manifest(self):
        parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]
        with mock.patch.dict(os.environ, {"IMAGE_MODEL": "env-model"}):
            _, line = self.run_main("--prompt", "x", opener=self.opener(response(parts)))
        self.assertEqual(line["model"], "env-model")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMAGE_MODEL", None)
            _, line = self.run_main("--prompt", "x", opener=self.opener(response(parts)))
        manifest = json.loads((ROOT / "skills" / "image-generation" / "manifest.json").read_text())
        self.assertEqual(line["model"], manifest["config"]["IMAGE_MODEL"])


class Failures(Base):
    def test_no_key_is_exit_2_and_names_the_remedy(self):
        with mock.patch.object(gen, "resolve_key", lambda: ("", "none")):
            rc, line = self.run_main("--prompt", "x", opener=self.opener(response()))
        self.assertEqual(rc, 2)
        self.assertEqual((line["ok"], line["error"]), (False, "no_key"))
        self.assertIn("Agent settings", line["remedy"])
        self.assertEqual(self.calls, [], "no request without a key")

    def test_no_image_carries_the_model_text_and_exits_1(self):
        rc, line = self.run_main("--prompt", "x", opener=self.opener(response([{"text": "I can only describe it"}])))
        self.assertEqual((rc, line["error"], line["message"]), (1, "no_image", "I can only describe it"))
        self.assertFalse((self.ws / "results" / "media").exists() and list((self.ws / "results" / "media").iterdir()))

    def test_a_refusal_is_named(self):
        rc, line = self.run_main("--prompt", "x", opener=self.opener(response([{"text": "no"}], finish="IMAGE_SAFETY")))
        self.assertEqual((rc, line["error"]), (1, "refused"))
        self.assertIn("IMAGE_SAFETY", line["message"])
        rc, line = self.run_main("--prompt", "x", opener=self.opener(response(block="PROHIBITED_CONTENT")))
        self.assertEqual((rc, line["error"]), (1, "refused"))

    def test_an_http_error_is_api_error_with_googles_message(self):
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"error": {"message": "model not found"}}'))
        rc, line = self.run_main("--prompt", "x", opener=self.opener(error=err))
        self.assertEqual((rc, line["error"], line["message"]), (1, "api_error", "model not found"))
        rc, line = self.run_main("--prompt", "x", opener=self.opener(error=urllib.error.URLError("dns down")))
        self.assertEqual((rc, line["error"]), (1, "api_error"))
        self.assertIn("dns down", line["message"])

    def test_a_missing_input_image_is_bad_input_before_any_request(self):
        rc, line = self.run_main("--prompt", "x", "--input", str(self.ws / "nope.png"), opener=self.opener(response()))
        self.assertEqual((rc, line["error"], self.calls), (2, "bad_input", []))

    def test_video_without_the_sdk_says_so(self):
        with mock.patch.dict(sys.modules, {"google": None, "google.genai": None}):
            rc, line = self.run_main("--video", "--prompt", "x")
        self.assertEqual((rc, line["error"]), (2, "sdk_missing"))
        self.assertIn("google-genai", line["remedy"])


class Contract(unittest.TestCase):
    def test_images_need_no_sdk_and_the_key_is_the_image_capability(self):
        src = SCRIPT.read_text()
        head = src.split("def generate_video")[0]
        self.assertNotIn("google.genai", head, "the image path must not import the SDK")
        self.assertIn('resolve_credential("gemini-image")', src)
        self.assertIn("x-goog-api-key", src)
        doc = (ROOT / "skills" / "image-generation" / "SKILL.md").read_text()
        self.assertIn("Never run `pip` for this skill", doc)
        self.assertNotIn("pip3 install Pillow", doc)

    def test_the_manifest_is_enabled_and_documented_for_core(self):
        m = json.loads((ROOT / "skills" / "image-generation" / "manifest.json").read_text())
        self.assertTrue(m["enabled"] and m["documented_for_core"])
        self.assertTrue(m["core_description"] and m["config"]["IMAGE_MODEL"])
        self.assertNotIn("tools", m, "documented_for_core skills are delegated through work, not inline")

    def test_the_skill_doc_carries_the_delivery_step_and_the_verbatim_messages(self):
        doc = (ROOT / "skills" / "image-generation" / "SKILL.md").read_text()
        self.assertIn("[file: <path>]", doc)
        self.assertIn("I can't generate images on this install yet: there is no Gemini key for image", doc)
        for err in ("no_key", "refused", "no_image", "api_error", "sdk_missing"):
            self.assertIn(f"`{err}`", doc)


if __name__ == "__main__":
    unittest.main(verbosity=1)
