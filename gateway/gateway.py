#!/usr/bin/env python3
import hashlib
import json
import mimetypes
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = os.environ.get("VIBE_BACKEND", "http://127.0.0.1:8080")
PORT = int(os.environ.get("VIBE_GATEWAY_PORT", "8443"))
CERT = os.environ.get("VIBE_TLS_CERT", "")
KEY = os.environ.get("VIBE_TLS_KEY", "")
WEB_DIR = os.environ.get(
    "VIBE_WEB_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"),
)
ADMIN_PORT = int(os.environ.get("VIBE_ADMIN_PORT", "8444"))
ADMIN_DIR = os.path.dirname(os.path.abspath(__file__))
APPROVAL_TIMEOUT = int(os.environ.get("VIBE_APPROVAL_TIMEOUT", "120"))
REQUIRE_APPROVAL = os.environ.get("VIBE_REQUIRE_APPROVAL", "1") == "1"
STATE_FILE = os.environ.get("VIBE_STATE_FILE", "usage.json")

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


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.stats = {"total": 0, "approved": 0, "denied": 0, "timeout": 0, "by_ip": {}}
        self._load()

    def _load(self):
        try:
            with open(STATE_FILE) as fh:
                self.stats.update(json.load(fh))
        except Exception:
            pass

    def save(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.stats, fh)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass

    def register(self, ip, endpoint, size, body_sha):
        rid = hashlib.sha256(
            ("%f|%s|%s" % (time.time(), ip, endpoint)).encode()
        ).hexdigest()[:12]
        entry = {
            "id": rid,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": ip,
            "endpoint": endpoint,
            "size": size,
            "sha256": body_sha,
            "event": threading.Event(),
            "decision": None,
        }
        with self.lock:
            self.pending[rid] = entry
            self.stats["total"] += 1
            self.stats["by_ip"][ip] = self.stats["by_ip"].get(ip, 0) + 1
            self.save()
        return rid

    def decide(self, rid, decision):
        with self.lock:
            entry = self.pending.get(rid)
            if not entry or entry["decision"] is not None:
                return False
            entry["decision"] = decision
            entry["event"].set()
            self.stats[decision] += 1
            self.save()
            return True

    def expire(self, rid):
        with self.lock:
            entry = self.pending.get(rid)
            if entry:
                if entry["decision"] is None:
                    self.stats["timeout"] += 1
                    self.save()
                self.pending.pop(rid, None)

    def pending_list(self):
        with self.lock:
            return [
                {
                    "id": e["id"],
                    "time": e["time"],
                    "ip": e["ip"],
                    "endpoint": e["endpoint"],
                    "size": e["size"],
                    "sha256": e["sha256"],
                }
                for e in self.pending.values()
                if e["decision"] is None
            ]

    def stats_view(self):
        with self.lock:
            return dict(self.stats)


STATE = State()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_bytes(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error_payload(self, status, message):
        payload = json.dumps({"error": {"message": message}}).encode("utf-8")
        self._send_bytes(status, payload, "application/json")

    def _serve_static(self, root_dir):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/":
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith(".."):
            self._error_payload(403, "forbidden")
            return
        full = os.path.join(root_dir, rel)
        if not os.path.isfile(full):
            self._error_payload(404, "not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            payload = fh.read()
        self._send_bytes(200, payload, ctype)

    def _await_approval(self, ip, endpoint, size, body_sha):
        rid = STATE.register(ip, endpoint, size, body_sha)
        entry = STATE.pending.get(rid)
        granted = entry["event"].wait(timeout=APPROVAL_TIMEOUT)
        decision = entry["decision"]
        STATE.expire(rid)
        if not granted or decision is None:
            self._error_payload(
                408, "no decision from host within %d seconds" % APPROVAL_TIMEOUT
            )
            return False
        if decision != "approved":
            self._error_payload(403, "request denied by host")
            return False
        return True

    def _proxy(self):
        if self.command == "GET":
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/icon.svg"):
                self._serve_static(WEB_DIR)
                return

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        if self.command == "POST" and REQUIRE_APPROVAL:
            client_ip = self.client_address[0]
            body_sha = hashlib.sha256(body).hexdigest()[:16] if body else "-"
            if not self._await_approval(client_ip, self.path, length, body_sha):
                return

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


class AdminHandler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_admin()
        elif path == "/api/pending":
            self._send_json(STATE.pending_list())
        elif path == "/api/stats":
            self._send_json(STATE.stats_view())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] in ("approve", "deny"):
            decision = "approved" if parts[1] == "approve" else "denied"
            ok = STATE.decide(parts[2], decision)
            self._send_json({"ok": ok})
        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_admin(self):
        full = os.path.join(ADMIN_DIR, "admin.html")
        if not os.path.isfile(full):
            self._send_json({"error": "admin.html missing"}, 500)
            return
        with open(full, "rb") as fh:
            payload = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        sys.stderr.write(
            "[admin] %s %s\n" % (self.log_date_time_string(), fmt % args)
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

    admin = QuietServer(("127.0.0.1", ADMIN_PORT), AdminHandler)
    threading.Thread(target=admin.serve_forever, daemon=True).start()

    httpd = QuietServer(("0.0.0.0", PORT), ProxyHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(
        "[gateway] HTTPS lyssnar på 0.0.0.0:%d -> %s (UI: %s)"
        % (PORT, BACKEND, WEB_DIR),
        flush=True,
    )
    print(
        "[gateway] Admin-panel: http://127.0.0.1:%d (godkännande: %s, timeout: %ds)"
        % (ADMIN_PORT, "PÅ" if REQUIRE_APPROVAL else "AV", APPROVAL_TIMEOUT),
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
