#!/usr/bin/env python3
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = os.environ.get("VIBE_BACKEND", "http://127.0.0.1:8080")
PORT = int(os.environ.get("VIBE_GATEWAY_PORT", "8443"))
CERT = os.environ.get("VIBE_TLS_CERT", "")
KEY = os.environ.get("VIBE_TLS_KEY", "")

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _error_payload(self, status, message):
        payload = json.dumps({"error": {"message": message}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        url = BACKEND + self.path
        req = urllib.request.Request(url, data=body, method=self.command)
        for name, value in self.headers.items():
            if name.lower() not in HOP_HEADERS:
                req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                for name, value in resp.headers.items():
                    if name.lower() not in HOP_HEADERS:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header(
                "Content-Type", exc.headers.get("Content-Type", "application/json")
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            self._error_payload(502, "backend unavailable: %s" % exc)

    do_GET = _proxy
    do_POST = _proxy
    do_DELETE = _proxy

    def log_message(self, fmt, *args):
        sys.stderr.write(
            "[gateway] %s %s\n" % (self.log_date_time_string(), fmt % args)
        )


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def main():
    if not CERT or not KEY:
        sys.exit("[gateway] VIBE_TLS_CERT / VIBE_TLS_KEY saknas")
    if not os.path.isfile(CERT) or not os.path.isfile(KEY):
        sys.exit("[gateway] TLS-certifikat saknas: %s" % CERT)

    httpd = QuietServer(("0.0.0.0", PORT), ProxyHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(
        "[gateway] HTTPS lyssnar på 0.0.0.0:%d -> %s" % (PORT, BACKEND),
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
