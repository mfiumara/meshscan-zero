#!/usr/bin/env python3
"""meshscan-web: read-only live dashboard for meshscan (port 8080).

GET /                 dashboard (web/index.html)
GET /api/<x>          JSON snapshots: summary, nodes, hops, stats, packets, messages, status
GET /api/stream       Server-Sent Events: packet, status
"""
import json, os, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB = os.environ.get("MESHSCAN_DB", "/var/lib/meshscan/meshscan.db")
STATUS = os.path.join(os.path.dirname(DB), "status.json")
PORT = int(os.environ.get("MESHSCAN_WEB_PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = next(p for p in (os.path.join(HERE, "web", "index.html"), os.path.join(HERE, "index.html")) if os.path.exists(p))


def q(sql, args=()):
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.close()


def status():
    try:
        with open(STATUS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


PKT_COLS = "id, ts, config, proto, freq, sf, bw, rssi, snr, len, crc_ok, decoded"


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
        return q("SELECT * FROM messages ORDER BY ts DESC LIMIT 50")
    if path == "/api/status":
        return status()
    if path == "/api/summary":
        r = q("SELECT COUNT(*) n, MIN(ts) first, MAX(ts) last, MAX(id) max_id FROM packets")[0]
        r["nodes"] = q("SELECT COUNT(*) n FROM nodes WHERE node_id NOT LIKE 'hop:%'")[0]["n"]
        r["relays"] = q("SELECT COUNT(*) n FROM nodes WHERE node_id LIKE 'hop:%'")[0]["n"]
        r["hour"] = q("SELECT COUNT(*) n FROM packets WHERE ts>?", (time.time() - 3600,))[0]["n"]
        return r
    return None


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def stream(self, qs):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive"); self.end_headers()
        last = int(qs.get("since", ["0"])[0]) or (q("SELECT COALESCE(MAX(id),0) m FROM packets")[0]["m"])
        last_status = 0; last_beat = time.time()
        try:
            while True:
                rows = q(f"SELECT {PKT_COLS} FROM packets WHERE id>? ORDER BY id LIMIT 50", (last,))
                for r in rows:
                    last = r["id"]
                    self.wfile.write(f"event: packet\ndata: {json.dumps(r)}\n\n".encode())
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

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/stream":
                return self.stream(qs)
            data = api(u.path, qs)
            if data is None:
                return self.send(404, b'{"error":"not found"}', "application/json")
            self.send(200, json.dumps(data).encode(), "application/json")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send(500, json.dumps({"error": str(e)}).encode(), "application/json")


class Server(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    Server(("0.0.0.0", PORT), H).serve_forever()
