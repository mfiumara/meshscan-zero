"""meshscan chat mode: a real MeshCore node on the Cap LoRa-1262, built on openhop-core (MIT, PyPI).

The node owns the radio, keeps the Public channel (plus channels from the settings), tags its
floods with a region scope (Dutch repeaters drop unscoped floods), and sends the messages the
dashboard drops into ctl/outbox. Every packet it hears still goes through meshscan's own decoder
into the same SQLite database, so the map, node list and live feed keep working.
"""
import asyncio, base64, inspect, json, logging, os, time
from pathlib import Path

import meshscan_board as board

PUBLIC_PSK = "izOH6cXN6mrJ5e26oRXNcg=="
log = logging.getLogger("meshscan.chat")


def payload_of(raw: bytes):
    """The MeshCore payload of a raw frame (identical across repeats; only the path changes)."""
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
    """Run the chat node until mode_of() stops returning 'chat'."""
    from openhop_core import companion as companion_mod
    from openhop_core.protocol import identity as identity_mod
    from openhop_core.protocol.transport_keys import get_auto_key_for
    from openhop_core.hardware import sx1262_wrapper as sx

    node_cfg = cfg["node"]
    name = node_cfg["name"]
    scope = (node_cfg.get("scope") or "").lstrip("#")
    radio_cfg = node_cfg["radio"]
    config_name = node_cfg.get("config_name", "meshcore")
    chat_cfg = dict(ms.CONFIG_LABELS.get(config_name) or {"name": config_name, "proto": "meshcore",
                    "freq": radio_cfg["frequency_hz"] / 1e6, "sf": radio_cfg["spreading_factor"],
                    "bw": radio_cfg["bandwidth_hz"] / 1e3})
    tx_power = int(node_cfg.get("tx_power_dbm", 22))
    state = Path(os.path.dirname(ms.DB)) / "node"; state.mkdir(parents=True, exist_ok=True)
    outbox = ctl / "outbox"; outbox.mkdir(parents=True, exist_ok=True)

    switched = board.prepare_board()
    radio = None
    try:
        kwargs = board.radio_kwargs(inspect.signature(sx.SX1262Radio.__init__).parameters, radio_cfg, tx_power)
        radio = sx.SX1262Radio(**kwargs)
        for attempt in range(3):
            try:
                if radio.begin():
                    break
            except SystemExit:  # openhop's GPIO manager exits on a busy pin
                pass
            radio.cleanup(); await asyncio.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError("SX1262 did not start: is another program using the radio?")

        seed = board.load_identity_seed(state, lambda: identity_mod.LocalIdentity().get_signing_key_bytes())
        identity = identity_mod.LocalIdentity(seed)
        sent_payloads = {}  # payload hex -> outgoing message row id

        class Node(companion_mod.CompanionRadio):
            pending_id = None

            async def _on_packet_sent(self, pkt):
                if pkt.get_payload_type() == 5 and Node.pending_id is not None:  # GRP_TXT
                    sent_payloads[pkt.get_payload().hex()] = Node.pending_id

        node = Node(radio, identity, node_name=name, radio_config={
            "frequency": radio_cfg["frequency_hz"], "bandwidth": radio_cfg["bandwidth_hz"],
            "spreading_factor": radio_cfg["spreading_factor"], "coding_rate": radio_cfg["coding_rate"],
            "tx_power": tx_power})
        channels = [{"name": "Public", "psk": PUBLIC_PSK}] + list(node_cfg.get("channels", []))
        for idx, ch in enumerate(channels):
            # hashtag channels derive their key from the name, like the MeshCore app does
            secret = base64.b64decode(ch["psk"]) if ch.get("psk") else get_auto_key_for(ch["name"])
            node.set_channel(idx, ch["name"], secret)
        if scope:
            node.set_default_flood_scope(f"#{scope}", get_auto_key_for(f"#{scope}"))
        pubkey = node.get_public_key().hex()
        counters = {"rx": 0, "tx": 0}

        async def on_rx(snr, rssi, data):
            counters["rx"] += 1
            raw = bytes(data)
            try:
                ms.store(db, chat_cfg, raw, float(rssi), float(snr), True, messages=False)
                pl = payload_of(raw)
                mid = sent_payloads.get(pl.hex()) if pl else None
                if mid:
                    db.execute("UPDATE messages SET repeats=repeats+1, status='repeated', updated=? WHERE id=?",
                               (time.time(), mid))
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
        log.info("chat node %s… '%s' scope %s on %.3f MHz SF%d", pubkey[:16], name, f"#{scope}" if scope else "none",
                 radio_cfg["frequency_hz"] / 1e6, radio_cfg["spreading_factor"])
        started = time.time()

        while mode_of() == "chat":
            for f in sorted(outbox.glob("*.json")):
                try:
                    job = json.loads(f.read_text())
                except (OSError, ValueError):
                    f.unlink(missing_ok=True); continue
                f.unlink(missing_ok=True)
                idx = int(job.get("channel", 0)); text = str(job.get("text", ""))[:200]
                ch = node.channels.get(idx); ts = int(time.time())
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
            try:
                noise = radio.get_noise_floor()
            except Exception:
                noise = None
            if noise is not None:
                ms.NOISE_HIST.append((round(time.time(), 1), noise, config_name)); del ms.NOISE_HIST[:-240]
            ms.write_status_raw({
                "mode": "chat", "radio": "on", "config": config_name, "proto": "meshcore", "freq": radio_cfg["frequency_hz"] / 1e6,
                "sf": radio_cfg["spreading_factor"], "bw": radio_cfg["bandwidth_hz"] / 1e3, "start": started,
                "end": None, "stats": {"packets": counters["rx"], "tx": counters["tx"], "pre": 0, "crc": 0},
                "noise": noise, "noise_hist": ms.NOISE_HIST[-120:],
                "node": {"name": name, "pubkey": pubkey, "scope": scope,
                         "channels": [{"idx": i, "name": c["name"]} for i, c in enumerate(channels)]}})
            await asyncio.sleep(0.5)
        log.info("leaving chat mode")
        await node.stop()
    finally:
        if radio is not None:
            board.forget_edge_threads(radio)
            radio.cleanup()
        board.restore_board(switched)
