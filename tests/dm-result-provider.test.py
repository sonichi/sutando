#!/usr/bin/env python3
"""dm-result delivers through the shared DiscordRestClient — its private
Discord transport (raw urllib API wrapper, hand-rolled multipart, private
filename sanitize) is retired; orchestration (voice gate, markers, allowlist,
chunking, batching) stays.

Run: python3 tests/dm-result-provider.test.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-dmr-provider-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')

from outbox import DeliveryOutcome, RetrySafety  # noqa: E402
from outbox_adapter import DeliveryReceipt  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def load():
    spec = importlib.util.spec_from_file_location("dmr_p", SRC / "dm-result.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dmr_p"] = mod
    spec.loader.exec_module(mod)
    return mod


class StubClient:
    def __init__(self, send_outcomes=None, upload_outcomes=None):
        self.sends = []
        self.uploads = []
        self.dm_opens = []
        self.user_gets = []
        self._send_outcomes = send_outcomes or []
        self._upload_outcomes = upload_outcomes or []

    def _receipt(self, pool):
        oc = pool.pop(0) if pool else DeliveryOutcome.CONFIRMED
        return DeliveryReceipt(oc, receipt_id="m1" if oc is DeliveryOutcome.CONFIRMED else None,
                               safety=RetrySafety.UNSAFE, detail="stub")

    def send_message(self, channel_id, payload):
        self.sends.append((channel_id, payload))
        return self._receipt(self._send_outcomes)

    def upload_files(self, channel_id, payload, files):
        self.uploads.append((channel_id, payload, files))
        return self._receipt(self._upload_outcomes)

    def create_dm_channel(self, recipient_id):
        self.dm_opens.append(recipient_id)
        return "dm-chan-1"

    def get_user(self, uid):
        self.user_gets.append(uid)
        return {"id": uid, "bot": False}


def main() -> int:
    dmr = load()
    stub = StubClient()
    dmr._client = lambda token: stub
    dmr._load_token = lambda: "tok"
    dmr._resolve_owner_id = lambda token: "owner-1"

    # 1. Text chunks go through client.send_message; success -> True.
    ok = dmr.send_dm("hello world")
    check("send_dm delivers via client.send_message", ok is True and len(stub.sends) == 1)
    check("DM channel opened via client", stub.dm_opens == ["owner-1"])

    # 2. NOT_DELIVERED aborts honestly.
    stub = StubClient(send_outcomes=[DeliveryOutcome.NOT_DELIVERED])
    dmr._client = lambda token: stub
    check("NOT_DELIVERED chunk -> send_dm False", dmr.send_dm("x") is False)

    # 3. OUTCOME_UNKNOWN also aborts (no blind resend from the fallback path).
    stub = StubClient(send_outcomes=[DeliveryOutcome.OUTCOME_UNKNOWN])
    dmr._client = lambda token: stub
    check("OUTCOME_UNKNOWN chunk -> send_dm False (no blind resend)",
          dmr.send_dm("x") is False)

    # 4. File markers: allowlisted file uploads as (basename, bytes) via client.
    with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False,
                                     dir=str(REPO / "results") if (REPO / "results").is_dir() else None) as tf:
        tf.write(b"FILE-BYTES")
        fpath = tf.name
    stub = StubClient()
    dmr._client = lambda token: stub
    dmr._is_path_sendable = lambda p: True
    ok = dmr.send_dm(f"see attachment [file: {fpath}]")
    check("upload path: client.upload_files called", ok is True and len(stub.uploads) == 1)
    if stub.uploads:
        _, _, files = stub.uploads[0]
        check("upload passes (basename, bytes)",
              files == [(os.path.basename(fpath), b"FILE-BYTES")])
    os.unlink(fpath)

    # 5. Batching at 10 files per message.
    paths = []
    for i in range(12):
        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as tf:
            tf.write(b"B")
            paths.append(tf.name)
    stub = StubClient()
    dmr._client = lambda token: stub
    markers = " ".join(f"[file: {p}]" for p in paths)
    ok = dmr.send_dm(f"batch test {markers}")
    check("12 files -> 2 upload batches (10 + 2)",
          ok is True and [len(u[2]) for u in stub.uploads] == [10, 2])
    for p in paths:
        os.unlink(p)

    # 5b. The retired multipart path's 30s timeout survives the migration
    #     (the client defaults to 15; review finding on #3094).
    real = dmr._client
    try:
        del dmr.__dict__["_client"]
    except KeyError:
        pass
    import importlib
    spec2 = importlib.util.spec_from_file_location("dmr_p2", SRC / "dm-result.py")
    fresh = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(fresh)
    check("production _client preserves the 30s upload timeout",
          fresh._client("tok")._timeout == 30)
    dmr._client = real

    # 6. The private transport is gone (no raw urllib Discord API wrapper).
    body = (SRC / "dm-result.py").read_text()
    check("no private _discord_api remains", "_discord_api" not in body)
    check("no private multipart assembly remains", "payload_json" not in body)
    check("no private filename sanitize remains (client owns it)",
          'replace(\'"\', "_")' not in body)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: dm-result delivers through the shared DiscordRestClient")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
