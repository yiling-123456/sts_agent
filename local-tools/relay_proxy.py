"""Local HTTP -> HTTPS forwarder for the metamind GPT relay.

Why this exists
---------------
From this machine, the relay at newapi.metamind.work only completes TLS
handshakes that OpenSSL performs in *blocking* mode. Clients that drive the
handshake through a MemoryBIO / non-blocking socket (httpx -> anyio, and
urllib -> http.client once a timeout is set) stall forever in the ClientHello:
the relay never answers, and the caller sees
``ConnectTimeout ... _ssl.c:NNNN: The handshake operation timed out``.

httpx cannot be configured onto the blocking path, so instead of patching every
client we terminate the problem here: the agent (and any OpenAI SDK script)
talks plain HTTP to 127.0.0.1, and this process talks to the relay over a
blocking ssl socket that succeeds on every attempt.

Framing
-------
The upstream response body is relayed byte-for-byte. If the relay answered with
``Transfer-Encoding: chunked`` that header is forwarded and the already-framed
body is passed through untouched, so h11 (inside httpcore) de-chunks exactly
once and streaming completions keep flowing without buffering. Responses
without a transfer-encoding are delimited by closing the client connection.

Usage
-----
    python relay_proxy.py                 # listens on 127.0.0.1:8129
    RELAY_PROXY_PORT=9000 python relay_proxy.py

Then point clients at http://127.0.0.1:8129/v1 (in place of the https URL).
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = os.environ.get("RELAY_HOST", "newapi.metamind.work")
UPSTREAM_PORT = int(os.environ.get("RELAY_PORT", "443"))
LISTEN_HOST = os.environ.get("RELAY_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("RELAY_PROXY_PORT", "8129"))
UPSTREAM_TIMEOUT = float(os.environ.get("RELAY_PROXY_UPSTREAM_TIMEOUT", "900"))

# ``host`` is rewritten; the rest are hop-by-hop and must not be forwarded.
DROP_REQUEST_HEADERS = {"proxy-connection", "connection", "keep-alive", "te", "upgrade", "host"}
# Kept from the upstream response unless it is close-delimited.
HOP_BY_HOP_RESPONSE = {"proxy-connection", "connection", "keep-alive", "upgrade"}


def log(msg: str) -> None:
    print(msg, flush=True)


class RelayProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RelayProxy/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log("[proxy] %s - %s" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        self._forward("GET")

    def do_POST(self) -> None:
        self._forward("POST")

    def do_PUT(self) -> None:
        self._forward("PUT")

    def do_PATCH(self) -> None:
        self._forward("PATCH")

    def do_DELETE(self) -> None:
        self._forward("DELETE")

    def do_OPTIONS(self) -> None:
        self._forward("OPTIONS")

    # ---------------------------------------------------------------- internals

    def _read_body(self) -> bytes:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return b""
        try:
            length = int(raw_len)
        except ValueError:
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def _upstream_request(self, method: str, body: bytes) -> bytes:
        lines = [f"{method} {self.path} HTTP/1.1", f"Host: {UPSTREAM_HOST}"]
        for name, value in self.headers.items():
            if name.lower() in DROP_REQUEST_HEADERS:
                continue
            lines.append(f"{name}: {value}")
        # One upstream request per connection: the blocking handshake is the
        # expensive part and `close` delimits the body without extra parsing.
        lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        return head + body

    def _connect_upstream(self) -> ssl.SSLSocket:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=30.0)
        # Blocking wrap_socket: this is the handshake flavour the relay accepts.
        tls = ctx.wrap_socket(raw, server_hostname=UPSTREAM_HOST)
        tls.settimeout(UPSTREAM_TIMEOUT)
        return tls

    def _fail(self, status: int, reason: str) -> None:
        payload = reason.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _read_headers(self, tls: ssl.SSLSocket) -> tuple[bytes, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = tls.recv(65536)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _forward(self, method: str) -> None:
        body = self._read_body()
        try:
            tls = self._connect_upstream()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as 502
            log(f"[proxy] upstream connect failed: {type(exc).__name__}: {exc}")
            self._fail(502, f"relay proxy: upstream connect failed: {exc}")
            return

        chunked = False
        try:
            tls.sendall(self._upstream_request(method, body))
            head, rest = self._read_headers(tls)
            if not head:
                self._fail(502, "relay proxy: upstream closed before sending headers")
                return

            head_lines = head.split(b"\r\n")
            for line in head_lines[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"transfer-encoding":
                    chunked = b"chunked" in value.lower()

            self.wfile.write(head_lines[0] + b"\r\n")
            for line in head_lines[1:]:
                if not line.strip():
                    continue
                name = line.split(b":", 1)[0].strip().lower().decode("latin-1")
                if name in HOP_BY_HOP_RESPONSE:
                    continue
                if not chunked and name in {"content-length", "transfer-encoding"}:
                    # Body is relayed until upstream EOF, so its length is not
                    # known up front: let the client close-delimit instead.
                    continue
                self.wfile.write(line + b"\r\n")
            if not chunked:
                self.wfile.write(b"Connection: close\r\n")
            self.wfile.write(b"\r\n")

            if rest:
                self.wfile.write(rest)
                self.wfile.flush()
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away (e.g. cancelled stream)
        except Exception as exc:  # noqa: BLE001
            log(f"[proxy] relay error: {type(exc).__name__}: {exc}")
        finally:
            try:
                tls.close()
            except Exception:  # noqa: BLE001
                pass
            self.close_connection = True


class RelayProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    server = RelayProxyServer((LISTEN_HOST, LISTEN_PORT), RelayProxyHandler)
    log(f"[proxy] http://{LISTEN_HOST}:{LISTEN_PORT}  ->  https://{UPSTREAM_HOST}:{UPSTREAM_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("[proxy] shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
