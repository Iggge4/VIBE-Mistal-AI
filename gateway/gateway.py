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
from html.parser import HTMLParser
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
LIB_DIR = os.environ.get(
    "VIBE_LIB_DIR",
    os.path.join(os.path.dirname(os.path.abspath(WEB_DIR)), "data", "library"),
)
MAX_CONTEXT_CHARS = int(os.environ.get("VIBE_MAX_CONTEXT_CHARS", "12000"))

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


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "template"}
    BREAK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._chunks.append(data.strip() + " ")

    def text(self):
        lines = (ln.strip() for ln in "".join(self._chunks).splitlines())
        return "\n".join(ln for ln in lines if ln)


def extract_html_text(data):
    parser = _TextExtractor()
    try:
        parser.feed(data.decode("utf-8", errors="replace"))
    except Exception:
        return ""
    return parser.text()


def _lib_meta_path():
    return os.path.join(LIB_DIR, "library.json")


def _load_lib_meta():
    try:
        with open(_lib_meta_path()) as fh:
            return json.load(fh)
    except Exception:
        return []


def _save_lib_meta(docs):
    os.makedirs(LIB_DIR, exist_ok=True)
    tmp = _lib_meta_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(docs, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _lib_meta_path())


def library_add(name, source, text):
    doc_id = hashlib.sha256(
        ("%f|%s|%s" % (time.time(), name, source)).encode()
    ).hexdigest()[:12]
    os.makedirs(LIB_DIR, exist_ok=True)
    with open(os.path.join(LIB_DIR, doc_id + ".txt"), "w") as fh:
        fh.write(text)
    entry = {
        "id": doc_id,
        "name": name,
        "source": source,
        "chars": len(text),
        "date": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _lib_lock:
        docs = _load_lib_meta()
        docs.append(entry)
        _save_lib_meta(docs)
    return entry


def library_list():
    with _lib_lock:
        return _load_lib_meta()


def library_delete(doc_id):
    with _lib_lock:
        docs = _load_lib_meta()
        remaining = [d for d in docs if d["id"] != doc_id]
        if len(remaining) == len(docs):
            return False
        _save_lib_meta(remaining)
    try:
        os.remove(os.path.join(LIB_DIR, doc_id + ".txt"))
    except OSError:
        pass
    return True


def library_get(doc_ids):
    metas = {d["id"]: d for d in library_list()}
    out = []
    for doc_id in doc_ids:
        meta = metas.get(doc_id)
        if not meta:
            continue
        try:
            with open(os.path.join(LIB_DIR, doc_id + ".txt")) as fh:
                text = fh.read()
        except OSError:
            continue
        out.append((meta, text))
    return out


def fetch_url_text(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError("ogiltig URL (måste börja med http:// eller https://)")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; VIBE-Mistral-Ai)"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read(2 * 1024 * 1024)
    head = data[:512].lower()
    if "html" in ctype or b"<html" in head or b"<!doctype" in head:
        return extract_html_text(data)
    return data.decode("utf-8", errors="replace")


_lib_lock = threading.Lock()


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

    def _apply_library(self, body):
        try:
            data = json.loads(body)
        except Exception:
            return body
        if "library" not in data:
            return body
        ids = data.pop("library")
        docs = library_get(ids) if isinstance(ids, list) else []
        payload = json.dumps(data).encode("utf-8")
        if not docs:
            return payload
        parts = []
        used = 0
        truncated = False
        for meta, text in docs:
            if used >= MAX_CONTEXT_CHARS:
                truncated = True
                break
            chunk = text[: MAX_CONTEXT_CHARS - used]
            if len(chunk) < len(text):
                truncated = True
            parts.append(
                "Dokument: %s (källa: %s)\n%s" % (meta["name"], meta["source"], chunk)
            )
            used += len(chunk)
        context = "\n\n".join(parts)
        if truncated:
            context += "\n\n[Vissa dokument var för stora och har kortats av.]"
        missing = len(ids) - len(docs)
        note = " (%d valda dokument hittades inte)" % missing if missing > 0 else ""
        names = ", ".join(m["name"] for m, _ in docs)
        system_msg = {
            "role": "system",
            "content": (
                "Använd följande dokument från användarens bibliotek (%s)%s som "
                "kunskapskälla när du svarar. Svara på användarens språk.\n\n%s"
                % (names, note, context)
            ),
        }
        messages = data.get("messages")
        if isinstance(messages, list):
            data["messages"] = [system_msg] + messages
        return json.dumps(data).encode("utf-8")

    def _proxy(self):
        if self.command == "GET":
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/icon.svg"):
                self._serve_static(WEB_DIR)
                return
            if path == "/v1/library":
                self._send_bytes(
                    200,
                    json.dumps(library_list(), ensure_ascii=False).encode("utf-8"),
                    "application/json",
                )
                return

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        if self.command == "POST" and body:
            body = self._apply_library(body)

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
        elif path == "/api/library":
            self._send_json(library_list())
        else:
            self._send_json({"error": "not found"}, 404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _lib_add_file(self):
        data = self._read_json_body()
        if not data or not data.get("name") or not data.get("content"):
            self._send_json({"error": "namn och innehåll krävs"}, 400)
            return
        entry = library_add(str(data["name"]), "fil", str(data["content"]))
        self._send_json({"ok": True, "doc": entry})

    def _lib_add_url(self):
        data = self._read_json_body()
        url = (data or {}).get("url", "").strip() if data else ""
        if not url:
            self._send_json({"error": "url krävs"}, 400)
            return
        try:
            text = fetch_url_text(url)
        except Exception as exc:
            self._send_json({"error": "kunde inte hämta sidan: %s" % exc}, 502)
            return
        if not text.strip():
            self._send_json({"error": "ingen text kunde extraheras från sidan"}, 400)
            return
        entry = library_add(url, url, text)
        self._send_json({"ok": True, "doc": entry})

    def do_POST(self):
        path = self.path.split("?", 1)[0].strip("/")
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] in ("approve", "deny"):
            decision = "approved" if parts[1] == "approve" else "denied"
            ok = STATE.decide(parts[2], decision)
            self._send_json({"ok": ok})
        elif path == "api/library/file":
            self._lib_add_file()
        elif path == "api/library/url":
            self._lib_add_url()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "library":
            ok = library_delete(parts[2])
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
