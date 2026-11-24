# SPDX-License-Identifier: Apache-2.0
# Adapted from MeshTerm's meshterm/core/radionode.py (https://github.com/jpmartineau/MeshTerm),
# Copyright the MeshTerm authors, licensed under the Apache License 2.0 (see NOTICE and
# licenses/Apache-2.0.txt). Changes: trimmed to the CardputerZero Cap LoRa-1262 bring-up that
# meshscan needs (power switches, PI4IOE5V6408 RF expander, SX1262 constructor args, identity seed).
"""Board bring-up for the M5Stack Cap LoRa-1262 on a CardputerZero, for openhop-core's SX1262Radio."""
import fcntl, logging, os, time
from pathlib import Path

log = logging.getLogger("meshscan.board")

#: Pins and switches of the Cap LoRa-1262 on the CardputerZero EXT header (same as MeshTerm's
#: CARDPUTER_ZERO_CAP and Meshtastic's lora-cardputer-zero.yaml).
CAP_WIRING = {
    "bus_id": 0, "cs_id": 1, "cs_pin": -1, "gpio_chip": 0, "use_gpiod_backend": False,
    "reset_pin": 26, "busy_pin": 22, "irq_pin": 23, "txen_pin": -1, "rxen_pin": -1,
    "use_dio2_rf": True, "use_dio3_tcxo": True, "is_waveshare": False,
}
LEDS = ("ext_5v_out=1", "ext_usb_gpio_fun=0")
PI4IO_BUS, PI4IO_ADDR, PI4IO_HIGH = 1, 0x43, (0,)
_I2C_SLAVE = 0x0703
_PI4IO_ID, _PI4IO_DIRECTION, _PI4IO_OUTPUT, _PI4IO_HIGH_Z = 0x01, 0x03, 0x05, 0x07
_PI4IO_MAKER = 0b101


def _set_leds(entries, root=Path("/sys/class/leds")):
    previous = []
    for entry in entries:
        name, _, value = entry.partition("=")
        path = root / name / "brightness"
        if not path.exists():
            raise RuntimeError(f"{path} does not exist: is this a CardputerZero with the Cap LoRa-1262?")
        previous.append((path, path.read_text().strip()))
        path.write_text(value)
    return previous


def restore_board(previous):
    """Put the power/routing switches back as prepare_board() found them."""
    for path, before in reversed(previous):
        try:
            path.write_text(before)
        except OSError as exc:
            log.warning("could not restore %s: %s", path, exc)


def _drive_pi4io(bus, address, high):
    mask = sum(1 << pin for pin in high) & 0xFF
    fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
    try:
        fcntl.ioctl(fd, _I2C_SLAVE, address)
        os.write(fd, bytes([_PI4IO_ID]))
        if os.read(fd, 1)[0] >> 5 != _PI4IO_MAKER:
            raise RuntimeError(f"{address:#04x} on i2c-{bus} is not a PI4IOE5V6408: is the Cap attached?")
        # Output level before direction, so the pin never glitches low.
        for register, value in ((_PI4IO_OUTPUT, mask), (_PI4IO_DIRECTION, mask), (_PI4IO_HIGH_Z, 0xFF & ~mask)):
            os.write(fd, bytes([register, value]))
    finally:
        os.close(fd)


def prepare_board():
    """Power the EXT header and enable the Cap's RF expander. Returns state for restore_board()."""
    previous = _set_leds(LEDS)
    time.sleep(0.1)
    try:
        _drive_pi4io(PI4IO_BUS, PI4IO_ADDR, PI4IO_HIGH)
    except BaseException:
        restore_board(previous)
        raise
    return previous


def preamble_for_sf(sf):
    """MeshCore's rule (RadioLibWrapper::preambleLengthForSF): 32 symbols up to SF8, else 16."""
    return 32 if sf <= 8 else 16


def radio_kwargs(signature_params, radio, tx_power):
    """SX1262Radio constructor args, filtered to what this openhop-core version accepts."""
    wanted = dict(CAP_WIRING)
    wanted.update(preamble_length=preamble_for_sf(radio["spreading_factor"]), frequency=radio["frequency_hz"],
                  bandwidth=radio["bandwidth_hz"], spreading_factor=radio["spreading_factor"],
                  coding_rate=radio["coding_rate"], tx_power=tx_power)
    return {k: v for k, v in wanted.items() if k in signature_params and v is not None}


def load_identity_seed(state: Path, mint):
    """The node's private seed from state/identity.key, minted and saved (0600) on first use."""
    path = state / "identity.key"
    try:
        seed = path.read_bytes()
    except OSError:
        seed = b""
    if seed:
        return seed
    seed = bytes(mint())
    tmp = path.with_name(f"identity.key.{os.getpid()}.tmp")
    tmp.write_bytes(seed); os.chmod(tmp, 0o600); tmp.replace(path)
    log.info("minted a new node identity at %s", path)
    return seed


def forget_edge_threads(radio):
    """Skip the 2 s join on openhop's IRQ watcher thread at shutdown (it sits in a 30 s poll)."""
    manager = getattr(radio, "_gpio_manager", None)
    stops = getattr(manager, "_edge_stop_events", None)
    threads = getattr(manager, "_edge_threads", None)
    if isinstance(stops, dict) and isinstance(threads, dict):
        for stop in stops.values():
            stop.set()
        threads.clear()
