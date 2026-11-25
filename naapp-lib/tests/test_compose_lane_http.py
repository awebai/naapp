"""The request a lane machine sends for ComposeLane.http, run here against a real
HTTP server: the same script, read from stdin, answering on stdout."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from aweb_naapp_testing.compose_lane import LANE_HTTP_SCRIPT, lane_response


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _answer(self, status: int, body: bytes, headers: list[tuple[str, str]]) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/cookies":
            self._answer(200, b"two", [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
        elif self.path == "/moved":
            self._answer(302, b"", [("Location", "/cookies")])
        elif self.path == "/broken":
            self._answer(503, b'{"detail":"down"}', [("Content-Type", "application/json")])
        else:
            self._answer(404, b'{"detail":"Not Found"}', [("Content-Type", "application/json")])

    def do_POST(self) -> None:
        sent = self.rfile.read(int(self.headers["Content-Length"]))
        echoed = {"body": sent.decode(), "header": self.headers["X-Probe"]}
        self._answer(201, json.dumps(echoed).encode(), [("Content-Type", "application/json")])


@pytest.fixture
def origin() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _send(method: str, url: str, *, body: bytes | None = None, headers=()):
    request = {
        "method": method,
        "url": url,
        "headers": list(headers),
        "body": None if body is None else body.hex(),
        "timeout": 10,
    }
    result = subprocess.run(
        [sys.executable, "-c", LANE_HTTP_SCRIPT],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return lane_response(method, url, result.stdout)


def test_repeated_headers_and_the_body_come_back(origin: str) -> None:
    response = _send("GET", f"{origin}/cookies")
    assert response.status_code == 200
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert response.content == b"two"


def test_an_error_status_is_an_answer_with_its_body(origin: str) -> None:
    missing = _send("GET", f"{origin}/nothing")
    assert (missing.status_code, missing.json()) == (404, {"detail": "Not Found"})
    down = _send("GET", f"{origin}/broken")
    assert (down.status_code, down.json()) == (503, {"detail": "down"})


def test_a_redirect_is_returned_not_followed(origin: str) -> None:
    response = _send("GET", f"{origin}/moved")
    assert response.status_code == 302
    assert response.headers["location"] == "/cookies"


def test_the_body_and_headers_are_sent(origin: str) -> None:
    response = _send("POST", f"{origin}/echo", body=b'{"x": 1}', headers=[("X-Probe", "seen")])
    assert response.status_code == 201
    assert response.json() == {"body": '{"x": 1}', "header": "seen"}
