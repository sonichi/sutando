#!/usr/bin/env python3
"""
Screen capture HTTP server — runs in a terminal (has Screen Recording permission).
The voice agent calls http://localhost:7845/capture to get instant screenshots.

Usage: python3 src/screen-capture-server.py
(Run in a terminal window — NOT as a launchd daemon)
"""

import http.server
import subprocess
import json
import os
from datetime import datetime

PORT = 7845
DIR = "/tmp/sutando-screenshots"

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        if self.path.startswith("/capture"):
            os.makedirs(DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            # Parse display number from query: /capture?display=2 or /capture?all=true
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            display = query.get("display", [None])[0]
            capture_all = query.get("all", ["false"])[0] == "true"
            try:
                if capture_all:
                    # Capture all displays separately
                    paths = []
                    for d in range(1, 5):  # up to 4 displays
                        p = f"{DIR}/screen-{ts}-d{d}.png"
                        result = subprocess.run(["screencapture", "-x", f"-D{d}", p], timeout=5, capture_output=True)
                        if result.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
                            paths.append(p)
                        else:
                            try: os.unlink(p)
                            except: pass
                            break  # no more displays
                    path = paths[0] if paths else f"{DIR}/screen-{ts}.png"
                else:
                    suffix = f"-d{display}" if display else ""
                    path = f"{DIR}/screen-{ts}{suffix}.png"
                    paths = [path]
                    cmd = ["screencapture", "-x"]
                    if display:
                        cmd.append(f"-D{display}")
                    cmd.append(path)
                    subprocess.run(cmd, timeout=5, check=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = {"status": "ok", "path": paths[0] if paths else path}
                if len(paths) > 1:
                    resp["all_paths"] = paths
                    resp["displays"] = len(paths)
                self.wfile.write(json.dumps(resp).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": str(e)}).encode())
        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"pong":true}')
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Screen capture server → http://localhost:{PORT}/capture")
    print("Keep this terminal open — it has Screen Recording permission.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
