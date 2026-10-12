# Hikvision Firmware Tool (Python)

Cross-platform (macOS / Linux / Windows) firmware tool for **Hikvision IP cameras**, in pure stdlib Python 3 — no dependencies, no .NET runtime.

Two modes:

1. **Flash a *running* camera over HTTP (ISAPI)** — no sudo, no IP changes. For cameras whose web UI is broken but which are still on the network.
2. **Bootloader TFTP recovery server** — for bricked cameras. Implements the proprietary Hikvision `SWKH` handshake and handles firmware files larger than 32 MB.

This is the Python continuation of [Den34DK's Hikvision-Recovery-TFTP](https://github.com/Den34DK/Hikvision-Recovery-TFTP) (C#/.NET, MIT). The C# code has been removed from this repo; its protocol logic (SWKH handshake, 16-bit block-number wrap, bootloop blacklist) lives on in the Python recovery mode.

---

## Mode 1 — Flash a running camera (no sudo)

Uses the official ISAPI endpoint `PUT /ISAPI/System/updateFirmware` with HTTP Digest auth.

```bash
# credentials via env (CLI --user/--password also work; password is prompted if omitted)
export HIK_HOST=192.168.0.156 HIK_USER=admin HIK_PASS=yourpassword

python3 hikvision_firmware_tool.py info        # read model / firmware / serial
python3 hikvision_firmware_tool.py push --yes  # flash ./digicap.dav to the camera
# or: python3 hikvision_firmware_tool.py push --firmware /path/to/digicap.dav --yes
```

What `push` does:

1. Reads `deviceInfo` so you can verify the target model before flashing.
2. Uploads the digicap with a curl-style two-phase Digest auth (headers first, then the body — plain urllib breaks the connection on multi-MB PUTs).
3. The camera validates the package server-side and **rejects wrong-model/region files instead of flashing them**.
4. On success the camera stages the firmware and answers `rebootRequired` — the tool **reboots the camera automatically** and polls until it returns, reporting the new firmware version.

Response codes you may see:

| Camera response | Meaning |
| :--- | :--- |
| `statusCode=7 rebootRequired` | **Success.** Package staged; reboot applies it (handled automatically). |
| `statusCode=6 badDevType` | Firmware is for a different model or region. Get the package for exactly your model. |
| `statusCode=3 deviceError` | File is not a valid firmware package. |
| connection closed mid-upload | Camera rejected the package at header validation and hung up. Check the file's origin/model/region. |

Get firmware only from the official portal for your exact model and region: <https://www.hikvision.com/en/support/download/firmware/>

---

## Mode 2 — Bootloader TFTP recovery (requires sudo)

For cameras that won't boot or won't answer HTTP. The bootloader looks for a TFTP server at a **hardcoded IP**, so the PC must answer at `192.168.1.128` (or `192.0.0.128` for pre-2015 models). You do **not** need to change your main IP — add it as a secondary address (alias).

```bash
sudo python3 hikvision_firmware_tool.py          # serves digicap.dav from the script's folder
sudo python3 hikvision_firmware_tool.py --dir /path/to/firmware
```

Port 69 is privileged, hence sudo. To keep your main network untouched:

```bash
# macOS (en0 = your adapter)
sudo ifconfig en0 alias 192.168.1.128 255.255.255.0
# Linux
sudo ip addr add 192.168.1.128/24 dev eth0
```

### How it works

* **`SWKH` magic knock (UDP 9978):** modern bootloaders broadcast a probe with payload `53 57 4b 48` before starting recovery; the tool echoes it, which makes the camera request the firmware via TFTP. Standard TFTP servers don't do this, which is why they fail.
* **>32 MB firmware support:** TFTP block counters are 16-bit (65535 × 512 B ≈ 33.55 MB). The tool wraps the counter seamlessly (`65535 → 0`), verified byte-identical on a 33.5 MB transfer across the wrap.
* **Bootloop prevention:** after a finished transfer the camera's IP is blacklisted for 10 minutes so it isn't dragged back into recovery on its next reboot.
* **Auto-fallback redirect:** requests for missing model-specific files (e.g. `digicap_E10.dav`) get a TFTP error, pushing the camera to request the standard `digicap.dav`.

### Recovery procedure

1. Disable VPNs / VM adapters; wire PC and camera to the same switch/PoE injector.
2. Add the alias for `192.168.1.128` (commands above) and place `digicap.dav` next to the script.
3. Run with sudo. The startup check tells you whether the recovery subnet is actually routable.
4. Power-cycle the camera and watch: SWKH knock → file request → progress bar → `Success`.
5. When you see `ignored (recently flashed)`, wait 10–20 s, power-cycle the camera again — it boots normally.

### Network expectations

| Parameter | Value |
| :--- | :--- |
| PC IP (alias) | `192.168.1.128` (new) / `192.0.0.128` (legacy) |
| Subnet mask | `255.255.255.0` |
| Camera recovery IP | `192.168.1.64` / `192.0.0.64` (set by bootloader) |
| Ports | UDP 69 (TFTP), UDP 9978 (SWKH) |
| Firmware file | `digicap.dav`, exactly this name, in `--dir` |

---

## Environment variables (CLI flags win)

| Variable | Used by | Meaning |
| :--- | :--- | :--- |
| `HIK_HOST` | info / push | camera address |
| `HIK_USER` / `HIK_PASS` | info / push | credentials (default user: `admin`) |
| `HIK_DIR` | recovery / push | firmware directory |
| `HIK_TFTP_PORT` / `HIK_HANDSHAKE_PORT` | recovery | override 69 / 9978 (testing only) |

---

## Verified hardware

* **Hikvision DS-2CV2121G2-IDW** — V5.7.24 → V5.7.26 flashed live via ISAPI push (web UI was broken; repaired by the upgrade). TFTP mode protocol-tested on loopback incl. the 32 MB block wrap.
* HiWatch DS-I400(D), HiWatch DS-I450L(C), Hikvision DS-2CD2043G2-I(U) — verified with the original C# tool; recovery protocol unchanged.

---

## Troubleshooting

* **`Connection refused` / nothing happens (recovery mode):** firewall blocking UDP 69/9978, or the `192.168.1.128` alias is missing. The startup check shows the actual route to the camera.
* **`Permission denied` on port 69:** run with sudo, or add a capability on Linux.
* **Port already in use:** another TFTP server (Tftpd64, etc.) is running.
* **macOS firewall prompt:** allow `python3` incoming connections when asked.
* **Upload broken pipe:** the camera rejected the package at header validation — your digicap is for a different model/region or is not a firmware package at all.
* **Web UI dead but camera reachable:** use `push` — the ISAPI upgrade path rebuilds the web components with the new firmware.

---

## License

MIT — see [LICENSE.txt](LICENSE.txt). Based on Den34DK's Hikvision-Recovery-TFTP; the Hikvision `SWKH` recovery protocol details come from that project's research.
