---
name: troubleshoot-hikvision-ipcam
description: Diagnose and fix Hikvision/HiWatch IP camera access, authentication,
  and firmware problems — ISAPI HTTP Digest auth, API firmware push, and bootloader
  TFTP recovery — using hikvision_firmware_tool.py from this repo (Python 3 stdlib
  only). Covers broken web UIs, refused upgrades, and bricked/bootlooping cameras.
user-invocable: true
when_to_use: Use when a user cannot reach or authenticate to their Hikvision-family
  IP camera, the web UI is broken, a firmware upgrade fails, or a camera needs
  unbricking via TFTP recovery.
---

# Troubleshoot Hikvision IPCam

Playbook for helping a user connect to their Hikvision-family IP camera, authenticate
over ISAPI, push firmware through the HTTP API, or recover a dead camera via the
bootloader TFTP protocol — with `hikvision_firmware_tool.py` in this repo.

## Privacy rules (non-negotiable)

- Never write the user's real IPs, serials, MACs, hostnames, or credentials into
  code, docs, tickets, commit messages, or chat-visible files. Use placeholders:
  `<camera-ip>`, `192.168.x.x`, `admin`.
- Secrets travel via env vars (`HIK_PASS`) or `getpass` prompts — never CLI flags
  in shared logs, never pasted into chat. If a password was pasted into a chat or
  shell history, advise rotating it after the work.
- Full scan of a target is fine (ports, ISAPI GETs) — it's the user's device.

## 0. Triage: which situation?

| Signal | Situation | Path |
| :--- | :--- | :--- |
| Ping OK, TCP 80/443 + 554 (RTSP) + 8000 (SDK) open | Running camera, likely broken web UI only | §1 → §2 |
| HTTP answers `401` on `/ISAPI/*` | API alive, auth needed | §1 → §2 |
| `404` on all ISAPI routes, odd ports | Not a Hikvision HTTP stack; re-identify device | §1 |
| No ping, no ports; SWKH knocks seen on UDP 9978 at power-on | Bootloader alive, firmware dead | §3 TFTP recovery |
| Camera boots into recovery repeatedly | Stuck download loop | §3 (blacklist explains it) |

Recon one-liners (adapt interface/ports to the user's OS):

```bash
ping -c 2 <camera-ip>
for p in 80 443 554 8000 9978; do nc -z -G 2 <camera-ip> $p && echo "$p open"; done
curl -s -m 6 -D - -o /dev/null http://<camera-ip>/ISAPI/System/deviceInfo | grep -i www-auth
```

A `WWW-Authenticate: Digest qop="auth", realm="IP Camera(...)"` header confirms a
healthy Hikvision ISAPI stack that only needs credentials.

## 1. Connect with auth

Have the user set credentials without leaking them:

```bash
read -s HIK_PASS; export HIK_PASS   # or: pre-commit-free one-liner in their shell
export HIK_HOST=<camera-ip> HIK_USER=admin
python3 hikvision_firmware_tool.py info
```

- `info` does an HTTP **Digest** GET on `/ISAPI/System/deviceInfo` and prints
  model / firmware / serial — always show this to the user before anything else.
- Wrong password → clean `HTTP 401` message; unreachable → `Cannot reach camera`.
- Curl equivalent for ad-hoc checks: `curl --digest -u admin '<http://<camera-ip>/ISAPI/System/deviceInfo>'`
- Auth notes: Hikvision uses standard MD5 Digest with `qop="auth"`. 401 on a route
  means "exists, needs auth"; 404 means the route (or firmware generation) differs.

## 2. API firmware update (running camera, no sudo)

**Preconditions:** camera answers `info`; user has `digicap.dav` from the official
Hikvision portal for the **exact model + region** shown by `info`.

```bash
python3 hikvision_firmware_tool.py push --yes --firmware ./digicap.dav
```

What the tool does: Digest-authenticated PUT to `/ISAPI/System/updateFirmware`,
parsed response, auto-reboot when the camera stages the image, then polling until
the camera returns with the new firmware version.

Interpreting camera responses:

| Response | Meaning | Action |
| :--- | :--- | :--- |
| `statusCode=7 rebootRequired` | **Success** — package staged, reboot applies it | Tool reboots + polls automatically |
| `statusCode=1 OK` | Success, flashing started | Tool polls automatically |
| `statusCode=6 badDevType` | Wrong model or region | Stop. Get matching package. Never force. |
| `statusCode=3 deviceError` | Not a valid firmware package | Check file provenance |
| `Broken pipe` mid-upload | Camera validated the header early and hung up | Package is wrong for this device; explain, don't retry blindly |

Upload mechanics worth knowing if writing custom code: `urllib`'s Digest handler
sends the whole body before the 401 challenge, which kills multi-MB PUTs with
`Errno 32`. The tool uses a curl-style two-phase PUT (headers + `Expect:
100-continue` first, then the authed body) — see `put_firmware_digest()`.

Safety: never power off between "accepted/staged" and "back online" (~2–5 min).
A re-flash of the same version is valid and rebuilds broken web components.

## 3. TFTP bootloader recovery (bricked camera, requires sudo)

When HTTP is gone, the bootloader still asks for help at power-on: it broadcasts a
`SWKH` probe (`53 57 4b 48`) on UDP 9978, then TFTP-downloads `digicap.dav` from a
**hardcoded** server address `192.168.1.128` (legacy: `192.0.0.128`).

The PC does **not** need its main IP changed — add a secondary address (alias):

```bash
sudo ifconfig <iface> alias 192.168.1.128 255.255.255.0   # macOS
sudo ip addr add 192.168.1.128/24 dev <iface>             # Linux
sudo python3 hikvision_firmware_tool.py --dir ./firmware
```

Then: power-cycle the camera → tool logs the SWKH knock → file request → progress
bar → `Success`. The tool blacklists freshly-flashed IPs for 10 min (bootloop
prevention); when the log shows `ignored (recently flashed)`, wait 10–20 s and
power-cycle once more — the camera boots normally.

Notes: port 69 needs root (OS-level, not optional). If a firewall blocks it, no
knock will be answered — check before blaming the camera. Missing model-specific
files get a TFTP error so the camera falls back to `digicap.dav`.

## 4. Failure signatures cheat sheet

- `401` everywhere incl. `/ISAPI/System/deviceInfo` → credentials, not breakage.
- Web UI renders blank/plugin errors but ISAPI 200/401 → classic dead browser-plugin
  UI; the fix is `push` (re-flash), not browser archaeology.
- Upload rejected with XML `badDevType` → region/model mismatch; fetch the exact
  portal package. The camera refusing is a feature — it protects the device.
- Port 69 `Permission denied` → run recovery with sudo.
- `Address already in use` on 69/9978 → another TFTP server is running.

## 5. Reference

- Tool + protocol details: `./README.md`, module docstring in
  `hikvision_firmware_tool.py`.
- Protocol behavior is covered by `tests/test_firmware_tool.py` (loopback, no
  camera needed) — run `python3 -m unittest discover -s tests -v` after any change.
- Original protocol research: Den34DK's Hikvision-Recovery-TFTP (MIT).
