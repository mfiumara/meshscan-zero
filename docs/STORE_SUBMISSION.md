# CardputerZero AppStore submission — working notes

> **Not ready to submit.** The Debian package, radio handoff, migration, store
> metadata, screenshots, and the device/CI validation required for this release
> are not complete. Do not run the publish command below until those gates pass.

## Intended listing

- **Title:** Mesh Map
- **Package:** `meshscan`
- **Summary:** LoRa scanner, live map, browser dashboard and MeshCore chat
- **Category:** Radio & Comms
- **Author:** Mattia Fiumara (`mfiumara`)
- **Source:** https://github.com/mfiumara/meshscan-zero
- **Candidate share code:** `MSCN` (not reserved until submission; recheck)
- **Permissions:** network, additional hardware, background service: true;
  camera, microphone, IMU, external display: false.

The current app receives MeshCore/Meshtastic traffic and shows decoded activity
on the device and in a browser. Chat mode can transmit user-composed messages.
Describe pause/release and non-root operation in the final listing only after
those features are implemented and verified. Licence and third-party notices
also need an audit before selecting a definitive SPDX identifier.

On the fresh `CardputerZero/packages` snapshot at
`4355f46a620a9215e83d852f472759ab177ab725`, no package `meshscan`, title
`Mesh Map`, or share code `MSCN` was present among 188 metadata entries. This
is a point-in-time check, not a reservation; the catalog can change at any time.

## Important builder distinction

The official `czdev publish --deb` accepts a package built by this repository.
The generic AppBuilder `build-deb.yml` at commit
`042356c579d728d684eccb68ed9f8669a32822bc` is a different path: it runs SCons
and then its own `pack_deb.py`. That script copies a single executable and
images/fonts, writes its own control file with an M5Stack placeholder
maintainer, and generates one generic service. It does not consume this
project's custom maintainer scripts, dependencies, or both services. A harmless
local probe of the stock packer reproduced those control and payload fields.
Therefore `app-builder.json` alone does **not** make that workflow produce the
required package. Use the project's custom build script once it exists; do not
submit the stock probe package.

## Commands for the owner — only after completion and review

The owner performs login, publish, and the review video. From the AppBuilder
checkout, the owner logs in once:

```sh
/path/to/CardputerZero-AppBuilder/czdev login
```

From the root of the completed meshscan repository, with its finished
`app-builder.json` and verified package:

```sh
/path/to/CardputerZero-AppBuilder/czdev publish --deb /absolute/path/to/meshscan_VERSION_arm64.deb
```

`publish` creates a release asset and a pull request; it is not a dry run. No
login, publish, PR, or GitHub release has been performed during this work.

## Review video: 30–45 seconds

1. **0–5 s:** Show the CardputerZero, LoRa cap and Mesh Map in the launcher;
   launch it.
2. **5–15 s:** Show real nodes on the map, select a node and zoom/pan.
3. **15–25 s:** Show the browser dashboard and its QR link; show chat without
   transmitting unless the owner explicitly wants to demonstrate it.
4. **25–35 s:** Once implemented, show radio pause, a handoff to the other LoRa
   application, and a resume/status change.
5. **35–45 s:** Return to Mesh Map and exit to APPLaunch.

The store requires a real-device demo showing launch, primary features and
exit, posted by the owner in the submission PR. Avoid showing PINs, private
channel keys, private messages, session cookies or the local control token.

## Remaining release gates

- Build an actual reproducible `.deb`, with pinned and licensed dependencies,
  no install-time pip/network access, non-root radio and web services, and a
  safe migration from the legacy `/opt`/`/etc/systemd/system` install.
- Browser sessions are held in memory and will be lost when the web service
  restarts. This is accepted for migration: users can re-enter their PIN.
- Implement and verify persistent pause/resume, radio-busy reporting, actual
  GPIO/SPI/I²C release, and a no-transmit handoff to MeshTerm.
- Generate fresh real-device screenshots and a suitable store icon; run the
  official package path, metadata and czdev preflight checks against the actual
  package, and verify upgrades, remove/reinstall and the final device state.
- Recheck package name, title and share-code uniqueness immediately before
  publication, and complete the full third-party licensing/NOTICE audit.
