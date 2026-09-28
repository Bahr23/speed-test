"""Shared local HTTP fixtures and CLI helpers; no collected test cases."""

import gzip
import io
import threading
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import measurement
import speed_test


BODY = b"download test data\n" * 8000  # Larger than two read buffers.
COMPRESSED_BODY = gzip.compress(BODY, mtime=0)
SCRIPT = Path(__file__).resolve().parents[1] / "speed_test.py"


@contextmanager
def local_server(*, body_release=None):
    requests_seen = []
    events = []
    arrivals = threading.Barrier(10)
    requests_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                # The client may close during a response or while waiting for another request.
                pass

        def do_GET(self):
            with requests_lock:
                requests_seen.append(
                    (self.path, self.client_address[1], self.headers.get("Accept-Encoding"))
                )
                self.number = sum(path == self.path for path, _, _ in requests_seen)
                events.append("start")
            self.respond()

        def respond(self):
            if self.path in {"/parallel", "/parallel-fail"}:
                # No response is sent until all ten requests have arrived together.
                try:
                    arrivals.wait(timeout=3)
                except threading.BrokenBarrierError:
                    self.send_error(503, "Ten concurrent requests did not arrive")
                    return
                events.append("barrier passed")
                if self.path == "/parallel-fail" and self.number == 3:
                    self.send_error(500, "Intentional parallel failure")
                    return
                time.sleep(0.15)
            redirects = {
                "/redirect": "/data",
                "/redirect-error": "/error",
                "/redirect-loop": "/redirect-loop",
            }
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            status = 200
            body = BODY
            if self.path in {"/error", "/not-found"} or (
                self.path == "/fail-third" and self.number == 3
            ):
                status = 404 if self.path == "/not-found" else 500
                body = b"server error"
            elif self.path == "/empty":
                body = b""
            elif self.path in {"/gzip", "/gzip-chunked"}:
                body = COMPRESSED_BODY
            elif self.path == "/slow-headers":
                time.sleep(0.25)
            elif self.path == "/ui-wait":
                time.sleep(0.6)

            chunked = self.path in {
                "/chunked", "/broken-chunked", "/chunked-length", "/gzip-chunked"
            }
            self.send_response(status)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
                if self.path == "/chunked-length":
                    self.send_header("Content-Length", str(len(body)))
            elif self.path == "/invalid-length":
                self.send_header("Content-Length", "unknown")
            elif self.path != "/no-length":
                length = len(body) + (100 if self.path == "/truncated" else 0)
                self.send_header("Content-Length", str(length))
            if status >= 400 or self.path in {
                "/no-length", "/invalid-length", "/truncated", "/broken-chunked"
            }:
                self.send_header("Connection", "close")
                self.close_connection = True
            if self.path in {"/gzip", "/gzip-chunked"}:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()

            if chunked:
                chunk_size = 17 if self.path == "/gzip-chunked" else 17000
                for start in range(0, len(body), chunk_size):
                    chunk = body[start : start + chunk_size]
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                if self.path != "/broken-chunked":
                    self.wfile.write(b"0\r\n\r\n")
            elif self.path == "/small-blocks":
                self.wfile.write(body[:1024])
                self.wfile.flush()
                body_release.wait(timeout=1)
                events.append("body")
                self.wfile.write(body[1024:])
            elif self.path == "/ui-trickle":
                for start in range(0, len(body), 1024):
                    self.wfile.write(body[start : start + 1024])
                    self.wfile.flush()
                    time.sleep(0.05)
            elif self.path == "/steady":
                for start in range(0, len(body), 16384):
                    self.wfile.write(body[start : start + 16384])
                    self.wfile.flush()
                    time.sleep(0.05)
            elif self.path in {"/sequential", "/delayed", "/slow-body", "/progress", "/ui-stream"}:
                split = measurement.CHUNK_SIZE if self.path in {"/progress", "/ui-stream"} else 1
                self.wfile.write(body[:split])
                self.wfile.flush()
                delay = {
                    "/sequential": 0.01, "/delayed": 0.08, "/slow-body": 0.25,
                    "/progress": 0.08, "/ui-stream": 0.6,
                }
                time.sleep(delay[self.path])
                events.append("body")
                self.wfile.write(body[split:])
            else:
                self.wfile.write(body)

    class TestServer(ThreadingHTTPServer):
        request_queue_size = 32

    server = TestServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False  # Join delayed handlers before the next test captures stderr.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests_seen, events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_cli(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = speed_test.main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


