#!/usr/bin/env python3
"""
GitHub webhook bridge — receives GitHub events and writes task files.

Listens on port 7846 for GitHub webhook payloads. Converts relevant events
(new issues, PRs, stars, comments) into task files in tasks/.

Setup:
  1. Start: python3 src/github-webhook.py &
  2. Expose via ngrok: ngrok http 7846
  3. Add webhook in GitHub repo settings → Payload URL = ngrok URL
     Content type: application/json
     Events: Issues, Pull requests, Stars, Issue comments

Usage:
  python3 src/github-webhook.py              # start server
  python3 src/github-webhook.py --port 7846  # custom port
"""

import json
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO / "tasks"
PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 7847

# Events we care about and how to summarize them
def format_event(event_type: str, payload: dict):
    """Convert a GitHub webhook payload into a task description. Returns None to skip."""
    action = payload.get("action", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")
    sender = payload.get("sender", {}).get("login", "unknown")

    if event_type == "issues" and action == "opened":
        issue = payload["issue"]
        return f"[GitHub] New issue #{issue['number']} by @{sender}: {issue['title']}\n{issue.get('body', '')[:500]}"

    if event_type == "pull_request" and action == "opened":
        pr = payload["pull_request"]
        return f"[GitHub] New PR #{pr['number']} by @{sender}: {pr['title']}\n{pr.get('body', '')[:500]}"

    if event_type == "pull_request" and action == "closed" and payload["pull_request"].get("merged"):
        pr = payload["pull_request"]
        return f"[GitHub] PR #{pr['number']} merged by @{sender}: {pr['title']}"

    if event_type == "star" and action == "created":
        count = payload.get("repository", {}).get("stargazers_count", "?")
        return f"[GitHub] New star from @{sender}! Total: {count}"

    if event_type == "issue_comment" and action == "created":
        issue = payload["issue"]
        comment = payload["comment"]
        # Skip bot comments and our own
        if comment.get("user", {}).get("type") == "Bot":
            return None
        return f"[GitHub] @{sender} commented on #{issue['number']} ({issue['title']}): {comment['body'][:300]}"

    return None


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        event_type = self.headers.get("X-GitHub-Event", "unknown")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        task_text = format_event(event_type, payload)
        if task_text:
            task_id = f"task-gh-{int(time.time() * 1000)}"
            task_content = f"id: {task_id}\ntimestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\ntask: {task_text}\nsource: github\n"
            TASKS_DIR.mkdir(exist_ok=True)
            (TASKS_DIR / f"{task_id}.txt").write_text(task_content)
            print(f"[{time.strftime('%H:%M:%S')}] {event_type}/{payload.get('action', '')} → {task_id}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "service": "github-webhook"}).encode())

    def log_message(self, format, *args):
        pass  # suppress request logs


def main():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"GitHub webhook bridge listening on port {PORT}")
    print(f"Events: issues.opened, pull_request.opened/merged, star.created, issue_comment.created")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.server_close()


if __name__ == "__main__":
    main()
