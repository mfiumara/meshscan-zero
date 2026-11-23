#!/usr/bin/env python3
"""meshscan: receive-only LoRa scanner for CardputerZero + Cap LoRa-1262 (SX1262).

Hops through LoRa configs (raw LoRaWAN sanity, Meshtastic, MeshCore), logs every
packet to SQLite, decodes Meshtastic (default key) and MeshCore (adverts, Public
channel) and keeps a node database. Never transmits.
"""
import base64, hashlib, hmac, json, os, signal, sqlite3, struct, sys, time

import gpiod, smbus2, spidev
from gpiod.line import Direction, Value

DB = os.environ.get("MESHSCAN_DB", "/var/lib/meshscan/meshscan.db")
CFG = os.environ.get("MESHSCAN_CFG", "/etc/meshscan.json")

DEFAULT_CONFIGS = [
    # name, proto, MHz, SF, BW code, sync, dwell s
    {"name": "meshcore_nl_sf7", "proto": "meshcore", "freq": 869.618, "sf": 7, "bw": 62.5, "sync": 0x12, "dwell": 600},
    {"name": "meshtastic_longfast", "proto": "meshtastic", "freq": 869.525, "sf": 11, "bw": 250, "sync": 0x2B, "dwell": 600},
    {"name": "meshtastic_mediumfast", "proto": "meshtastic", "freq": 869.525, "sf": 9, "bw": 250, "sync": 0x2B, "dwell": 300},
    {"name": "meshcore_eu_narrow_sf8", "proto": "meshcore", "freq": 869.618, "sf": 8, "bw": 62.5, "sync": 0x12, "dwell": 180},
    {"name": "raw_lorawan_868.1_sf7", "proto": "raw", "freq": 868.1, "sf": 7, "bw": 125, "sync": 0x34, "dwell": 120},
]
BW_CODE = {7.8: 0x00, 10.4: 0x08, 15.6: 0x01, 20.8: 0x09, 31.25: 0x02, 41.7: 0x0A, 62.5: 0x03, 125: 0x04, 250: 0x05, 500: 0x06}

# Meshtastic default channel key ("AQ==") and MeshCore Public channel key
MT_DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
MC_PUBLIC_KEY = base64.b64decode("izOH6cXN6mrJ5e26oRXNcg==")

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None
try:
    from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2
except ImportError:
    mesh_pb2 = None


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
        self.cmd(0x98, 0xD7, 0xDB); time.sleep(0.01)  # image cal 863-870
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
    if Cipher is None or mesh_pb2 is None:
        return out
    try:
        nonce = struct.pack("<QI", pid, src) + b"\0\0\0\0"
        data = mesh_pb2.Data(); data.ParseFromString(aes_ctr(MT_DEFAULT_KEY, nonce, p[16:]))
        if data.portnum == 0:
            raise ValueError
        out["decrypted"] = True
        out["port"] = portnums_pb2.PortNum.Name(data.portnum)
        pl = data.payload
        if data.portnum == portnums_pb2.TEXT_MESSAGE_APP:
            out["text"] = pl.decode("utf-8", "replace")
        elif data.portnum == portnums_pb2.NODEINFO_APP:
            u = mesh_pb2.User(); u.ParseFromString(pl)
            out["user"] = {"long_name": u.long_name, "short_name": u.short_name,
                           "hw": mesh_pb2.HardwareModel.Name(u.hw_model) if u.hw_model else None,
                           "role": u.role}
        elif data.portnum == portnums_pb2.POSITION_APP:
            pos = mesh_pb2.Position(); pos.ParseFromString(pl)
            if pos.latitude_i or pos.longitude_i:
                out["pos"] = {"lat": pos.latitude_i / 1e7, "lon": pos.longitude_i / 1e7, "alt": pos.altitude}
        elif data.portnum == portnums_pb2.TELEMETRY_APP:
            t = telemetry_pb2.Telemetry(); t.ParseFromString(pl)
            if t.HasField("device_metrics"):
                dm = t.device_metrics
                out["telemetry"] = {"battery": dm.battery_level, "voltage": round(dm.voltage, 2),
                                    "ch_util": round(dm.channel_utilization, 1), "air_util": round(dm.air_util_tx, 1)}
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


def store(db, c, data, rssi, snr, crc_ok):
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
        if dec.get("text"):
            db.execute("INSERT INTO messages(ts,proto,channel,src,text) VALUES(?,?,?,?,?)",
                       (now, "meshtastic", str(dec["channel_hash"]), dec["src"], dec["text"]))
    if dec and c["proto"] == "meshcore":
        if dec.get("pubkey"):
            pos = dec.get("pos") or {}
            upsert_node(db, "meshcore", dec["pubkey"][:16], now, rssi, snr, name=dec.get("name"),
                        kind=dec.get("node_type"), lat=pos.get("lat"), lon=pos.get("lon"))
        if dec.get("text"):
            db.execute("INSERT INTO messages(ts,proto,channel,src,text) VALUES(?,?,?,?,?)",
                       (now, "meshcore", dec.get("channel"), None, dec["text"]))
        # last relay hop = a repeater we heard directly
        if dec.get("path") and dec["route"] in ("FLOOD", "TRANSPORT_FLOOD"):
            upsert_node(db, "meshcore", "hop:" + dec["path"][-1], now, rssi, snr, kind="repeater?")
    db.commit()
    return dec


# ---------------------------------------------------------------- main loop
RUNNING = True
STATUS = os.path.join(os.path.dirname(DB), "status.json")
NOISE_HIST = []  # (ts, dBm, config) for the dashboard sparkline


def write_status(c, start, end, st, noise):
    try:
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "config": c["name"], "proto": c["proto"], "freq": c["freq"], "sf": c["sf"],
                       "bw": c["bw"], "start": start, "end": end, "stats": st, "noise": noise[-1] if noise else None,
                       "noise_hist": NOISE_HIST[-120:]}, f)
        os.chmod(tmp, 0o644); os.replace(tmp, STATUS)
    except OSError:
        pass


def main():
    global RUNNING
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("RUNNING", False))
    configs = DEFAULT_CONFIGS
    if os.path.exists(CFG):
        configs = json.load(open(CFG))["configs"]
        for c in configs:
            c["sync"] = int(c["sync"], 16) if isinstance(c["sync"], str) else c["sync"]
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    db = sqlite3.connect(DB); db.executescript(SCHEMA); db.commit()
    once = "--once" in sys.argv
    r = SX1262()
    try:
        while RUNNING:
            for c in configs:
                if not RUNNING:
                    break
                r.configure(c)
                start = time.time(); end = start + c["dwell"]
                st = dict(packets=0, crc=0, hdr_err=0, pre=0, hdr=0); noise = []; last_n = time.time(); last_s = 0  # skip the settling sample
                print(f"[{time.strftime('%H:%M:%S')}] listen {c['name']} {c['freq']}MHz SF{c['sf']} BW{c['bw']} sync 0x{c['sync']:02X} {c['dwell']}s", flush=True)
                while RUNNING and time.time() < end:
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
