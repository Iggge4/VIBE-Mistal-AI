#!/usr/bin/env python3
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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
AUTO_FILE = os.environ.get(
    "VIBE_AUTO_FILE",
    os.path.join(os.path.dirname(os.path.abspath(STATE_FILE)), "autoapprove.json"),
)
LIB_DIR = os.environ.get(
    "VIBE_LIB_DIR",
    os.path.join(os.path.dirname(os.path.abspath(WEB_DIR)), "data", "library"),
)
MAX_CONTEXT_CHARS = int(os.environ.get("VIBE_MAX_CONTEXT_CHARS", "16000"))
CHUNK_CHARS = 900
MAX_CHUNKS = 16
MAX_DOC_CHARS = 2_000_000
MAX_PDF_BYTES = 20_000_000
PDFTOTEXT = os.environ.get("VIBE_PDFTOTEXT") or shutil.which("pdftotext")

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
        self.stats = {"total": 0, "approved": 0, "denied": 0, "timeout": 0, "auto": 0, "by_ip": {}}
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

    def register(self, ip, endpoint, size, body_sha, name=""):
        rid = hashlib.sha256(
            ("%f|%s|%s" % (time.time(), ip, endpoint)).encode()
        ).hexdigest()[:12]
        entry = {
            "id": rid,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": ip,
            "name": name,
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
                    "name": e["name"],
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


def _doc_text(doc_id):
    try:
        with open(os.path.join(LIB_DIR, doc_id + ".txt")) as fh:
            return fh.read()
    except OSError:
        return ""


def library_list():
    with _lib_lock:
        docs = _load_lib_meta()
    out = []
    for d in docs:
        d = dict(d)
        d["preview"] = _doc_text(d["id"])[:200]
        out.append(d)
    return out


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


def pdf_to_text(data):
    if not PDFTOTEXT:
        raise RuntimeError("pdftotext saknas i systemet")
    try:
        proc = subprocess.run(
            [PDFTOTEXT, "-", "-"],
            input=data,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("PDF-konverteringen tog för lång tid")
    if proc.returncode != 0:
        raise RuntimeError("pdftotext fel: %s" % proc.stderr.decode(errors="replace")[:200])
    return proc.stdout.decode("utf-8", errors="replace")


def _query_terms(text):
    return list(dict.fromkeys(re.findall(r"[a-zåäöéü0-9]{2,}", text.lower())))


def _split_chunks(text, size=CHUNK_CHARS):
    chunks = []
    cur = ""
    for para in text.split("\n"):
        if cur and len(cur) + len(para) + 1 > size:
            chunks.append(cur.strip())
            cur = para
        else:
            cur = (cur + "\n" + para) if cur else para
        while len(cur) > size * 2:
            chunks.append(cur[:size].strip())
            cur = cur[size:]
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def _select_chunks(docs, terms):
    all_chunks = []
    for meta, text in docs:
        for idx, chunk in enumerate(_split_chunks(text)):
            all_chunks.append((meta, idx, chunk))
    if not all_chunks:
        return [], False
    lows = [c.lower() for _, _, c in all_chunks]
    n = len(lows)
    scored = []
    for i, low in enumerate(lows):
        score = 0.0
        for term in terms:
            cnt = low.count(term)
            if cnt:
                df = sum(1 for l in lows if term in l)
                score += cnt * (1.0 + n / (1.0 + df))
        if score:
            scored.append((score, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked, used, truncated = [], 0, False
    for _, i in scored:
        meta, idx, chunk = all_chunks[i]
        if used + len(chunk) > MAX_CONTEXT_CHARS:
            truncated = True
            continue
        picked.append((meta, idx, chunk))
        used += len(chunk) + 40
        if len(picked) >= MAX_CHUNKS:
            truncated = len(scored) > len(picked)
            break
    picked.sort(key=lambda x: (x[0]["id"], x[1]))
    return [(meta, chunk) for meta, _, chunk in picked], truncated


_lib_lock = threading.Lock()


def auto_list():
    try:
        with open(AUTO_FILE) as fh:
            return json.load(fh)
    except Exception:
        return []


def _save_auto(names):
    os.makedirs(os.path.dirname(_auto_path()) or ".", exist_ok=True)
    tmp = _auto_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(names, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _auto_path())


def _auto_path():
    return AUTO_FILE


def auto_add(name):
    with _lib_lock:
        names = auto_list()
        if name not in names:
            names.append(name)
            _save_auto(names)
        return names


def auto_remove(name):
    with _lib_lock:
        names = auto_list()
        if name in names:
            names.remove(name)
            _save_auto(names)
            return True
        return False


def auto_check(name):
    if not name:
        return False
    return name.strip().lower() in (n.strip().lower() for n in auto_list())


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

    def _await_approval(self, ip, endpoint, size, body_sha, name=""):
        rid = STATE.register(ip, endpoint, size, body_sha, name)
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
        payload = json.dumps(data).encode("utf-8")
        if not isinstance(ids, list) or not ids:
            return payload
        docs = library_get(ids)
        if not docs:
            return payload

        question = ""
        messages = data.get("messages")
        if isinstance(messages, list):
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    question = str(msg.get("content", ""))
                    break
        terms = _query_terms(question)
        selected, truncated = _select_chunks(docs, terms)
        sys.stderr.write(
            "[gateway] bibliotek: %d dokument valda, %d utdrag skickade (%d tecken)%s\n"
            % (len(docs), len(selected), sum(len(c) for _, c in selected),
               " (avkortat)" if truncated else "")
        )

        by_doc = {}
        for meta, chunk in selected:
            by_doc.setdefault(meta["id"], (meta, []))[1].append(chunk)
        parts = []
        for meta, chunks in by_doc.values():
            parts.append(
                "Dokument: %s (källa: %s)\n%s"
                % (meta["name"], meta["source"], "\n[…]\n".join(chunks))
            )
        context = "\n\n".join(parts)
        if truncated:
            context += "\n\n[Det fanns fler träffar än som fick plats — tidigaste utdrag visas.]"
        if not selected:
            context += "\n\n[Inga sökträffar för frågans ord hittades i dokumenten.]"
        missing = len(ids) - len(docs)
        note = " (%d valda dokument hittades inte)" % missing if missing > 0 else ""
        names = ", ".join(m["name"] for m, _ in docs)
        system_msg = {
            "role": "system",
            "content": (
                "Nedan följer de mest relevanta utdragen ur användarens "
                "biblioteksdokument (%s)%s, valda automatiskt utifrån frågan. "
                "Läs utdragen nögrant — svaret finns oftast i dem. Använd dem "
                "som kunskapskälla och citera gärna exakta formuleringar. Säg "
                "bara att du inte vet om du verkligen inte hittar det. Svara "
                "på användarens språk.\n\n%s"
                % (names, note, context)
            ),
        }
        if isinstance(messages, list):
            merged = []
            for msg in messages:
                if (
                    merged
                    and isinstance(msg, dict)
                    and msg.get("role") == "system"
                    and merged[-1].get("role") == "system"
                ):
                    merged[-1] = dict(merged[-1])
                    merged[-1]["content"] = (
                        str(merged[-1].get("content", ""))
                        + "\n\n"
                        + str(msg.get("content", ""))
                    )
                else:
                    merged.append(msg)
            if merged and isinstance(merged[0], dict) and merged[0].get("role") == "system":
                merged[0] = dict(merged[0])
                merged[0]["content"] = (
                    str(merged[0].get("content", ""))
                    + "\n\n"
                    + system_msg["content"]
                )
            else:
                merged.insert(0, system_msg)
            data["messages"] = merged
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

        client_name = ""
        if self.command == "POST" and body:
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    client_name = str(data.pop("client_name", "") or "")[:60]
                    body = json.dumps(data).encode("utf-8")
            except Exception:
                pass
            body = self._apply_library(body)

        if self.command == "POST" and REQUIRE_APPROVAL:
            client_ip = self.client_address[0]
            if auto_check(client_name):
                with STATE.lock:
                    STATE.stats["approved"] += 1
                    STATE.stats["auto"] += 1
                    STATE.save()
                sys.stderr.write(
                    "[gateway] auto-godk\u00e4nd: %s %s\n" % (client_name, self.path)
                )
            else:
                body_sha = hashlib.sha256(body).hexdigest()[:16] if body else "-"
                if not self._await_approval(
                    client_ip, self.path, len(body), body_sha, client_name
                ):
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
        elif path == "/api/autoapprove":
            self._send_json(auto_list())
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
        content = str(data["content"])
        if content.startswith("%PDF") or "\x00" in content[:4096]:
            self._send_json(
                {"error": "binärfil kan inte läsas som text — PDF:er hanteras automatiskt, övriga spara som .txt"},
                400,
            )
            return
        if len(content) > MAX_DOC_CHARS:
            self._send_json({"error": "för stor fil (max 2 MB text)"}, 400)
            return
        entry = library_add(str(data["name"]), "fil", content)
        self._send_json({"ok": True, "doc": entry})

    def _lib_add_pdf(self):
        data = self._read_json_body()
        if not data or not data.get("name") or not data.get("data"):
            self._send_json({"error": "namn och fildata krävs"}, 400)
            return
        try:
            raw = base64.b64decode(data["data"], validate=True)
        except Exception:
            self._send_json({"error": "ogiltig fildata"}, 400)
            return
        if not raw.startswith(b"%PDF"):
            self._send_json({"error": "filen är ingen giltig PDF"}, 400)
            return
        if len(raw) > MAX_PDF_BYTES:
            self._send_json({"error": "för stor PDF (max 20 MB)"}, 400)
            return
        try:
            text = pdf_to_text(raw)
        except Exception as exc:
            self._send_json({"error": "kunde inte läsa PDF:en: %s" % exc}, 400)
            return
        if not text.strip():
            self._send_json(
                {"error": "ingen text kunde extraheras — kan vara en skannad bild-PDF (OCR stöds inte)"},
                400,
            )
            return
        entry = library_add(str(data["name"]), "PDF", text)
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
        elif path == "api/library/pdf":
            self._lib_add_pdf()
        elif path == "api/library/url":
            self._lib_add_url()
        elif path == "api/library/test":
            data = self._read_json_body()
            question = str((data or {}).get("question", "")) if data else ""
            docs = [(d, _doc_text(d["id"])) for d in library_list()]
            terms = _query_terms(question)
            selected, truncated = _select_chunks(docs, terms)
            self._send_json(
                {
                    "terms": terms,
                    "truncated": truncated,
                    "chunks": [
                        {"doc": meta["name"], "text": chunk[:600]}
                        for meta, chunk in selected
                    ],
                }
            )
        elif path == "api/autoapprove":
            data = self._read_json_body()
            name = (data or {}).get("name", "").strip() if data else ""
            if not name:
                self._send_json({"error": "namn kr\u00e4vs"}, 400)
                return
            names = auto_add(name)
            self._send_json({"ok": True, "users": names})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "library":
            ok = library_delete(parts[2])
            self._send_json({"ok": ok})
        elif len(parts) == 3 and parts[0] == "api" and parts[1] == "autoapprove":
            ok = auto_remove(urllib.parse.unquote(parts[2]))
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
