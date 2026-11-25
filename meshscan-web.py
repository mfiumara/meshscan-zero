#!/usr/bin/env python3
"""meshscan-web: live dashboard + JSON API for meshscan (port 8080).

Viewing is open to the local network. Sending or switching modes needs a PIN session or the
owner-only local token over loopback; changing settings always needs the PIN session.

GET  /                      dashboard (web/index.html)
GET  /api/<x>               summary, nodes, hops, stats, packets, messages, status, settings, session
GET  /api/stream            Server-Sent Events: packet, message, status
POST /api/setup             first-run setup (only while unconfigured)
POST /api/login | logout    PIN session (HttpOnly cookie)
POST /api/send | mode       PIN session or local token; /api/settings needs a session
"""
import json, os, re, secrets, sqlite3, stat, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import meshscan_common as common  # noqa: E402

DB = os.environ.get("MESHSCAN_DB", os.path.join(common.STATE, "meshscan.db"))
STATUS = os.path.join(common.STATE, "status.json")
PORT = int(os.environ.get("MESHSCAN_WEB_PORT", "8080"))
MAX_TEXT = 140  # bytes; MeshCore group text also carries "<name>: " inside the same frame
INDEX = next(p for p in (os.path.join(HERE, "web", "index.html"), os.path.join(HERE, "index.html")) if os.path.exists(p))
SESSION_TTL = 30 * 86400
COOKIE = "meshscan_session"

SESSIONS = {}          # token -> expiry
FAILS = {}             # client ip -> [timestamps of failed PIN attempts]
LOCK = threading.Lock()
LOCAL_TOKEN = None       # Never exposed through the HTTP API.
LOCAL_HEADER = "X-Meshscan-Local-Token"


def init_local_token():
    """Create once (0600); reject links, other owners and world-readable files."""
    os.makedirs(common.CTL, exist_ok=True)
    path = os.path.join(common.CTL, "local.token")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        fd = None
    if fd is not None:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        # Group bits may be set: the package grants the desktop user read access with an ACL
        # (which shows up as the group mask). Nobody else may read or write it.
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o027:
            raise PermissionError("local token must be a regular file owned by this service, not world-readable")
        token = f.read(256).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token):
        raise ValueError("invalid local token file")
    return token


def valid_local_request(ip, headers, path):
    if path not in ("/api/send", "/api/mode", "/api/radio") or ip not in ("127.0.0.1", "::1"):
        return False
    # Browsers must use the PIN/session path, even when browsing on the device.
    if headers.get("Origin") or headers.get("X-Forwarded-For") or headers.get("Forwarded"):
        return False
    candidate = headers.get(LOCAL_HEADER, "")
    return bool(LOCAL_TOKEN and candidate and secrets.compare_digest(candidate.encode(), LOCAL_TOKEN.encode()))


def q(sql, args=()):
    if not os.path.exists(DB):  # fresh install: the radio service hasn't created it yet
        return []
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def one(sql, args=(), key="n", default=0):
    rows = q(sql, args)
    return rows[0][key] if rows and rows[0][key] is not None else default


def status():
    try:
        with open(STATUS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


PKT_COLS = "id, ts, config, proto, freq, sf, bw, rssi, snr, len, crc_ok, decoded"


def public_settings():
    s = common.load_settings()
    regions = {k: {"label": r["label"], "detail": r["detail"], "scopes": r["scopes"], "default_scope": r["default_scope"]}
               for k, r in common.REGIONS.items()}
    if not s:
        return {"configured": False, "legacy": os.path.exists(common.LEGACY), "regions": regions,
                "suggested_name": os.uname().nodename}
    return {"configured": True, "name": s["name"], "region": s["region"], "scope": s.get("scope", ""),
            "channels": [{"name": c["name"], "private": bool(c.get("psk"))} for c in s.get("channels", [])],
            "default_mode": s.get("default_mode", "scan"), "home": s.get("home"), "regions": regions}


def api(path, qs):
    hours = float(qs.get("hours", ["24"])[0]); since = time.time() - hours * 3600
    if path == "/api/nodes":
        return q("SELECT * FROM nodes WHERE node_id NOT LIKE 'hop:%' ORDER BY last_seen DESC")
    if path == "/api/hops":
        return q("SELECT substr(node_id,5) AS id, packets, last_rssi, last_snr, first_seen, last_seen FROM nodes "
                 "WHERE node_id LIKE 'hop:%' ORDER BY packets DESC LIMIT 40")
    if path == "/api/stats":
        return q("SELECT config, SUM(end-start) AS secs, SUM(packets) AS packets, SUM(crc_errors) AS crc, "
                 "SUM(preambles) AS preambles, AVG(noise_dbm) AS noise FROM dwells WHERE start>? GROUP BY config "
                 "ORDER BY packets DESC", (since,))
    if path == "/api/packets":
        return q(f"SELECT {PKT_COLS} FROM packets ORDER BY id DESC LIMIT 80")
    if path == "/api/messages":
        return q("SELECT * FROM messages ORDER BY ts DESC LIMIT 200")
    if path == "/api/status":
        return status()
    if path == "/api/settings":
        return public_settings()
    if path == "/api/summary":
        return {"n": one("SELECT COUNT(*) n FROM packets"), "last": one("SELECT MAX(ts) n FROM packets", default=None),
                "max_id": one("SELECT MAX(id) n FROM packets"),
                "nodes": one("SELECT COUNT(*) n FROM nodes WHERE node_id NOT LIKE 'hop:%'"),
                "relays": one("SELECT COUNT(*) n FROM nodes WHERE node_id LIKE 'hop:%'"),
                "hour": one("SELECT COUNT(*) n FROM packets WHERE ts>?", (time.time() - 3600,)),
                "messages": one("SELECT COUNT(*) n FROM messages")}
    return None


def write_ctl(name, data):
    os.makedirs(os.path.join(common.CTL, "outbox"), exist_ok=True)
    path = os.path.join(common.CTL, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- validation
NAME_RE = re.compile(r"^[^\x00-\x1f:]{1,24}$")      # ':' would break "<name>: text" on the air
SCOPE_RE = re.compile(r"^[a-z0-9-]{0,30}$")
PIN_RE = re.compile(r"^\d{4,8}$")


def clean_settings(body, current=None):
    """Validated settings from a setup/settings form, or raise ValueError with a user-facing reason."""
    s = dict(current or {})
    name = str(body.get("name", s.get("name", ""))).strip()
    if not NAME_RE.match(name):
        raise ValueError("Name: 1–24 characters, no ':'")
    region = body.get("region", s.get("region"))
    if region not in common.REGIONS:
        raise ValueError("Pick a region")
    scope = str(body.get("scope", s.get("scope", ""))).strip().lstrip("#").lower()
    if not SCOPE_RE.match(scope):
        raise ValueError("Region tag: lowercase letters, digits and '-' only")
    mode = body.get("default_mode", s.get("default_mode", "scan"))
    if mode not in ("chat", "scan"):
        raise ValueError("Default mode must be chat or scan")
    channels = []
    for ch in body.get("channels", s.get("channels", [])) or []:
        cname = str(ch.get("name", "")).strip()
        if not cname or len(cname) > 31 or cname.lower() == "public":
            raise ValueError(f"Channel name '{cname}' is not allowed")
        entry = {"name": cname}
        old = next((c for c in (current or {}).get("channels", []) if c["name"] == cname), None)
        if ch.get("psk"):
            import base64, binascii
            try:
                if len(base64.b64decode(ch["psk"], validate=True)) not in (16, 32):
                    raise ValueError
            except (ValueError, binascii.Error):
                raise ValueError(f"Key for '{cname}' must be base64 of 16 or 32 bytes") from None
            entry["psk"] = ch["psk"]
        elif ch.get("keep_psk") and old and old.get("psk"):  # unchanged private channel from the UI
            entry["psk"] = old["psk"]
        elif not cname.startswith("#"):
            raise ValueError(f"'{cname}': hashtag channels start with '#', private ones need a key")
        channels.append(entry)
    if len(channels) > 7:
        raise ValueError("At most 7 extra channels")
    home = body.get("home", s.get("home"))
    if home:
        try:
            home = {"lat": round(float(home["lat"]), 5), "lon": round(float(home["lon"]), 5)}
            if not (-90 <= home["lat"] <= 90 and -180 <= home["lon"] <= 180):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("Home location must be latitude, longitude") from None
    s.update(version=1, name=name, region=region, scope=scope, default_mode=mode, channels=channels, home=home or None)
    return s


# ---------------------------------------------------------------- sessions
def new_session():
    token = secrets.token_urlsafe(32)
    with LOCK:
        now = time.time()
        for t in [t for t, exp in SESSIONS.items() if exp < now]:
            del SESSIONS[t]
        SESSIONS[token] = now + SESSION_TTL
    return token


def valid_session(token):
    with LOCK:
        return bool(token) and SESSIONS.get(token, 0) > time.time()


def throttled(ip):
    with LOCK:
        recent = [t for t in FAILS.get(ip, []) if t > time.time() - 300]
        FAILS[ip] = recent
        return len(recent) >= 5


def note_fail(ip):
    with LOCK:
        FAILS.setdefault(ip, []).append(time.time())


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def send(self, code, body, ctype, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers(); self.wfile.write(body)

    def json(self, code, obj, cookie=None):
        self.send(code, json.dumps(obj).encode(), "application/json", cookie)

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    @staticmethod
    def cookie(token, max_age=SESSION_TTL):
        return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"

    def stream(self, qs):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive"); self.end_headers()
        last = int(qs.get("since", ["0"])[0]) or one("SELECT MAX(id) n FROM packets")
        last_msg = one("SELECT MAX(updated) n FROM messages")
        last_status = 0; last_beat = time.time()
        try:
            while True:
                for r in q(f"SELECT {PKT_COLS} FROM packets WHERE id>? ORDER BY id LIMIT 50", (last,)):
                    last = r["id"]
                    self.wfile.write(f"event: packet\ndata: {json.dumps(r)}\n\n".encode())
                for m in q("SELECT * FROM messages WHERE updated>? ORDER BY updated LIMIT 50", (last_msg,)):
                    last_msg = max(last_msg, m["updated"] or 0)
                    self.wfile.write(f"event: message\ndata: {json.dumps(m)}\n\n".encode())
                st = status()
                if st.get("ts", 0) != last_status:
                    last_status = st.get("ts", 0)
                    self.wfile.write(f"event: status\ndata: {json.dumps(st)}\n\n".encode())
                if time.time() - last_beat > 15:
                    self.wfile.write(b": beat\n\n"); last_beat = time.time()
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.json(415, {"error": "JSON only"})  # also blocks simple cross-site form posts
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8192:
                return self.json(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            self.route_post(path, body)
        except ValueError as e:
            self.json(400, {"error": str(e)})
        except OSError as e:
            self.json(500, {"error": str(e)})

    def route_post(self, path, body):
        ip = self.client_address[0]
        settings = common.load_settings()
        if path == "/api/setup":
            if settings:
                return self.json(409, {"error": "already set up; change things in Settings"})
            pin = str(body.get("pin", ""))
            if not PIN_RE.match(pin):
                raise ValueError("PIN: 4–8 digits")
            s = clean_settings(body)
            s["pin"] = common.hash_pin(pin)
            common.save_settings(s)
            write_ctl("mode", s["default_mode"])
            return self.json(200, {"ok": True}, self.cookie(new_session()))
        if path == "/api/login":
            if not settings:
                return self.json(409, {"error": "finish setup first"})
            if throttled(ip):
                return self.json(429, {"error": "Too many wrong PINs. Try again in 5 minutes."})
            if not common.check_pin(settings.get("pin"), str(body.get("pin", ""))):
                note_fail(ip)
                return self.json(401, {"error": "Wrong PIN"})
            return self.json(200, {"ok": True}, self.cookie(new_session()))
        if path == "/api/logout":
            with LOCK:
                SESSIONS.pop(self.token(), None)
            return self.json(200, {"ok": True}, self.cookie("", 0))
        # everything below transmits or changes the node
        if not settings:
            return self.json(409, {"error": "finish setup first"})
        if not valid_session(self.token()) and not valid_local_request(ip, self.headers, path):
            return self.json(401, {"error": "locked"})
        if path == "/api/radio":
            on = body.get("on")
            if not isinstance(on, bool):
                raise ValueError("on must be a boolean")
            write_ctl("radio", "on" if on else "off")
            return self.json(200, {"ok": True, "on": on})
        if path == "/api/mode":
            mode = body.get("mode")
            if mode not in ("chat", "scan"):
                raise ValueError("mode must be chat or scan")
            write_ctl("mode", mode)
            return self.json(200, {"ok": True, "mode": mode})
        if path == "/api/send":
            if not common.radio_on() or status().get("radio", "on") != "on":
                return self.json(409, {"error": "radio is unavailable"})
            text = str(body.get("text", "")).strip()
            if not text:
                raise ValueError("empty message")
            if len(text.encode()) > MAX_TEXT:
                raise ValueError(f"message is longer than {MAX_TEXT} bytes")
            if status().get("mode") != "chat":
                return self.json(409, {"error": "radio is in scan mode"})
            job = {"text": text, "channel": int(body.get("channel", 0))}
            write_ctl(os.path.join("outbox", f"{time.time_ns()}.json"), json.dumps(job))
            return self.json(202, {"ok": True})
        if path == "/api/settings":
            s = clean_settings(body, settings)
            new_pin = str(body.get("new_pin", "") or "")
            if new_pin:
                if not PIN_RE.match(new_pin):
                    raise ValueError("New PIN: 4–8 digits")
                s["pin"] = common.hash_pin(new_pin)
            common.save_settings(s)  # the radio service sees the new mtime and restarts itself
            return self.json(200, {"ok": True})
        self.json(404, {"error": "not found"})

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/stream":
                return self.stream(qs)
            if u.path == "/api/session":
                return self.json(200, {"unlocked": valid_session(self.token())})
            data = api(u.path, qs)
            if data is None:
                return self.json(404, {"error": "not found"})
            self.json(200, data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.json(500, {"error": str(e)})


class Server(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    LOCAL_TOKEN = init_local_token()
    Server(("0.0.0.0", PORT), H).serve_forever()
