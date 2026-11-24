"""meshscan chat mode: a real MeshCore node on the Cap LoRa-1262, built on openhop-core.

The node owns the radio, keeps the Public channel (plus any channels from the config),
tags its floods with a region scope (Dutch repeaters drop unscoped floods), and sends
messages that the dashboard drops into ``ctl/outbox``. Every packet it hears still goes
through meshscan's own decoder into the same SQLite database, so the map, the node list
and the live feed keep working.

Board bring-up reuses MeshTerm's ``radionode.py`` (Apache-2.0), which ships on the
CardputerZero image together with openhop-core (MIT).
"""
import asyncio, importlib.util, json, logging, os, sqlite3, sys, time
from pathlib import Path

MESHTERM_SITE = os.environ.get("MESHTERM_SITE", "/usr/lib/meshterm/site")
RADIONODE = Path(MESHTERM_SITE) / "meshterm" / "core" / "radionode.py"

# Same pins and switches as MeshTerm's CARDPUTER_ZERO_CAP wiring.
CAP_WIRING = {
    "bus_id": 0, "cs_id": 1, "cs_pin": -1, "gpio_chip": 0, "use_gpiod_backend": False,
    "reset_pin": 26, "busy_pin": 22, "irq_pin": 23, "txen_pin": -1, "rxen_pin": -1,
    "en_pins": None, "leds": ["ext_5v_out=1", "ext_usb_gpio_fun=0"],
    "pi4io_bus": 1, "pi4io_address": 0x43, "pi4io_high": [0],
    "use_dio2_rf": True, "use_dio3_tcxo": True, "is_waveshare": False,
}
NL_RADIO = {"frequency_hz": 869_618_000, "bandwidth_hz": 62_500, "spreading_factor": 7, "coding_rate": 5}
PUBLIC_PSK = "izOH6cXN6mrJ5e26oRXNcg=="
CONFIG_NAME = "meshcore_nl_sf7"
log = logging.getLogger("meshscan.chat")


def _load_radionode():
    if MESHTERM_SITE not in sys.path:
        # First, so openhop's pinned deps (cffi 2.x + its _cffi_backend, pycryptodome) win over
        # older system copies; meshscan's own imports (cryptography, gpiod, spidev) are not shadowed.
        sys.path.insert(0, MESHTERM_SITE)
    if not RADIONODE.exists():
        raise SystemExit(f"chat mode needs MeshTerm/openhop-core ({RADIONODE} not found)")
    spec = importlib.util.spec_from_file_location("meshscan_radionode", RADIONODE)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def payload_of(raw: bytes):
    """The MeshCore payload bytes of a raw frame (identical across repeats; only the path changes)."""
    if len(raw) < 2:
        return None
    route = raw[0] & 3; i = 1
    if route in (0, 3):
        i += 4
    if i >= len(raw):
        return None
    pl = raw[i]; i += 1
    i += (pl & 0x3F) * ((pl >> 6) + 1)
    return raw[i:]


async def run_chat(ms, cfg, db, ctl: Path, mode_of):
    """Run the chat node until the mode file stops saying 'chat'."""
    rn = _load_radionode()
    import openhop_core.companion as companion_mod
    from openhop_core.companion import models
    from openhop_core.protocol import identity as identity_mod
    from openhop_core.protocol.transport_keys import get_auto_key_for
    from openhop_core.hardware import sx1262_wrapper as sx
    import base64, inspect

    node_cfg = cfg.get("node", {})
    name = node_cfg.get("name", "meshscan")
    scope = node_cfg.get("scope", "nl-nh").lstrip("#")
    radio_cfg = {**NL_RADIO, **node_cfg.get("radio", {})}
    tx_power = int(node_cfg.get("tx_power_dbm", 22))
    state = Path(os.path.dirname(ms.DB)) / "node"; state.mkdir(parents=True, exist_ok=True)
    outbox = ctl / "outbox"; outbox.mkdir(parents=True, exist_ok=True)

    prefs = models.NodePrefs(node_name=name, tx_power_dbm=tx_power, frequency_hz=radio_cfg["frequency_hz"],
                             bandwidth_hz=radio_cfg["bandwidth_hz"], spreading_factor=radio_cfg["spreading_factor"],
                             coding_rate=radio_cfg["coding_rate"])
    switched = rn.prepare_board(CAP_WIRING)
    radio = None
    try:
        kwargs = rn.radio_kwargs(inspect.signature(sx.SX1262Radio.__init__).parameters, CAP_WIRING, prefs)
        radio = sx.SX1262Radio(**kwargs)
        for attempt in range(3):
            try:
                if radio.begin():
                    break
            except SystemExit:
                pass
            radio.cleanup(); await asyncio.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError("SX1262 did not start (radio busy?)")

        seed = rn.load_identity_seed(state, lambda: identity_mod.LocalIdentity().get_signing_key_bytes())
        identity = identity_mod.LocalIdentity(seed)
        sent_payloads = {}  # payload hex -> message row id

        class Node(companion_mod.CompanionRadio):
            async def _on_packet_sent(self, pkt):
                try:
                    if pkt.get_payload_type() == 5 and Node.pending_id is not None:  # GRP_TXT
                        sent_payloads[pkt.get_payload().hex()] = Node.pending_id
                except Exception:
                    pass
        Node.pending_id = None

        node = Node(radio, identity, node_name=name, radio_config={
            "frequency": prefs.frequency_hz, "bandwidth": prefs.bandwidth_hz,
            "spreading_factor": prefs.spreading_factor, "coding_rate": prefs.coding_rate, "tx_power": tx_power})
        channels = [{"name": "Public", "psk": PUBLIC_PSK}] + list(node_cfg.get("channels", []))
        for idx, ch in enumerate(channels):
            if ch["name"].startswith("#") and not ch.get("psk"):
                secret = get_auto_key_for(ch["name"])  # hashtag channels: key derived from the name
            else:
                secret = base64.b64decode(ch["psk"])
            node.set_channel(idx, ch["name"], secret)
        node.set_default_flood_scope(f"#{scope}", get_auto_key_for(f"#{scope}"))
        pubkey = node.get_public_key().hex()

        counters = {"rx": 0, "tx": 0}

        async def on_rx(snr, rssi, data):
            counters["rx"] += 1
            raw = bytes(data)
            try:
                ms.store(db, CHAT_CFG, raw, float(rssi), float(snr), True, messages=False)
                pl = payload_of(raw)
                mid = sent_payloads.get(pl.hex()) if pl else None
                if mid:
                    db.execute("UPDATE messages SET repeats=repeats+1, status='repeated', updated=? WHERE id=?", (time.time(), mid))
                    db.commit()
            except Exception as e:
                log.warning("rx store failed: %s", e)

        async def on_channel(ev):
            try:
                ms.record_message(db, "meshcore", ev.channel_name, ev.sender_name, ev.text, ev.timestamp,
                                  rssi=ev.rssi, snr=ev.snr, hops=ev.path_len)
            except Exception as e:
                log.warning("message store failed: %s", e)

        node.add_push_callback("rx_log_data", on_rx)
        node.on_channel_message_event(on_channel)
        await node.start()
        log.info("chat node %s… '%s' scope #%s on %.3f MHz SF%d", pubkey[:16], name, scope,
                 prefs.frequency_hz / 1e6, prefs.spreading_factor)
        started = time.time()

        while mode_of() == "chat":
            for f in sorted(outbox.glob("*.json")):
                try:
                    job = json.loads(f.read_text())
                except (OSError, ValueError):
                    f.unlink(missing_ok=True); continue
                f.unlink(missing_ok=True)
                idx = int(job.get("channel", 0)); text = str(job.get("text", ""))[:200]
                ts = int(time.time())
                ch = node.channels.get(idx)
                cur = db.execute("INSERT INTO messages(ts,proto,channel,src,text,direction,status,repeats,sender_ts,updated) "
                                 "VALUES(?,?,?,?,?,'out','sending',0,?,?)",
                                 (time.time(), "meshcore", ch.name if ch else str(idx), name, text, ts, time.time()))
                db.commit(); Node.pending_id = cur.lastrowid
                ok = await node.send_channel_message(idx, text, timestamp=ts)
                Node.pending_id = None
                counters["tx"] += int(ok)
                db.execute("UPDATE messages SET status=?, updated=? WHERE id=? AND status='sending'",
                           ("sent" if ok else "failed", time.time(), cur.lastrowid))
                db.commit()
                log.info("sent to %s: %r -> %s", ch.name if ch else idx, text, ok)
            noise = None
            try:
                noise = radio.get_noise_floor()
            except Exception:
                pass
            if noise is not None:
                ms.NOISE_HIST.append((round(time.time(), 1), noise, CONFIG_NAME)); del ms.NOISE_HIST[:-240]
            ms.write_status_raw({
                "mode": "chat", "config": CONFIG_NAME, "proto": "meshcore", "freq": prefs.frequency_hz / 1e6,
                "sf": prefs.spreading_factor, "bw": prefs.bandwidth_hz / 1e3, "start": started, "end": None,
                "stats": {"packets": counters["rx"], "tx": counters["tx"], "pre": 0, "crc": 0},
                "noise": noise, "noise_hist": ms.NOISE_HIST[-120:],
                "node": {"name": name, "pubkey": pubkey, "scope": scope,
                         "channels": [{"idx": i, "name": c["name"]} for i, c in enumerate(channels)]}})
            await asyncio.sleep(0.5)
        log.info("mode changed, stopping chat node")
        await node.stop()
    finally:
        if radio is not None:
            rn._forget_edge_threads(radio)
            radio.cleanup()
        rn.restore_leds(switched)


CHAT_CFG = {"name": CONFIG_NAME, "proto": "meshcore", "freq": 869.618, "sf": 7, "bw": 62.5}
