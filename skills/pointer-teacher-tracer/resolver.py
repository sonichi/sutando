#!/usr/bin/env python3
"""Pointer Teacher tracer — RESOLVER.
Intent string -> on-screen Target, written to the IPC command file.

Slice proven by the grill POCs: capture (:7845) -> gemini-3-flash-preview
(native [y,x] 0-1000 format, thinking off) -> normalized point + spoken line.
AX-first is the documented next layer (open item #3) — vision-only here, which
is the path the POC actually proved.

Usage: resolver.py "where do I commit here?"
Emits  /tmp/pointer-cmd.json : {"nx","ny","label","say","ts"}
"""
import base64, json, os, re, subprocess, sys, time, urllib.request, urllib.error

CMD = "/tmp/pointer-cmd.json"
MODEL = "gemini-3-flash-preview"

def die(msg, code=1):
    print(f"resolver: {msg}", file=sys.stderr); sys.exit(code)

query = " ".join(sys.argv[1:]).strip() or die("need a query")

key = ""
for line in open(os.path.expanduser("~/Documents/GitHub/sutando/.env"),
                 encoding="utf-8", errors="ignore"):
    if line.startswith("GEMINI_API_KEY="):
        key = line.split("=", 1)[1].strip().strip('"').strip("'"); break
key or die("no GEMINI_API_KEY")

# 1. capture main display via the production :7845 server
t0 = time.time()
cap = json.load(urllib.request.urlopen("http://localhost:7845/capture?display=1", timeout=8))
cap.get("status") == "ok" or die(f"capture failed: {cap}")
shot = cap["path"]
small = "/tmp/pointer-shot.jpg"
subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1568", shot, "--out", small],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b64 = base64.b64encode(open(small, "rb").read()).decode()

# 2. resolve Target via Gemini native point format (thinking disabled)
prompt = (
    f'Point to {query}. Return ONLY minified JSON, no prose, no code fence: '
    f'{{"point":[y,x],"label":"<3 words>","say":"<one friendly spoken sentence '
    f'(max 20 words) telling me where it is and what to do>"}}. '
    f'point is [y, x] normalized to 0-1000 over the whole image.'
)
body = json.dumps({
    "contents": [{"parts": [{"text": prompt},
                            {"inlineData": {"mimeType": "image/jpeg", "data": b64}}]}],
    "generationConfig": {"temperature": 0, "maxOutputTokens": 1200,
                         "thinkingConfig": {"thinkingBudget": 0}},
}).encode()
url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
       f"{MODEL}:generateContent?key={key}")
try:
    r = json.load(urllib.request.urlopen(
        urllib.request.Request(url, body, {"Content-Type": "application/json"}), timeout=60))
    txt = r["candidates"][0]["content"]["parts"][0]["text"].strip()
except urllib.error.HTTPError as e:
    die(f"gemini HTTP {e.code}: {e.read().decode()[:160]}")

raw = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
try:
    obj = json.loads(raw)
    y, x = float(obj["point"][0]), float(obj["point"][1])
    label, say = obj.get("label", query), obj.get("say", "")
except Exception:
    m = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", raw)
    m or die(f"unparseable model reply: {txt[:200]!r}")
    y, x = float(m.group(1)), float(m.group(2))
    label, say = query, ""

cmd = {"nx": round(x / 1000, 5), "ny": round(y / 1000, 5),
       "label": label, "say": say, "ts": time.time()}
open(CMD, "w").write(json.dumps(cmd))
print(f"resolved {query!r} -> nx={cmd['nx']} ny={cmd['ny']} "
      f"({(time.time()-t0)*1000:.0f} ms)  say={say!r}")
print(json.dumps(cmd))
