#!/usr/bin/env python3
"""
Control Center Agent
====================
Runs locally on the host machine alongside the claude CLI.
Listens for job pushes from the Django API, runs claude, posts results back.

Usage:
    python3 agent/agent.py

Config (env vars):
    AGENT_PORT   — port to listen on (default: 8002)
    CLAUDE_BIN   — path to claude binary (default: ~/.local/bin/claude)
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AGENT_PORT = int(os.environ.get("AGENT_PORT", 8002))
CLAUDE_BIN = Path(os.environ.get("CLAUDE_BIN", Path.home() / ".local/bin/claude"))


def run_claude(job_id: str, run_id: str, md_content: str, callback_url: str, project_path: str):
    """Run claude against the md content, stream output, post result back."""
    cwd = project_path if project_path else str(Path.home() / "Desktop/olive")
    print(f"[agent] Running job {job_id} in {cwd}")

    try:
        proc = subprocess.run(
            [
                str(CLAUDE_BIN),
                "-p", md_content,
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "--verbose",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        output = proc.stdout + proc.stderr
        status = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        output = f"Agent error: {exc}"
        status = "error"

    print(f"[agent] Job {job_id} finished with status={status}")

    # Post result back to Django
    try:
        import urllib.request
        payload = json.dumps({"output": output, "status": status}).encode()
        req = urllib.request.Request(
            callback_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[agent] Result posted for job {job_id}")
    except Exception as exc:
        print(f"[agent] Failed to post result for job {job_id}: {exc}")


class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[agent] {self.command} {self.path} — {args[0]}")

    def send_json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.send_json(200, {"status": "ok"})
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/run":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return self.send_json(400, {"error": "invalid JSON"})

            job_id       = payload.get("job_id", "")
            run_id       = payload.get("run_id", "")
            md_content   = payload.get("md_content", "")
            callback_url = payload.get("callback", "")
            project_path = payload.get("project_path", "")

            if not md_content or not callback_url:
                return self.send_json(400, {"error": "missing md_content or callback"})

            threading.Thread(
                target=run_claude,
                args=(job_id, run_id, md_content, callback_url, project_path),
                daemon=True,
            ).start()

            return self.send_json(202, {"accepted": True, "job_id": job_id})

        self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    if not CLAUDE_BIN.exists():
        print(f"[warn] claude binary not found at {CLAUDE_BIN}")
        print("[warn] Set CLAUDE_BIN env var if it's in a different location")

    server = HTTPServer(("0.0.0.0", AGENT_PORT), AgentHandler)
    print(f"[agent] Listening on http://localhost:{AGENT_PORT}")
    print(f"[agent] Using claude at {CLAUDE_BIN}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[agent] Stopped.")
