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
import struct
import sys
import tempfile
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "image-generation" / "scripts" / "generate.py"
spec = importlib.util.spec_from_file_location("generate", SCRIPT)
gen = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(gen)



def png_1x1() -> bytes:
    """A real one-pixel RGB PNG, so a Pillow that is present decodes it instead of rejecting it."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + b"\x00\x80\x80")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


PNG = png_1x1()
NOT_A_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # the signature and nothing behind it


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


import types


class FakeImage:
    """The slice of PIL.Image this script touches: open → size/resize/convert/save."""
    saved = []

    def __init__(self, size=(8192, 4096)):
        self.size = size

    def resize(self, size, resample=None):
        return FakeImage(size)

    def convert(self, mode):
        return self

    def save(self, target, fmt=None, quality=None, format=None):
        blob = b"FAKE-" + (fmt or format or "PNG").encode()
        if isinstance(target, (str, Path)):
            Path(target).write_bytes(blob)
        else:
            target.write(blob)
        FakeImage.saved.append((fmt or format, quality))


def fake_pil(open=lambda fh: FakeImage()):
    image_mod = types.ModuleType("PIL.Image")
    image_mod.open = open
    image_mod.LANCZOS = 1
    pil = types.ModuleType("PIL")
    pil.Image = image_mod
    return {"PIL": pil, "PIL.Image": image_mod}


def _reject(fh):
    raise OSError("cannot identify image file")


def pillow_present():
    """The real Pillow when it is installed; otherwise a stub that rejects every image the way it would."""
    if importlib.util.find_spec("PIL") is not None:
        return contextlib.nullcontext()
    return mock.patch.dict(sys.modules, fake_pil(open=_reject))


class KeyAndEnv(Base):
    def test_load_env_reads_only_gemini_keys_from_the_two_env_files(self):
        home = self.ws / "home"; home.mkdir()
        repo = self.ws / "repo"; (repo / "skills" / "image-generation").mkdir(parents=True)
        (repo / ".env").write_text("# comment\n\nGEMINI_API_KEY='from-repo'\nOTHER=1\nnot a pair\n")
        (home / ".env").write_text('GEMINI_VOICE_API_KEY="from-home"\nGEMINI_API_KEY=home-loses\n')
        env = {"HOME": str(home), "GEMINI_API_KEY": "stale-shell"}
        with mock.patch.object(gen, "SKILL_DIR", repo / "skills" / "image-generation"), \
                mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GEMINI_VOICE_API_KEY", None)
            os.environ.pop("OTHER", None)
            for pt in self.patches:
                pt.stop()
            try:
                gen.load_env()
                self.assertEqual(os.environ["GEMINI_API_KEY"], "home-loses", "later file wins, quotes stripped")
                self.assertEqual(os.environ["GEMINI_VOICE_API_KEY"], "from-home")
                self.assertNotIn("OTHER", os.environ)
                (repo / ".env").unlink(); (home / ".env").unlink()
                gen.load_env()  # both files gone: nothing to read, nothing raised
            finally:
                for pt in self.patches:
                    pt.start()

    def test_resolve_key_prefers_the_resolver_then_the_env_chain(self):
        for pt in self.patches:
            pt.stop()
        try:
            got = types.SimpleNamespace(key="managed-k", source="managed")
            fake = types.ModuleType("credential_resolver")
            fake.resolve_credential = lambda cap: got if cap == "gemini-image" else None
            with mock.patch.dict(sys.modules, {"credential_resolver": fake}):
                self.assertEqual(gen.resolve_key(), ("managed-k", "managed"))
            with mock.patch.dict(sys.modules, {"credential_resolver": None}), \
                    mock.patch.dict(os.environ, {"GEMINI_VOICE_API_KEY": "voice-k"}, clear=False):
                os.environ.pop("GEMINI_API_KEY", None)
                self.assertEqual(gen.resolve_key(), ("voice-k", "env"))
                os.environ["GEMINI_API_KEY"] = "text-k"
                self.assertEqual(gen.resolve_key(), ("text-k", "env"), "the text key outranks the voice key")
                os.environ.pop("GEMINI_API_KEY"); os.environ.pop("GEMINI_VOICE_API_KEY")
                self.assertEqual(gen.resolve_key(), ("", "none"))
        finally:
            for pt in self.patches:
                pt.start()

    def test_manifest_config_and_media_dir_degrade_without_the_core_tree(self):
        with mock.patch.object(gen, "SKILL_DIR", self.ws / "no-such-skill"):
            self.assertIsNone(gen.manifest_config("IMAGE_MODEL"))
        for pt in self.patches:
            pt.stop()
        try:
            with_core = gen.media_dir()
            self.assertEqual((with_core.parent.name, with_core.name), ("results", "media"), "the workspace's attachment allowlist")
            with mock.patch.dict(sys.modules, {"workspace_default": None}):
                self.assertEqual(gen.media_dir().name, "sutando-media")
        finally:
            for pt in self.patches:
                pt.start()


class InputsAndOutputs(Base):
    def test_a_non_image_input_is_bad_input(self):
        txt = self.ws / "notes.txt"; txt.write_text("hi")
        self.assertIsNone(gen.read_input_image(str(txt)))
        rc, line = self.run_main("--prompt", "x", "--input", str(txt), opener=self.opener(response()))
        self.assertEqual((rc, line["error"]), (2, "bad_input"))

    def test_a_large_input_is_downscaled_when_pillow_is_around(self):
        FakeImage.saved.clear()
        src = self.ws / "big.png"; src.write_bytes(PNG)
        err = io.StringIO()
        with mock.patch.dict(sys.modules, fake_pil()), contextlib.redirect_stderr(err):
            data, mime = gen.read_input_image(str(src))
        self.assertEqual((data, mime), (b"FAKE-PNG", "image/png"))
        self.assertIn("Resized", err.getvalue())
        self.assertIn("4096x2048", err.getvalue())
        with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            self.assertEqual(gen.read_input_image(str(src)), (PNG, "image/png"), "no Pillow: bytes pass through as-is")

    def test_a_corrupt_image_file_is_bad_input_with_pillow_present(self):
        bad = self.ws / "bad.png"; bad.write_bytes(NOT_A_PNG)
        err = io.StringIO()
        with pillow_present(), contextlib.redirect_stderr(err):
            self.assertIsNone(gen.read_input_image(str(bad)))
            rc, line = self.run_main("--prompt", "x", "--input", str(bad), opener=self.opener(response()))
        self.assertEqual((rc, line["error"]), (2, "bad_input"))
        self.assertEqual(self.calls, [], "nothing is sent for an image Pillow cannot read")
        self.assertIn("Not a readable image", err.getvalue())

    def test_a_returned_blob_pillow_cannot_convert_keeps_its_own_extension(self):
        parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(NOT_A_PNG).decode()}}]
        out = self.ws / "pic.jpg"
        with pillow_present():
            rc, line = self.run_main("--prompt", "x", "--output", str(out), opener=self.opener(response(parts)))
        self.assertEqual((rc, Path(line["path"]).suffix), (0, ".png"))
        self.assertEqual(Path(line["path"]).read_bytes(), NOT_A_PNG)
        self.assertFalse(out.exists())

    def test_http_error_without_a_json_body_reads_as_its_code(self):
        err = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(b"<html>oops</html>"))
        self.assertEqual(gen.api_error_message(err), "HTTP 503")

    def test_jpg_output_converts_with_pillow_and_falls_back_to_the_returned_format_without_it(self):
        FakeImage.saved.clear()
        parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]
        out = self.ws / "pic.jpg"
        with mock.patch.dict(sys.modules, fake_pil()):
            rc, line = self.run_main("--prompt", "x", "--output", str(out), "--quality", "70", opener=self.opener(response(parts)))
        self.assertEqual((rc, line["path"]), (0, str(out.resolve())))
        self.assertEqual(out.read_bytes(), b"FAKE-JPEG")
        self.assertEqual(FakeImage.saved, [("JPEG", 70)])
        out2 = self.ws / "pic2.webp"
        with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            rc, line = self.run_main("--prompt", "x", "--output", str(out2), opener=self.opener(response(parts)))
        self.assertEqual(Path(line["path"]).suffix, ".png", "no Pillow: the returned png keeps its own extension")
        self.assertEqual(Path(line["path"]).read_bytes(), PNG)
        out3 = self.ws / "pic3.bmp"
        rc, line = self.run_main("--prompt", "x", "--output", str(out3), opener=self.opener(response(parts)))
        self.assertEqual(Path(line["path"]).suffix, ".png", "an unknown extension is never a mislabel")


class FakeGenai:
    """The slice of google-genai the video path touches."""

    def __init__(self, fail=None, polls=1):
        self.fail = fail
        self.polls = polls
        self.calls = []
        self.downloaded = []
        outer = self

        class _Op:
            def __init__(self, done):
                self.done = done
                video = types.SimpleNamespace(video=types.SimpleNamespace(save=lambda path: Path(path).write_bytes(b"MP4")))
                self.response = types.SimpleNamespace(generated_videos=[video])

        class _Models:
            def generate_videos(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.fail:
                    raise outer.fail
                return _Op(done=outer.polls == 0)

        class _Operations:
            def get(self, op):
                outer.polls -= 1
                return _Op(done=outer.polls <= 0)

        class _Files:
            def download(self, file=None):
                outer.downloaded.append(file)

        class Client:
            def __init__(self, api_key=None):
                outer.calls.append(("client", api_key))
                self.models, self.operations, self.files = _Models(), _Operations(), _Files()

        genai = types.ModuleType("google.genai")
        genai.Client = Client
        gtypes = types.ModuleType("google.genai.types")
        gtypes.Image = lambda image_bytes=None, mime_type=None: ("image", mime_type, len(image_bytes))
        gtypes.GenerateVideosConfig = lambda aspect_ratio=None: ("config", aspect_ratio)
        genai.types = gtypes
        google = types.ModuleType("google")
        google.genai = genai
        self.modules = {"google": google, "google.genai": genai, "google.genai.types": gtypes}


class Video(Base):
    def test_a_video_is_generated_polled_downloaded_and_saved(self):
        sdk = FakeGenai(polls=2)
        out = self.ws / "clip.mp4"
        with mock.patch.dict(sys.modules, sdk.modules), mock.patch.object(gen.time, "sleep", lambda s: None):
            rc, line = self.run_main("--video", "--prompt", "a city", "--output", str(out), "--aspect", "9:16")
        self.assertEqual((rc, line["ok"], line["path"], line["model"]), (0, True, str(out.resolve()), gen.DEFAULT_VIDEO_MODEL))
        self.assertEqual(out.read_bytes(), b"MP4")
        self.assertEqual(sdk.calls[0], ("client", "k-test"))
        self.assertEqual(sdk.calls[1]["config"], ("config", "9:16"))
        self.assertNotIn("image", sdk.calls[1])
        self.assertEqual(len(sdk.downloaded), 1)

    def test_a_reference_image_rides_along_and_a_missing_one_is_bad_input(self):
        sdk = FakeGenai(polls=0)
        src = self.ws / "ref.png"; src.write_bytes(PNG)
        with mock.patch.dict(sys.modules, sdk.modules), mock.patch.dict(os.environ, {"VIDEO_MODEL": "veo-test"}):
            rc, line = self.run_main("--video", "--prompt", "x", "--input", str(src), "--output", str(self.ws / "v.mp4"))
        self.assertEqual((rc, line["model"]), (0, "veo-test"))
        self.assertEqual(sdk.calls[1]["image"], ("image", "image/png", len(PNG)))
        with mock.patch.dict(sys.modules, sdk.modules):
            rc, line = self.run_main("--video", "--prompt", "x", "--input", str(self.ws / "nope.png"))
        self.assertEqual((rc, line["error"]), (2, "bad_input"))

    def test_every_sdk_failure_is_one_api_error(self):
        sdk = FakeGenai(fail=RuntimeError("quota exceeded"))
        with mock.patch.dict(sys.modules, sdk.modules):
            rc, line = self.run_main("--video", "--prompt", "x", "--output", str(self.ws / "v.mp4"))
        self.assertEqual((rc, line["error"], line["message"]), (1, "api_error", "quota exceeded"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
