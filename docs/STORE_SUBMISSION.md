# Submitting Mesh Map to the CardputerZero AppStore

Everything up to the actual submission is done and checked. Only the steps that need your GitHub account and your hands are left.

## 1. Log in (once)

```sh
git clone https://github.com/CardputerZero/AppBuilder ~/repos/cz-AppBuilder
~/repos/cz-AppBuilder/czdev login
```

It prints a code and a URL: open the URL, enter the code, approve. The token is stored in `~/.czdev/credentials`.

## 2. Build, check, publish

From the root of this repository (that's where `czdev` looks for `app-builder.json`):

```sh
cd ~/repos/meshscan-zero
packaging/build-deb.sh            # -> dist/meshscan_1.0.0_all.deb
packaging/check-store.sh          # the store's own checks, offline
~/repos/cz-AppBuilder/czdev publish --deb dist/meshscan_1.0.0_all.deb
```

`publish` is not a dry run. It uploads the `.deb` to a release on your fork of `CardputerZero/packages` and opens a pull request with the listing (`app-builder.json` → `meta.json`, icon, screenshots). The first release of an app is merged by a maintainer after a human review and a **device demo**: attach the video to that pull request.

Later versions: bump `version` in `app-builder.json`, rebuild, publish again; those merge automatically once checks pass. Listing-only changes: `czdev publish --meta-only`.

The AppBuilder CI template (`build-deb.yml`) can't build this app: its generic packer writes its own control file and a single service, so it would drop our dependencies and maintainer scripts. Always publish the `.deb` from `packaging/build-deb.sh`.

## 3. Review video (30–45 s, one take, the real device)

| Time | Show |
|---|---|
| 0–5 s | The CardputerZero with the Cap LoRa-1262 on. Scroll the launcher to **Mesh Map** and open it. |
| 5–15 s | The map with real nodes. Zoom in (Sym +), press Tab to select a node: name, type, last heard. |
| 15–25 s | Press **C**: chat with channel tabs and live messages. Type a few characters to show the keyboard, then Esc. Don't send on Public on camera unless you mean to. |
| 25–32 s | Press **P**: the panel switches to *Radio paused*. Press **P** again: back to LIVE. |
| 32–40 s | Phone next to it: scan the QR (Shift+Q) or open `http://cardputerzero.local:8080` and show the dashboard updating. |
| 40–45 s | Esc back to the launcher. |

Keep PINs, private channel keys and private messages out of frame.

## 4. Listing (as submitted)

- **Title:** Mesh Map · **Package:** `meshscan` · **Share code:** `MSCN`
- **Summary:** See the LoRa mesh around you: live map, MeshCore chat and a browser dashboard
- **Category:** Radio & Comms · **Licence:** GPL-3.0-or-later
- **Permissions:** network, additional hardware, background service
- **Description:** `store.description` in `app-builder.json`
- **Icon:** `store/icon.png` · **Screenshots:** `screenshots/` (map, selected node, chat, dashboard QR)

On `CardputerZero/packages` main (25 Nov 2026) the package name, title and share code were all free. That's first-come, first-served, not a reservation.

## Likely review questions

- **Background service:** the radio and dashboard keep running when Mesh Map is closed; that's what keeps the history and the browser dashboard working. Both run as the `meshscan` system user, not root, with only the `spi`, `i2c` and `gpio` groups.
- **Network:** the dashboard listens on port 8080 on the local network. Viewing is open; sending, switching modes and settings need a PIN. Map tiles come from OpenFreeMap.
- **Shared radio:** MeshTerm, MeshCore and the LoRa app use the same Cap. meshscan never stops other apps or services. P (or the dashboard's radio button) releases the radio; if another app holds it, meshscan shows "Radio in use by another app" and waits.
- **Transmitting:** Scan mode never transmits, and neither does anything before the first-run setup. Chat mode only transmits messages you send.
- **Removal:** `apt remove` keeps `/var/lib/meshscan`; `apt purge` deletes it and the `meshscan` user.
