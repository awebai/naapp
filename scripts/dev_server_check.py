#!/usr/bin/env python3
"""Starts an app's development server the way a developer does (`make run` in
its directory, on a free port) and requires /live to answer with the app's id.
The server runs in its own process group, which is stopped when the check ends.

Usage: dev_server_check.py <app> (from the repository root). The environment
supplies the app's configuration, for example its database URL.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> int:
    app = sys.argv[1]
    port = free_port()
    server = subprocess.Popen(
        ["make", "run", f"API_PORT={port}"],
        cwd=Path("naapp") / app,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if server.poll() is not None:
                print(f"make run for {app} exited with {server.returncode} before /live answered", file=sys.stderr)
                return 1
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/live", timeout=2) as response:
                    live = json.loads(response.read())
            except OSError:
                time.sleep(0.5)
                continue
            if live.get("service") != app:
                print(f"/live answered {live!r}, not service {app}", file=sys.stderr)
                return 1
            print(f"make run serves {app} on /live")
            return 0
        print(f"make run for {app} did not answer /live within 90 s", file=sys.stderr)
        return 1
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
