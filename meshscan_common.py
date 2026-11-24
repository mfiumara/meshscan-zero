"""Shared by the radio service (meshscan.py) and the dashboard (meshscan-web.py):
region presets, the settings file written by the first-run setup, and PIN hashing.
Standard library only, because the dashboard runs on the system Python.
"""
import hashlib, hmac, json, os, secrets

STATE = os.environ.get("MESHSCAN_STATE", "/var/lib/meshscan")
CTL = os.path.join(STATE, "ctl")                  # writable by the dashboard user
SETTINGS = os.path.join(CTL, "settings.json")
LEGACY = os.environ.get("MESHSCAN_CFG", "/etc/meshscan.json")


def _mc(name, freq, sf, bw, dwell):
    return {"name": name, "proto": "meshcore", "freq": freq, "sf": sf, "bw": bw, "sync": 0x12, "dwell": dwell}


def _mt(name, freq, sf, dwell):
    return {"name": name, "proto": "meshtastic", "freq": freq, "sf": sf, "bw": 250, "sync": 0x2B, "dwell": dwell}


LORAWAN_EU = {"name": "raw_lorawan_868.1_sf7", "proto": "raw", "freq": 868.1, "sf": 7, "bw": 125, "sync": 0x34, "dwell": 120}

NL_PROVINCES = [
    ("nl", "Whole country"), ("nl-gr", "Groningen"), ("nl-fr", "Friesland"), ("nl-dr", "Drenthe"),
    ("nl-ov", "Overijssel"), ("nl-fl", "Flevoland"), ("nl-ge", "Gelderland"), ("nl-ut", "Utrecht"),
    ("nl-nh", "Noord-Holland"), ("nl-zh", "Zuid-Holland"), ("nl-ze", "Zeeland"), ("nl-nb", "Noord-Brabant"),
    ("nl-li", "Limburg"),
]

# MeshCore chat presets per region (sources: meshwiki.nl "The switch" for NL; the MeshCore app
# preset list as documented by meshcore-info.eu / getuncabled for EU/UK Narrow; MeshCore Zero
# docs for USA/Canada) plus the presets the scanner hops through in Scan mode.
REGIONS = {
    "nl": {
        "label": "Netherlands", "detail": "MeshCore NL · 869.618 MHz · SF7 · 62.5 kHz",
        "radio": {"frequency_hz": 869_618_000, "bandwidth_hz": 62_500, "spreading_factor": 7, "coding_rate": 5},
        "chat_config": "meshcore_nl_sf7", "scopes": NL_PROVINCES, "default_scope": "nl",
        "scan": [_mc("meshcore_nl_sf7", 869.618, 7, 62.5, 600), _mt("meshtastic_longfast", 869.525, 11, 600),
                 _mt("meshtastic_mediumfast", 869.525, 9, 300), _mc("meshcore_eu_narrow_sf8", 869.618, 8, 62.5, 180),
                 LORAWAN_EU],
    },
    "eu_narrow": {
        "label": "EU / UK", "detail": "MeshCore EU/UK Narrow · 869.618 MHz · SF8 · 62.5 kHz",
        "radio": {"frequency_hz": 869_618_000, "bandwidth_hz": 62_500, "spreading_factor": 8, "coding_rate": 8},
        "chat_config": "meshcore_eu_narrow_sf8", "scopes": [], "default_scope": "",
        "scan": [_mc("meshcore_eu_narrow_sf8", 869.618, 8, 62.5, 600), _mt("meshtastic_longfast", 869.525, 11, 600),
                 _mt("meshtastic_mediumfast", 869.525, 9, 300), _mc("meshcore_nl_sf7", 869.618, 7, 62.5, 180),
                 LORAWAN_EU],
    },
    "us": {
        "label": "USA / Canada", "detail": "MeshCore US · 910.525 MHz · SF7 · 62.5 kHz",
        "radio": {"frequency_hz": 910_525_000, "bandwidth_hz": 62_500, "spreading_factor": 7, "coding_rate": 5},
        "chat_config": "meshcore_us_sf7", "scopes": [], "default_scope": "",
        "scan": [_mc("meshcore_us_sf7", 910.525, 7, 62.5, 600), _mt("meshtastic_us_longfast", 906.875, 11, 600)],
    },
}
CONFIG_LABELS = {c["name"]: c for r in REGIONS.values() for c in r["scan"]}


def load_settings():
    try:
        with open(SETTINGS) as f:
            s = json.load(f)
        return s if isinstance(s, dict) and s.get("region") in REGIONS else None
    except (OSError, ValueError):
        return None


def save_settings(s):
    os.makedirs(CTL, exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS)


def radio_config(settings):
    """What the radio service runs: chat node settings + scan list. None settings = receive-only defaults."""
    if settings is None:
        if os.path.exists(LEGACY):  # hand-written config from before the setup screen existed
            with open(LEGACY) as f:
                cfg = json.load(f)
            cfg.setdefault("configs", REGIONS["nl"]["scan"])
            cfg["configured"] = True
            return cfg
        return {"configured": False, "default_mode": "scan", "configs": REGIONS["nl"]["scan"], "node": {}}
    region = REGIONS[settings["region"]]
    return {
        "configured": True,
        "default_mode": settings.get("default_mode", "scan"),
        "configs": region["scan"],
        "node": {"name": settings["name"], "scope": settings.get("scope", ""), "channels": settings.get("channels", []),
                 "radio": region["radio"], "config_name": region["chat_config"]},
        "home": settings.get("home"),
    }


PIN_ITERATIONS = 120_000


def hash_pin(pin):
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "hash": hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_ITERATIONS).hex(),
            "iterations": PIN_ITERATIONS}


def check_pin(stored, pin):
    if not stored or not isinstance(pin, str):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(stored["salt"]), int(stored["iterations"]))
    return hmac.compare_digest(digest.hex(), stored["hash"])
