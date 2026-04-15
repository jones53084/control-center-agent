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

# job_id -> {"proc": Popen, "session_id": str} for cancellable/interjectible jobs
_running: dict = {}
_lock = threading.Lock()


def _post_json(url: str, payload: dict):
    import urllib.request
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def _post_result(callback_url: str, output: str, status: str):
    _post_json(callback_url, {"output": output, "status": status})


def _post_stream_chunk(job_id: str, line: str):
    """Post a single stream-json line to the API for live broadcasting."""
    try:
        _post_json(
            f"http://localhost:8080/api/jobs/{job_id}/stream/",
            {"line": line},
        )
    except Exception as exc:
        print(f"[agent] stream chunk failed for {job_id}: {exc}")


def run_claude(job_id: str, run_id: str, md_content: str, callback_url: str, project_path: str, session_id: str = ""):
    """Run claude from project_path, stream output live, post final result back."""
    project_path = (project_path or "").strip()

    if not project_path:
        try:
            _post_result(callback_url, "Agent error: project_path is required", "error")
        except Exception:
            pass
        return

    cwd = Path(project_path)
    if not cwd.is_dir():
        try:
            _post_result(callback_url, f"Agent error: project path does not exist: {project_path}", "error")
        except Exception:
            pass
        return

    print(f"[agent] Running job {job_id} in {cwd}")

    try:
        cmd = [str(CLAUDE_BIN)]
        if session_id:
            cmd += ["--resume", session_id]
            print(f"[agent] Resuming session {session_id} for job {job_id}")
        cmd += ["-p", md_content, "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd),
        )
        with _lock:
            _running[job_id] = {"proc": proc, "session_id": ""}

        # Stream stdout line-by-line to the API for live updates
        lines = []
        try:
            for line in proc.stdout:
                lines.append(line)
                stripped = line.strip()
                if stripped:
                    # Capture session_id from init or result messages
                    try:
                        obj = json.loads(stripped)
                        if obj.get("session_id"):
                            with _lock:
                                entry = _running.get(job_id)
                                if entry:
                                    entry["session_id"] = obj["session_id"]
                    except (json.JSONDecodeError, KeyError):
                        pass
                    _post_stream_chunk(job_id, stripped)
            stderr_out = proc.stderr.read()
            proc.wait()
        finally:
            with _lock:
                _running.pop(job_id, None)

        output = "".join(lines) + stderr_out
        if proc.returncode == 0:
            status = "done"
        elif proc.returncode < 0:
            status = "cancelled"
        else:
            status = "error"
    except Exception as exc:
        output = f"Agent error: {exc}"
        status = "error"

    print(f"[agent] Job {job_id} finished with status={status}")

    try:
        _post_result(callback_url, output, status)
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
            session_id   = payload.get("session_id", "")

            if not md_content or not callback_url:
                return self.send_json(400, {"error": "missing md_content or callback"})

            threading.Thread(
                target=run_claude,
                args=(job_id, run_id, md_content, callback_url, project_path, session_id),
                daemon=True,
            ).start()

            return self.send_json(202, {"accepted": True, "job_id": job_id})

        if self.path == "/cancel":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return self.send_json(400, {"error": "invalid JSON"})

            job_id = payload.get("job_id", "")
            with _lock:
                entry = _running.get(job_id)
            proc = entry["proc"] if entry else None
            if proc and proc.poll() is None:
                proc.terminate()
                print(f"[agent] Cancelled job {job_id}")
                return self.send_json(200, {"cancelled": True})
            return self.send_json(404, {"error": "job not running"})

        if self.path == "/interject":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return self.send_json(400, {"error": "invalid JSON"})

            job_id = payload.get("job_id", "")
            with _lock:
                entry = _running.get(job_id)
            if not entry:
                return self.send_json(404, {"error": "job not running"})

            proc = entry["proc"]
            session_id = entry["session_id"]

            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                print(f"[agent] Interjected job {job_id}, session={session_id}")

            return self.send_json(200, {
                "interjected": True,
                "session_id": session_id,
            })

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
