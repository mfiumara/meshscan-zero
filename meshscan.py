#!/usr/bin/env python3
"""meshscan: LoRa radio service for CardputerZero + Cap LoRa-1262 (SX1262).

Scan mode hops through LoRa presets (MeshCore, Meshtastic, LoRaWAN) and never transmits.
Chat mode (meshscan_chat.py) runs a MeshCore node that only transmits what you send from
the dashboard. Both decode every packet into SQLite (Meshtastic default channel, MeshCore
adverts and Public channel) and keep a node database. Until the first-run setup is done
the service stays in Scan mode.
"""
import base64, hashlib, hmac, json, os, signal, sqlite3, struct, sys, time
from pathlib import Path

import gpiod, smbus2, spidev
from gpiod.line import Direction, Value

DB = os.environ.get("MESHSCAN_DB", "/var/lib/meshscan/meshscan.db")

import meshscan_common as common

CONFIG_LABELS = common.CONFIG_LABELS
BW_CODE = {7.8: 0x00, 10.4: 0x08, 15.6: 0x01, 20.8: 0x09, 31.25: 0x02, 41.7: 0x0A, 62.5: 0x03, 125: 0x04, 250: 0x05, 500: 0x06}

# Meshtastic default channel key ("AQ==") and MeshCore Public channel key
MT_DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
MC_PUBLIC_KEY = base64.b64decode("izOH6cXN6mrJ5e26oRXNcg==")

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None
import meshscan_pb as pb


# ---------------------------------------------------------------- radio
class SX1262:
    PIN_BUSY, PIN_RST = 22, 26

    def __init__(self):
        self._power_on()
        self.gpio = gpiod.request_lines("/dev/gpiochip0", consumer="meshscan", config={
            self.PIN_RST: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE),
            self.PIN_BUSY: gpiod.LineSettings(direction=Direction.INPUT)})
        self.spi = spidev.SpiDev(); self.spi.open(0, 1); self.spi.max_speed_hz = 1_000_000; self.spi.mode = 0

    @staticmethod
    def _power_on():
        open("/sys/class/leds/ext_5v_out/brightness", "w").write("1")
        time.sleep(0.3)
        b = smbus2.SMBus(1)
        for _ in range(20):
            try:
                d = b.read_byte_data(0x43, 0x03); break
            except OSError:
                time.sleep(0.2)
        o = b.read_byte_data(0x43, 0x05); hz = b.read_byte_data(0x43, 0x07)
        b.write_byte_data(0x43, 0x05, o | 1); b.write_byte_data(0x43, 0x03, d | 1); b.write_byte_data(0x43, 0x07, hz & ~1)
        time.sleep(0.1)

    def _wait(self):
        t = time.time()
        while self.gpio.get_value(self.PIN_BUSY) == Value.ACTIVE:
            if time.time() - t > 2:
                raise RuntimeError("SX1262 BUSY stuck")
            time.sleep(0.0005)

    def cmd(self, *a):
        self._wait(); return self.spi.xfer2(list(a))

    def wreg(self, addr, *v): self.cmd(0x0D, addr >> 8, addr & 0xFF, *v)

    def rreg(self, addr): return self.cmd(0x1D, addr >> 8, addr & 0xFF, 0, 0)[4]

    def configure(self, c):
        self.gpio.set_value(self.PIN_RST, Value.INACTIVE); time.sleep(0.01)
        self.gpio.set_value(self.PIN_RST, Value.ACTIVE); time.sleep(0.01)
        self.cmd(0x80, 0x00)                       # standby RC
        self.cmd(0x97, 0x06, 0x00, 0x01, 0x40)     # DIO3 TCXO 3.0V, 5 ms
        self.cmd(0x89, 0x7F); time.sleep(0.01)     # calibrate all
        self.cmd(0x80, 0x00)
        self.cmd(0x9D, 0x01)                       # DIO2 as RF switch
        self.cmd(0x96, 0x01)                       # DC-DC
        self.cmd(0x8A, 0x01)                       # packet type LoRa
        # image calibration for the band in use: 863-870 MHz or 902-928 MHz
        self.cmd(0x98, *((0xE1, 0xE9) if c["freq"] > 900 else (0xD7, 0xDB))); time.sleep(0.01)
        f = int(c["freq"] * 1e6 * (1 << 25) / 32e6)
        self.cmd(0x86, (f >> 24) & 255, (f >> 16) & 255, (f >> 8) & 255, f & 255)
        self.cmd(0x8F, 0, 0)                       # buffer base
        bw_hz = c["bw"] * 1e3
        ldro = 1 if (2 ** c["sf"]) / bw_hz > 0.016 else 0
        self.cmd(0x8B, c["sf"], BW_CODE[c["bw"]], 0x01, ldro)
        self.cmd(0x8C, 0x00, 0x10, 0x00, 0xFF, 0x01, 0x00)  # preamble 16, explicit hdr, CRC on, std IQ
        s = c["sync"]
        self.wreg(0x0740, ((s >> 4) << 4) | 4, ((s & 15) << 4) | 4)
        self.wreg(0x08AC, 0x96)                    # boosted RX gain
        self.wreg(0x08B5, self.rreg(0x08B5) | 0x01)  # RX sensitivity patch (as Meshtastic)
        self.cmd(0x08, 0xFF, 0xFF, 0x00, 0x00, 0, 0, 0, 0)  # all IRQs, no DIO routing (we poll)
        self.cmd(0x02, 0xFF, 0xFF)
        self.cmd(0x82, 0xFF, 0xFF, 0xFF)           # continuous RX

    def irq(self):
        r = self.cmd(0x12, 0, 0, 0); return (r[2] << 8) | r[3]

    def clear(self, m=0xFFFF): self.cmd(0x02, m >> 8, m & 0xFF)

    def read_packet(self):
        bs = self.cmd(0x13, 0, 0, 0); ln, st = bs[2], bs[3]
        data = bytes(self.cmd(0x1E, st, 0, *([0] * ln))[3:]) if ln else b""
        ps = self.cmd(0x14, 0, 0, 0, 0)
        rssi = -ps[2] / 2; snr = (ps[3] - 256 if ps[3] > 127 else ps[3]) / 4
        return data, rssi, snr

    def rssi_inst(self): return -self.cmd(0x15, 0, 0)[2] / 2

    def sleep(self):
        try: self.cmd(0x80, 0x00)
        except Exception: pass


# ---------------------------------------------------------------- decoders
def aes_ctr(key, nonce, data):
    d = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor(); return d.update(data) + d.finalize()


def decode_meshtastic(p):
    if len(p) < 16:
        return None
    dst, src, pid = struct.unpack_from("<III", p, 0)
    flags, chash, next_hop, relay = p[12], p[13], p[14], p[15]
    out = {"src": f"!{src:08x}", "dst": "^all" if dst == 0xFFFFFFFF else f"!{dst:08x}", "id": pid,
           "hop_limit": flags & 7, "hop_start": flags >> 5, "want_ack": bool(flags & 8),
           "via_mqtt": bool(flags & 16), "channel_hash": chash, "relay": relay}
    if Cipher is None:
        return out
    try:
        nonce = struct.pack("<QI", pid, src) + b"\0\0\0\0"
        data = pb.mt_data(aes_ctr(MT_DEFAULT_KEY, nonce, p[16:]))
        port = data["portnum"]
        if port == 0 or port not in pb.PORTNUM:
            raise ValueError
        out["decrypted"] = True
        out["port"] = pb.PORTNUM[port]
        pl = data["payload"]
        if port == 1:    # TEXT_MESSAGE_APP
            out["text"] = pl.decode("utf-8", "replace")
        elif port == 4:  # NODEINFO_APP
            u = pb.mt_user(pl)
            out["user"] = {"long_name": u["long_name"], "short_name": u["short_name"],
                           "hw": pb.HARDWARE.get(u["hw_model"]) if u["hw_model"] else None, "role": u["role"]}
        elif port == 3:  # POSITION_APP
            pos = pb.mt_position(pl)
            if pos["latitude_i"] or pos["longitude_i"]:
                out["pos"] = {"lat": pos["latitude_i"] / 1e7, "lon": pos["longitude_i"] / 1e7, "alt": pos["altitude"]}
        elif port == 67:  # TELEMETRY_APP
            dm = pb.mt_device_metrics(pl)
            if dm:
                out["telemetry"] = {"battery": dm["battery_level"], "voltage": round(dm["voltage"], 2),
                                    "ch_util": round(dm["channel_utilization"], 1), "air_util": round(dm["air_util_tx"], 1)}
    except Exception:
        out["decrypted"] = False
    return out


MC_ROUTE = {0: "TRANSPORT_FLOOD", 1: "FLOOD", 2: "DIRECT", 3: "TRANSPORT_DIRECT"}
MC_TYPE = {0: "REQ", 1: "RESPONSE", 2: "TXT_MSG", 3: "ACK", 4: "ADVERT", 5: "GRP_TXT", 6: "GRP_DATA",
           7: "ANON_REQ", 8: "PATH", 9: "TRACE", 10: "MULTIPART", 11: "CONTROL", 15: "RAW_CUSTOM"}
MC_NODE_TYPE = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}


def decode_meshcore(p):
    if len(p) < 2:
        return None
    h = p[0]; route = h & 3; ptype = (h >> 2) & 15; ver = h >> 6
    i = 1
    out = {"route": MC_ROUTE[route], "type": MC_TYPE.get(ptype, ptype), "ver": ver}
    if route in (0, 3):
        out["transport"] = p[i:i + 4].hex(); i += 4
    pl_byte = p[i]; i += 1
    hash_size = (pl_byte >> 6) + 1; hops = pl_byte & 0x3F
    path = p[i:i + hops * hash_size]; i += hops * hash_size
    out["hops"] = hops; out["path"] = [path[k:k + hash_size].hex() for k in range(0, len(path), hash_size)]
    pl = p[i:]
    if ptype == 4 and len(pl) >= 101:  # ADVERT
        pub = pl[:32]; ts = struct.unpack_from("<I", pl, 32)[0]; app = pl[100:]
        out["pubkey"] = pub.hex(); out["ts"] = ts
        if app:
            fl = app[0]; j = 1
            out["node_type"] = MC_NODE_TYPE.get(fl & 15, fl & 15)
            if fl & 0x10 and len(app) >= j + 8:
                lat, lon = struct.unpack_from("<ii", app, j); j += 8
                if lat or lon:
                    out["pos"] = {"lat": lat / 1e6, "lon": lon / 1e6}
            if fl & 0x20: j += 2
            if fl & 0x40: j += 2
            if fl & 0x80:
                out["name"] = app[j:].decode("utf-8", "replace").rstrip("\0")
    elif ptype == 5 and len(pl) >= 3 and Cipher is not None:  # GRP_TXT
        ch = pl[0]; mac = pl[1:3]; ct = pl[3:]
        out["channel_hash"] = ch
        if ch == hashlib.sha256(MC_PUBLIC_KEY).digest()[0] and len(ct) % 16 == 0 and ct:
            secret = MC_PUBLIC_KEY + b"\0" * 16
            if hmac.new(secret, ct, hashlib.sha256).digest()[:2] == mac:
                d = Cipher(algorithms.AES(MC_PUBLIC_KEY), modes.ECB()).decryptor()
                pt = d.update(ct) + d.finalize()
                out["channel"] = "Public"; out["ts"] = struct.unpack_from("<I", pt, 0)[0]
                out["text"] = pt[5:].split(b"\0", 1)[0].decode("utf-8", "replace")
    return out


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS packets(id INTEGER PRIMARY KEY, ts REAL, config TEXT, proto TEXT, freq REAL, sf INT, bw REAL,
  rssi REAL, snr REAL, len INT, crc_ok INT, raw TEXT, decoded TEXT);
CREATE TABLE IF NOT EXISTS nodes(proto TEXT, node_id TEXT, name TEXT, short_name TEXT, kind TEXT, hw TEXT,
  lat REAL, lon REAL, first_seen REAL, last_seen REAL, last_rssi REAL, last_snr REAL, packets INT DEFAULT 0,
  PRIMARY KEY(proto, node_id));
CREATE TABLE IF NOT EXISTS dwells(id INTEGER PRIMARY KEY, config TEXT, start REAL, end REAL, packets INT,
  crc_errors INT, header_errors INT, preambles INT, headers INT, noise_dbm REAL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts REAL, proto TEXT, channel TEXT, src TEXT, text TEXT);
"""


def upsert_node(db, proto, node_id, now, rssi, snr, **f):
    db.execute("INSERT INTO nodes(proto,node_id,first_seen,last_seen,last_rssi,last_snr,packets) VALUES(?,?,?,?,?,?,1) "
               "ON CONFLICT(proto,node_id) DO UPDATE SET last_seen=excluded.last_seen,last_rssi=excluded.last_rssi,"
               "last_snr=excluded.last_snr,packets=packets+1", (proto, node_id, now, now, rssi, snr))
    for k, v in f.items():
        if v not in (None, ""):
            db.execute(f"UPDATE nodes SET {k}=? WHERE proto=? AND node_id=?", (v, proto, node_id))


MSG_COLUMNS = {"direction": "TEXT DEFAULT 'in'", "status": "TEXT", "repeats": "INT DEFAULT 0", "sender_ts": "INT",
               "rssi": "REAL", "snr": "REAL", "hops": "INT", "updated": "REAL"}


def migrate(db):
    have = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    for col, decl in MSG_COLUMNS.items():
        if col not in have:
            db.execute(f"ALTER TABLE messages ADD COLUMN {col} {decl}")
    if "sender_ts" not in have:  # one-time cleanup: older versions stored one row per repeat heard
        db.execute("DELETE FROM messages WHERE id IN (SELECT b.id FROM messages a JOIN messages b ON a.text=b.text "
                   "AND IFNULL(a.channel,'')=IFNULL(b.channel,'') AND a.id<b.id AND b.ts-a.ts<600)")
        db.execute("UPDATE messages SET updated=ts WHERE updated IS NULL")
    # older versions kept "Name: text" in one field
    db.execute("UPDATE messages SET src=substr(text,1,instr(text,': ')-1), text=substr(text,instr(text,': ')+2) "
               "WHERE src IS NULL AND instr(text,': ') BETWEEN 2 AND 48")
    db.commit()


def record_message(db, proto, channel, src, text, sender_ts=None, rssi=None, snr=None, hops=None):
    """Insert a received message once; further copies (repeats) only bump its counter."""
    if src is None and text and ": " in text[:48]:
        src, text = text.split(": ", 1)
    now = time.time()
    if sender_ts is not None:
        row = db.execute("SELECT id FROM messages WHERE proto=? AND IFNULL(channel,'')=IFNULL(?,'') AND IFNULL(src,'')=IFNULL(?,'') "
                         "AND text=? AND sender_ts=?", (proto, channel, src, text, sender_ts)).fetchone()
    else:
        row = db.execute("SELECT id FROM messages WHERE proto=? AND text=? AND IFNULL(src,'')=IFNULL(?,'') AND ts>?",
                         (proto, text, src, now - 600)).fetchone()
    if row:
        db.execute("UPDATE messages SET repeats=repeats+1 WHERE id=?", (row[0],))
    else:
        db.execute("INSERT INTO messages(ts,proto,channel,src,text,direction,repeats,sender_ts,rssi,snr,hops,updated) "
                   "VALUES(?,?,?,?,?,'in',0,?,?,?,?,?)", (now, proto, channel, src, text, sender_ts, rssi, snr, hops, now))
    db.commit()


def store(db, c, data, rssi, snr, crc_ok, messages=True):
    now = time.time(); dec = None
    try:
        if crc_ok and c["proto"] == "meshtastic":
            dec = decode_meshtastic(data)
        elif crc_ok and c["proto"] == "meshcore":
            dec = decode_meshcore(data)
    except Exception as e:
        dec = {"decode_error": str(e)}
    db.execute("INSERT INTO packets(ts,config,proto,freq,sf,bw,rssi,snr,len,crc_ok,raw,decoded) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (now, c["name"], c["proto"], c["freq"], c["sf"], c["bw"], rssi, snr, len(data), int(crc_ok), data.hex(),
                json.dumps(dec) if dec else None))
    if dec and c["proto"] == "meshtastic" and "src" in dec:
        u = dec.get("user") or {}; pos = dec.get("pos") or {}
        upsert_node(db, "meshtastic", dec["src"], now, rssi, snr, name=u.get("long_name"), short_name=u.get("short_name"),
                    hw=u.get("hw"), lat=pos.get("lat"), lon=pos.get("lon"))
        if dec.get("text") and messages:
            record_message(db, "meshtastic", "LongFast", dec["src"], dec["text"], rssi=rssi, snr=snr)
    if dec and c["proto"] == "meshcore":
        if dec.get("pubkey"):
            pos = dec.get("pos") or {}
            upsert_node(db, "meshcore", dec["pubkey"][:16], now, rssi, snr, name=dec.get("name"),
                        kind=dec.get("node_type"), lat=pos.get("lat"), lon=pos.get("lon"))
        if dec.get("text") and messages:
            record_message(db, "meshcore", dec.get("channel"), None, dec["text"], sender_ts=dec.get("ts"),
                           rssi=rssi, snr=snr, hops=dec.get("hops"))
        # last relay hop = a repeater we heard directly
        if dec.get("path") and dec["route"] in ("FLOOD", "TRANSPORT_FLOOD"):
            upsert_node(db, "meshcore", "hop:" + dec["path"][-1], now, rssi, snr, kind="repeater?")
    db.commit()
    return dec


# ---------------------------------------------------------------- main loop
RUNNING = True
STATUS = os.path.join(os.path.dirname(DB), "status.json")
CTL = Path(os.path.dirname(DB)) / "ctl"
NOISE_HIST = []  # (ts, dBm, config) for the dashboard sparkline


def write_status_raw(d):
    try:
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), **d}, f)
        os.chmod(tmp, 0o644); os.replace(tmp, STATUS)
    except OSError:
        pass


def write_status(c, start, end, st, noise):
    write_status_raw({"mode": "scan", "radio": "on", "config": c["name"], "proto": c["proto"], "freq": c["freq"], "sf": c["sf"],
                      "bw": c["bw"], "start": start, "end": end, "stats": st, "noise": noise[-1] if noise else None,
                      "noise_hist": NOISE_HIST[-120:]})


def load_config():
    cfg = common.radio_config(common.load_settings())
    for c in cfg["configs"]:
        c["sync"] = int(c["sync"], 16) if isinstance(c["sync"], str) else c["sync"]
    return cfg


RADIO_PINS = (22, 23, 26)  # BUSY, DIO1/IRQ, RESET on the Cap LoRa-1262


def radio_free():
    """None if we can claim the Cap's GPIO lines right now, else why not (another app holds them)."""
    try:
        req = gpiod.request_lines("/dev/gpiochip0", consumer="meshscan-probe",
                                  config={p: gpiod.LineSettings() for p in RADIO_PINS})
        req.release()
        return None
    except OSError as e:
        return f"{e.strerror or e}"


def wait_for_radio(mode_of):
    """Block while the radio is paused or held by another app. Never touches the radio itself.

    Returns False if we should exit instead (stop requested or settings changed)."""
    delay, logged = 2, None
    while RUNNING:
        m = mode_of()
        if m == "reload":
            return False
        if m == "paused":
            if logged != "paused":
                print("[radio] paused: hardware released for other apps", flush=True); logged = "paused"
            write_status_raw({"mode": getattr(mode_of, "base", lambda: None)(), "radio": "paused"})
            time.sleep(1); delay = 2
            continue
        why = radio_free()
        if why is None:
            if logged:
                print("[radio] available again", flush=True)
            return True
        if logged != "busy":
            print(f"[radio] in use by another app ({why}); retrying quietly", flush=True); logged = "busy"
        write_status_raw({"mode": m, "radio": "busy", "radio_message": "Radio in use by another app"})
        end = time.time() + delay
        while RUNNING and time.time() < end and mode_of() not in ("paused", "reload"):
            time.sleep(0.5)
        delay = min(delay * 2, 30)
    return False


def mode_reader(cfg):
    """'chat' | 'scan' from the dashboard's mode file, 'paused' while the radio switch is off,
    or 'reload' once the settings change."""
    default = cfg.get("default_mode", "scan")

    def mtime():
        try:
            return os.stat(common.SETTINGS).st_mtime
        except OSError:
            return None
    started_with = mtime()

    def base():
        if not cfg.get("configured"):
            return "scan"
        try:
            m = (CTL / "mode").read_text().strip()
            return m if m in ("chat", "scan") else default
        except OSError:
            return default

    def mode_of():
        if mtime() != started_with:
            return "reload"           # systemd restarts us with the new settings
        if not common.radio_on():
            return "paused"           # dashboard / Mesh Map paused the radio
        return base()                 # never "chat" before the first-run setup
    mode_of.base = base
    return mode_of


def main():
    global RUNNING
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("RUNNING", False))
    cfg = load_config(); configs = cfg["configs"]; mode_of = mode_reader(cfg)
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    CTL.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB); db.executescript(SCHEMA); migrate(db)
    if not wait_for_radio(mode_of):
        return  # stopping, or settings changed: systemd starts us again
    if mode_of() == "chat" and "--scan" not in sys.argv:
        import asyncio, logging, meshscan_chat
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        logging.getLogger("meshscan.chat").setLevel(logging.INFO)
        print("[mode] chat", flush=True)
        asyncio.run(meshscan_chat.run_chat(sys.modules[__name__], cfg, db, CTL,
                                           lambda: mode_of() if RUNNING else "stop"))
        return  # systemd (Restart=always) starts us again in the new mode
    print("[mode] scan", flush=True)
    once = "--once" in sys.argv
    r = SX1262()
    try:
        while RUNNING and mode_of() == "scan":
            for c in configs:
                if not RUNNING or mode_of() != "scan":
                    break
                r.configure(c)
                start = time.time(); end = start + c["dwell"]
                st = dict(packets=0, crc=0, hdr_err=0, pre=0, hdr=0); noise = []; last_n = time.time(); last_s = 0  # skip the settling sample
                print(f"[{time.strftime('%H:%M:%S')}] listen {c['name']} {c['freq']}MHz SF{c['sf']} BW{c['bw']} sync 0x{c['sync']:02X} {c['dwell']}s", flush=True)
                last_m = time.time()
                while RUNNING and time.time() < end:
                    if time.time() - last_m > 1:
                        last_m = time.time()
                        if mode_of() != "scan":
                            break
                    irq = r.irq()
                    if irq & 0x0004: st["pre"] += 1
                    if irq & 0x0010: st["hdr"] += 1
                    if irq & 0x0020: st["hdr_err"] += 1
                    if irq & 0x0002:
                        crc_ok = not (irq & 0x0040)
                        data, rssi, snr = r.read_packet()
                        r.clear()
                        if not crc_ok: st["crc"] += 1
                        st["packets"] += 1
                        dec = store(db, c, data, rssi, snr, crc_ok)
                        print(json.dumps({"cfg": c["name"], "rssi": rssi, "snr": snr, "len": len(data), "crc_ok": crc_ok,
                                          "dec": dec}, default=str)[:400], flush=True)
                    elif irq:
                        r.clear(irq & ~0x0002 & 0xFFFF)
                    if time.time() - last_n > 5:
                        noise.append(r.rssi_inst()); last_n = time.time()
                        NOISE_HIST.append((round(last_n, 1), noise[-1], c["name"])); del NOISE_HIST[:-240]
                    if time.time() - last_s > 1:
                        write_status(c, start, end, st, noise); last_s = time.time()
                    time.sleep(0.005)
                db.execute("INSERT INTO dwells(config,start,end,packets,crc_errors,header_errors,preambles,headers,noise_dbm) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (c["name"], start, time.time(), st["packets"], st["crc"], st["hdr_err"],
                                                         st["pre"], st["hdr"], sorted(noise)[len(noise) // 2] if noise else None))
                db.commit()
                print(f"  -> {st} noise~{sorted(noise)[len(noise)//2] if noise else None} dBm", flush=True)
            if once:
                break
    finally:
        r.sleep()


if __name__ == "__main__":
    main()
