#!/usr/bin/env python3
"""#2228 regression guard: conversation-server must load .env via fileURLToPath,
NOT `new URL(...).pathname`.

`URL.pathname` percent-encodes special chars (space -> %20), so on an install
path that contains a space the resulting string is not a real filesystem path
and dotenv silently fails to load .env — the phone server then boots without its
env. #2233 fixed this exact pattern elsewhere but missed the line-48 .env load;
this test locks it so the paths-with-spaces class (#2228) cannot silently
reappear here. Structural assertion (mirrors the other bridge source tests).
Run: python3 tests/phone-env-fileurltopath.test.py
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "skills/phone-conversation/scripts/conversation-server.ts"
text = SRC.read_text()

m = re.search(r"_dotenvConfig\(\{.*?\}\)", text, re.S)
assert m, "could not find the _dotenvConfig({...}) call in conversation-server.ts"
call = m.group(0)

assert ".pathname" not in call, (
    "FAIL: the .env load still builds a file path via URL.pathname — "
    "breaks on an install path with a space (#2228). Use fileURLToPath.\n  " + call
)
assert "fileURLToPath" in call, (
    "FAIL: the .env load should resolve the path via fileURLToPath(new URL(...)).\n  " + call
)

print("PASS — conversation-server loads .env via fileURLToPath, not URL.pathname (#2228 guard)")
sys.exit(0)
