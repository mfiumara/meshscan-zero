# meshscan-zero

A receive-only LoRa scanner and web dashboard for the M5Stack **CardputerZero** with the **Cap LoRa-1262** (SX1262).

I put my Cardputer next to the window, listened on Meshtastic LongFast for 10 minutes and heard nothing. Turns out the Dutch MeshCore network moved to SF7 in May 2026, and I was listening on the wrong settings. Once I fixed that, it picked up 66 packets in 10 minutes from repeaters all over Noord-Holland.

So meshscan hops between presets, saves everything it hears to SQLite, and shows the nodes on a map you can open from your phone or laptop.

![dashboard](docs/dashboard.png)

## What it does

- Drives the SX1262 directly over SPI. **It never transmits.**
- Cycles through presets (defaults below; override with `/etc/meshscan.json`).
- Stores every packet with RSSI, SNR, raw bytes, and CRC status, plus per-preset counters for preambles and headers, so you can see activity even when nothing decodes.
- **MeshCore:** decodes adverts (name, type, location, public key), logs the relay path, and decrypts the Public group channel.
- **Meshtastic:** decrypts the default channel (`AQ==`) and extracts node info, position, telemetry, and text.
- Keeps a node table (first seen, last seen, packet count, last RSSI/SNR), like a normal mesh node does.
- Serves a live dashboard on port 8080: packets stream in over Server-Sent Events, nodes on a MapLibre vector map (OpenFreeMap, no API key), what the radio is listening to right now, noise floor, relays, presets, and messages. Works on phones too.

| Slot | Freq | SF | BW | Dwell |
|---|---|---|---|---|
| MeshCore NL | 869.618 MHz | 7 | 62.5 kHz | 10 min |
| Meshtastic LongFast | 869.525 MHz | 11 | 250 kHz | 10 min |
| Meshtastic MediumFast | 869.525 MHz | 9 | 250 kHz | 5 min |
| MeshCore EU/UK Narrow | 869.618 MHz | 8 | 62.5 kHz | 3 min |
| LoRaWAN 868.1 | 868.1 MHz | 7 | 125 kHz | 2 min |

## Install

On the CardputerZero (Raspberry Pi OS trixie):

```sh
git clone https://github.com/mfiumara/meshscan-zero && cd meshscan-zero
sudo ./install.sh
```

Then open `http://cardputerzero.local:8080`. Summarize in the terminal with `meshscan-report` (optional argument: hours).

The radio can only be used by one program at a time. To use MeshTerm, the Cap-LoRa-1262 app or `meshtasticd`, run `sudo systemctl stop meshscan` first.

## Hardware notes

The cap needs power before the SX1262 responds. meshscan switches on `/sys/class/leds/ext_5v_out` and drives PI4IOE5V6408 P0 high (I²C1, 0x43). Pinout follows Meshtastic's `lora-cardputer-zero.yaml`: spidev0.1, BUSY GPIO22, RESET GPIO26, DIO2 as RF switch, TCXO 3.0 V on DIO3.

## Status

This is a weekend-afternoon tool, tested on one CardputerZero v5 in Amsterdam.

- Verified on air: MeshCore adverts, relay paths, and Meshtastic NodeInfo decryption.
- Not verified on air yet: MeshCore Public channel decryption (every group message I heard was on other channels), Meshtastic position and text, and the LoRaWAN slot.
- Only the EU868 presets are set up. Other regions need a different config.
- Uses polling instead of DIO1 interrupts. That's fine for logging, but it's not a real node.

## License

GPL-3.0. See [LICENSE](LICENSE). meshscan uses the GPL-3.0 `meshtastic` Python package for its protobuf definitions.
