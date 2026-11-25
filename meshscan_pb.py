"""Tiny protobuf wire-format reader, enough for meshscan.

Decodes the handful of Meshtastic messages meshscan shows (Data, User, Position, Telemetry) and
Mapbox Vector Tiles for Mesh Map, without the protobuf runtime or the meshtastic package.
Enum names come from the Meshtastic protobufs (GPL-3.0, https://github.com/meshtastic/protobufs).
"""
import struct


def varint(b, i):
    shift = result = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]; i += 1
        result |= (c & 0x7F) << shift
        if not c & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def fields(b):
    """Yield (field_number, wire_type, value) for one message. Raises ValueError on bad input."""
    i, n = 0, len(b)
    while i < n:
        key, i = varint(b, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = varint(b, i)
        elif wt == 1:
            if i + 8 > n:
                raise ValueError("truncated fixed64")
            v = b[i:i + 8]; i += 8
        elif wt == 2:
            ln, i = varint(b, i)
            if i + ln > n:
                raise ValueError("truncated bytes")
            v = b[i:i + ln]; i += ln
        elif wt == 5:
            if i + 4 > n:
                raise ValueError("truncated fixed32")
            v = b[i:i + 4]; i += 4
        else:
            raise ValueError(f"wire type {wt}")
        yield num, wt, v


def message(b):
    """{field: [values]} for one message."""
    out = {}
    for num, _, v in fields(b):
        out.setdefault(num, []).append(v)
    return out


def first(m, f, default=None):
    v = m.get(f)
    return v[0] if v else default


def int32(v):
    """A negative int32/int64 is sent as a 10-byte two's-complement varint."""
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= 1 << 63 else v


def zigzag(v):
    return (v >> 1) ^ -(v & 1)


def sfixed32(b):
    return struct.unpack("<i", b)[0] if isinstance(b, (bytes, bytearray)) and len(b) == 4 else 0


def float32(b):
    return struct.unpack("<f", b)[0] if isinstance(b, (bytes, bytearray)) and len(b) == 4 else 0.0


def text(b):
    return bytes(b).decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else ""


# ---------------------------------------------------------------- Meshtastic
PORTNUM = {
    0: 'UNKNOWN_APP', 1: 'TEXT_MESSAGE_APP', 2: 'REMOTE_HARDWARE_APP', 3: 'POSITION_APP', 4: 'NODEINFO_APP',
    5: 'ROUTING_APP', 6: 'ADMIN_APP', 7: 'TEXT_MESSAGE_COMPRESSED_APP', 8: 'WAYPOINT_APP', 9: 'AUDIO_APP',
    10: 'DETECTION_SENSOR_APP', 11: 'ALERT_APP', 12: 'KEY_VERIFICATION_APP', 13: 'REMOTE_SHELL_APP',
    32: 'REPLY_APP', 33: 'IP_TUNNEL_APP', 34: 'PAXCOUNTER_APP', 35: 'STORE_FORWARD_PLUSPLUS_APP',
    36: 'NODE_STATUS_APP', 64: 'SERIAL_APP', 65: 'STORE_FORWARD_APP', 66: 'RANGE_TEST_APP',
    67: 'TELEMETRY_APP', 68: 'ZPS_APP', 69: 'SIMULATOR_APP', 70: 'TRACEROUTE_APP', 71: 'NEIGHBORINFO_APP',
    72: 'ATAK_PLUGIN', 73: 'MAP_REPORT_APP', 74: 'POWERSTRESS_APP', 75: 'LORAWAN_BRIDGE',
    76: 'RETICULUM_TUNNEL_APP', 77: 'CAYENNE_APP', 78: 'ATAK_PLUGIN_V2', 112: 'GROUPALARM_APP',
    256: 'PRIVATE_APP', 257: 'ATAK_FORWARDER', 511: 'MAX',
}
HARDWARE = {
    0: 'UNSET', 1: 'TLORA_V2', 2: 'TLORA_V1', 3: 'TLORA_V2_1_1P6', 4: 'TBEAM', 5: 'HELTEC_V2_0',
    6: 'TBEAM_V0P7', 7: 'T_ECHO', 8: 'TLORA_V1_1P3', 9: 'RAK4631', 10: 'HELTEC_V2_1', 11: 'HELTEC_V1',
    12: 'LILYGO_TBEAM_S3_CORE', 13: 'RAK11200', 14: 'NANO_G1', 15: 'TLORA_V2_1_1P8', 16: 'TLORA_T3_S3',
    17: 'NANO_G1_EXPLORER', 18: 'NANO_G2_ULTRA', 19: 'LORA_TYPE', 20: 'WIPHONE', 21: 'WIO_WM1110',
    22: 'RAK2560', 23: 'HELTEC_HRU_3601', 24: 'HELTEC_WIRELESS_BRIDGE', 25: 'STATION_G1', 26: 'RAK11310',
    27: 'SENSELORA_RP2040', 28: 'SENSELORA_S3', 29: 'CANARYONE', 30: 'RP2040_LORA', 31: 'STATION_G2',
    32: 'LORA_RELAY_V1', 33: 'T_ECHO_PLUS', 34: 'PPR', 35: 'GENIEBLOCKS', 36: 'NRF52_UNKNOWN',
    37: 'PORTDUINO', 38: 'ANDROID_SIM', 39: 'DIY_V1', 40: 'NRF52840_PCA10059', 41: 'DR_DEV', 42: 'M5STACK',
    43: 'HELTEC_V3', 44: 'HELTEC_WSL_V3', 45: 'BETAFPV_2400_TX', 46: 'BETAFPV_900_NANO_TX', 47: 'RPI_PICO',
    48: 'HELTEC_WIRELESS_TRACKER', 49: 'HELTEC_WIRELESS_PAPER', 50: 'T_DECK', 51: 'T_WATCH_S3',
    52: 'PICOMPUTER_S3', 53: 'HELTEC_HT62', 54: 'EBYTE_ESP32_S3', 55: 'ESP32_S3_PICO', 56: 'CHATTER_2',
    57: 'HELTEC_WIRELESS_PAPER_V1_0', 58: 'HELTEC_WIRELESS_TRACKER_V1_0', 59: 'UNPHONE', 60: 'TD_LORAC',
    61: 'CDEBYTE_EORA_S3', 62: 'TWC_MESH_V4', 63: 'NRF52_PROMICRO_DIY', 64: 'RADIOMASTER_900_BANDIT_NANO',
    65: 'HELTEC_CAPSULE_SENSOR_V3', 66: 'HELTEC_VISION_MASTER_T190', 67: 'HELTEC_VISION_MASTER_E213',
    68: 'HELTEC_VISION_MASTER_E290', 69: 'HELTEC_MESH_NODE_T114', 70: 'SENSECAP_INDICATOR',
    71: 'TRACKER_T1000_E', 72: 'RAK3172', 73: 'WIO_E5', 74: 'RADIOMASTER_900_BANDIT', 75: 'ME25LS01_4Y10TD',
    76: 'RP2040_FEATHER_RFM95', 77: 'M5STACK_COREBASIC', 78: 'M5STACK_CORE2', 79: 'RPI_PICO2',
    80: 'M5STACK_CORES3', 81: 'SEEED_XIAO_S3', 82: 'MS24SF1', 83: 'TLORA_C6', 84: 'WISMESH_TAP',
    85: 'ROUTASTIC', 86: 'MESH_TAB', 87: 'MESHLINK', 88: 'XIAO_NRF52_KIT', 89: 'THINKNODE_M1',
    90: 'THINKNODE_M2', 91: 'T_ETH_ELITE', 92: 'HELTEC_SENSOR_HUB', 93: 'MUZI_BASE',
    94: 'HELTEC_MESH_POCKET', 95: 'SEEED_SOLAR_NODE', 96: 'NOMADSTAR_METEOR_PRO', 97: 'CROWPANEL',
    98: 'LINK_32', 99: 'SEEED_WIO_TRACKER_L1', 100: 'SEEED_WIO_TRACKER_L1_EINK', 101: 'MUZI_R1_NEO',
    102: 'T_DECK_PRO', 103: 'T_LORA_PAGER', 104: 'M5STACK_RESERVED', 105: 'WISMESH_TAG', 106: 'RAK3312',
    107: 'THINKNODE_M5', 108: 'HELTEC_MESH_SOLAR', 109: 'T_ECHO_LITE', 110: 'HELTEC_V4', 111: 'M5STACK_C6L',
    112: 'M5STACK_CARDPUTER_ADV', 113: 'HELTEC_WIRELESS_TRACKER_V2', 114: 'T_WATCH_ULTRA',
    115: 'THINKNODE_M3', 116: 'WISMESH_TAP_V2', 117: 'RAK3401', 118: 'RAK6421', 119: 'THINKNODE_M4',
    120: 'THINKNODE_M6', 121: 'MESHSTICK_1262', 122: 'TBEAM_1_WATT', 123: 'T5_S3_EPAPER_PRO',
    124: 'TBEAM_BPF', 125: 'MINI_EPAPER_S3', 126: 'TDISPLAY_S3_PRO', 127: 'HELTEC_MESH_NODE_T096',
    128: 'TRACKER_T1000_E_PRO', 129: 'THINKNODE_M7', 130: 'THINKNODE_M8', 131: 'THINKNODE_M9',
    132: 'HELTEC_V4_R8', 133: 'HELTEC_MESH_NODE_T1', 134: 'STATION_G3', 135: 'T_IMPULSE_PLUS',
    136: 'T_ECHO_CARD', 137: 'SEEED_WIO_TRACKER_L2', 138: 'CROWPANEL_P4', 139: 'HELTEC_MESH_TOWER_V2',
    140: 'MESHNOLOGY_W10', 255: 'PRIVATE_HW',
}


def mt_data(b):
    m = message(b)
    return {"portnum": first(m, 1, 0), "payload": bytes(first(m, 2, b""))}


def mt_user(b):
    m = message(b)
    return {"id": text(first(m, 1, b"")), "long_name": text(first(m, 2, b"")), "short_name": text(first(m, 3, b"")),
            "hw_model": first(m, 5, 0), "role": first(m, 7, 0)}


def mt_position(b):
    m = message(b)
    return {"latitude_i": sfixed32(first(m, 1, b"")), "longitude_i": sfixed32(first(m, 2, b"")),
            "altitude": int32(first(m, 3, 0))}


def mt_device_metrics(b):
    """Telemetry.device_metrics (field 2), or None when the telemetry is something else."""
    dm = first(message(b), 2)
    if dm is None:
        return None
    m = message(dm)
    return {"battery_level": first(m, 1, 0), "voltage": float32(first(m, 2, b"")),
            "channel_utilization": float32(first(m, 3, b"")), "air_util_tx": float32(first(m, 4, b""))}


# ---------------------------------------------------------------- Mapbox Vector Tiles
def _value(b):
    for num, _, v in fields(b):
        if num == 1:
            return text(v)
        if num == 2:
            return float32(v)
        if num == 3:
            return struct.unpack("<d", v)[0]
        if num == 4:
            return int32(v)
        if num == 5:
            return v
        if num == 6:
            return zigzag(v)
        if num == 7:
            return bool(v)
    return None


def _packed(b):
    out, i = [], 0
    while i < len(b):
        v, i = varint(b, i)
        out.append(v)
    return out


def _parts(cmds):
    """MVT geometry commands -> lists of (x, y) points (y grows downwards)."""
    parts, cur, x, y, i = [], None, 0, 0, 0
    while i < len(cmds):
        cmd, count = cmds[i] & 7, cmds[i] >> 3
        i += 1
        if cmd in (1, 2):
            for _ in range(count):
                if i + 1 >= len(cmds):
                    raise ValueError("truncated geometry")
                x += zigzag(cmds[i]); y += zigzag(cmds[i + 1])
                i += 2
                if cmd == 1:
                    cur = [(x, y)]
                    parts.append(cur)
                elif cur is not None:
                    cur.append((x, y))
        elif cmd == 7 and cur:
            cur.append(cur[0])
    return parts


def _area(r):
    return sum(r[k][0] * r[k + 1][1] - r[k + 1][0] * r[k][1] for k in range(len(r) - 1)) / 2


def _geometry(kind, cmds):
    parts = _parts(cmds)
    if kind == 1:
        pts = [p[0] for p in parts]
        if len(pts) == 1:
            return {"type": "Point", "coordinates": list(pts[0])}
        return {"type": "MultiPoint", "coordinates": pts}
    if kind == 2:
        if len(parts) == 1:
            return {"type": "LineString", "coordinates": parts[0]}
        return {"type": "MultiLineString", "coordinates": parts}
    if kind == 3:
        polys, outer = [], None
        for r in parts:
            a = _area(r)
            if a == 0:
                continue
            if outer is None:
                outer = a > 0
            if (a > 0) == outer or not polys:
                polys.append([r])          # exterior ring starts a new polygon
            else:
                polys[-1].append(r)        # opposite winding: a hole in the current polygon
        return {"type": "MultiPolygon", "coordinates": polys}
    return {"type": "Unknown", "coordinates": []}


def decode_tile(b):
    """Same shape as mapbox_vector_tile.decode(raw, default_options={'y_coord_down': True})."""
    layers = {}
    for num, _, lb in fields(b):
        if num != 3:
            continue
        name, extent, keys, values, feats = "", 4096, [], [], []
        for f, _, v in fields(lb):
            if f == 1:
                name = text(v)
            elif f == 3:
                keys.append(text(v))
            elif f == 4:
                values.append(_value(v))
            elif f == 5:
                extent = v
            elif f == 2:
                feats.append(v)
        features = []
        for fb in feats:
            try:
                m = message(fb)
                tags = _packed(first(m, 2, b""))
                props = {keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)
                         if tags[k] < len(keys) and tags[k + 1] < len(values)}
                geom = _geometry(first(m, 3, 0), _packed(first(m, 4, b"")))
            except (ValueError, IndexError):
                continue
            features.append({"properties": props, "geometry": geom, "id": first(m, 1)})
        layers[name] = {"extent": extent, "features": features}
    return layers
