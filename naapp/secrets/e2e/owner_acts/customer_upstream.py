"""Lane-local origin that counts broker calls without retaining credentials."""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from secrets import compare_digest

EXPECTED = (Path("/test-input/bearer").read_bytes()).strip()
RECEIPTS = Path("/tmp/customer-upstream-receipts")


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path != "/customer":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        authorized = (
            len(self.headers.get_all("Authorization", [])) == 1
            and compare_digest(
                self.headers["Authorization"].encode("latin1"), b"Bearer " + EXPECTED
            )
            and body == b"customer acceptance"
        )
        with RECEIPTS.open("a") as receipts:
            receipts.write("accepted\n" if authorized else "refused\n")
        if not authorized:
            self.send_error(HTTPStatus.UNAUTHORIZED)
            return
        # Deliberately attempt an accidental echo, as an upstream might.
        # The broker must withhold it from the agent.
        result = b'{"credential":"' + EXPECTED + b'"}'
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)


if __name__ == "__main__":
    RECEIPTS.touch(mode=0o600)
    HTTPServer(("0.0.0.0", 8080), Upstream).serve_forever()
