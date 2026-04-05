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
            # Parse display number from query: /capture?display=2
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            display = query.get("display", [None])[0]
            suffix = f"-d{display}" if display else ""
            path = f"{DIR}/screen-{ts}{suffix}.png"
            try:
                cmd = ["screencapture", "-x"]
                if display:
                    cmd.append(f"-D{display}")
                cmd.append(path)
                subprocess.run(cmd, timeout=5, check=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "path": path}).encode())
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
