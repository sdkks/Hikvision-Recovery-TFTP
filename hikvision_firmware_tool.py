#!/usr/bin/env python3
"""Hikvision camera firmware tool — cross-platform (macOS / Linux / Windows).

Two ways to use it:

  1. Flash a RUNNING camera over HTTP (no sudo, no IP changes):

         python3 hikvision_firmware_tool.py info 192.168.0.156
         python3 hikvision_firmware_tool.py push 192.168.0.156 --yes

     Uses the official ISAPI endpoint PUT /ISAPI/System/updateFirmware with
     HTTP Digest auth. Credentials: --user/--password, or env HIK_USER/HIK_PASS.
     The camera validates the digicap server-side and rejects wrong-model
     firmware (badDevType) instead of flashing it.

  2. Bootloader TFTP recovery mode (default, no subcommand) for bricked
     cameras — Python port of Den34DK's Hikvision-Recovery-TFTP (C#/.NET 8).
     Stdlib only. REQUIRES sudo/root (UDP port 69 is privileged) and the PC
     must answer at 192.168.1.128 (add it as an alias to keep your main IP).

         sudo python3 hikvision_firmware_tool.py

     Put the firmware file next to this script, named exactly 'digicap.dav'.

Environment variables (CLI flags win):
  HIK_HOST, HIK_USER, HIK_PASS     camera address / credentials for info/push
  HIK_DIR, HIK_TFTP_PORT,          recovery-mode server options
  HIK_HANDSHAKE_PORT

TFTP recovery implements:
  * the proprietary Hikvision 'SWKH' UDP handshake echo on port 9978
    (without it, modern bootloaders never start the TFTP recovery transfer), and
  * a read-only TFTP (RFC 1350) server on port 69 with 16-bit block-number
    wrap-around, so firmware images larger than ~32 MiB transfer correctly.
  * a 10-minute 'recently flashed' blacklist per camera IP, preventing the
    infinite recovery bootloop after a successful flash.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.client
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# Hikvision recovery network expectations
SERVER_IPS = ("192.168.1.128", "192.0.0.128")  # PC must own one of these
CAMERA_IPS = ("192.168.1.64", "192.0.0.64")  # bootloader's default client IPs
FIRMWARE_NAME = "digicap.dav"

# TFTP constants (mirror TftpServer.cs)
BLOCK_SIZE = 512
MAX_RETRIES = 5
ACK_TIMEOUT = 1.5  # seconds per ACK wait
OP_RRQ, OP_DATA, OP_ACK, OP_ERROR = 1, 3, 4, 5

# Bootloop blacklist (mirror TftpServer.cs)
BLACKLIST_SECS = 10 * 60
CLEANUP_SECS = 30

SWKH_MAGIC = b"SWKH"

_print_lock = threading.Lock()
_logfile = None  # type: ignore[var-annotated]


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


if _use_color():
    C_GREEN, C_YELLOW, C_RED, C_CYAN, C_WHITE, C_RESET = (
        "\033[32m",
        "\033[33m",
        "\033[31m",
        "\033[36m",
        "\033[37m",
        "\033[0m",
    )
else:
    C_GREEN = C_YELLOW = C_RED = C_CYAN = C_WHITE = C_RESET = ""


def log(kind: str, color: str, message: str) -> None:
    """Timestamped log line to console (colored) and log.txt (plain)."""
    line = f"[{time.strftime('%H:%M:%S')}] [{kind}] {message}"
    with _print_lock:
        print(f"{color}{line}{C_RESET}")
        if _logfile is not None:
            _logfile.write(line + "\n")
            _logfile.flush()


def log_info(msg: str) -> None:
    log("INFO", C_GREEN, msg)


def log_handshake(msg: str) -> None:
    log("HANDSHAKE", C_CYAN, msg)


def log_warning(msg: str) -> None:
    log("WARN", C_YELLOW, msg)


def log_error(msg: str) -> None:
    log("ERROR", C_RED, msg)


def source_ip_for(target_ip: str) -> str | None:
    """Which local IPv4 address would the kernel use to reach target_ip?

    UDP connect() only picks a route; it sends no packets.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def list_ipv4_addresses() -> list[str]:
    """Best-effort list of the host's IPv4 addresses (no third-party deps)."""
    addrs: list[str] = []
    for cmd in (["ip", "-4", "-o", "addr"], ["ifconfig", "-a"]):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5, check=False
            ).stdout
            for m in re.finditer(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out):
                ip = m.group(1)
                if ip != "127.0.0.1" and ip not in addrs:
                    addrs.append(ip)
            if addrs:
                break
        except (OSError, subprocess.SubprocessError):
            continue
    return addrs


class HikvisionRecoveryServer:
    def __init__(self, root_dir: str, tftp_port: int = 69, handshake_port: int = 9978):
        self.root_dir = os.path.abspath(root_dir)
        self.tftp_port = tftp_port
        self.handshake_port = handshake_port
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._active: set[str] = set()  # client IPs with transfer in flight
        self._recently_flashed: dict[str, float] = {}  # client IP -> monotonic time
        self._tftp_sock: socket.socket | None = None
        self._handshake_sock: socket.socket | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._tftp_sock = self._bind_udp(self.tftp_port)
        self._handshake_sock = self._bind_udp(self.handshake_port)
        log_info(f"TFTP Server started on port {self.tftp_port}.")
        log_handshake(
            f"Handshake listener (SWKH) active on port {self.handshake_port}."
        )
        log_info("Waiting for camera... TURN ON CAMERA POWER NOW!")
        for target in (self._listen_tftp, self._listen_handshake, self._cleanup_loop):
            threading.Thread(target=target, daemon=True).start()

    @staticmethod
    def _bind_udp(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except PermissionError:
            raise SystemExit(
                f"[ERROR] Permission denied binding UDP port {port}. "
                f"Ports below 1024 require root: re-run with sudo.\n"
                f"Tip: add 192.168.1.128 as a secondary address (alias) so your\n"
                f"main IP stays untouched:\n"
                f"  macOS: sudo ifconfig <iface> alias 192.168.1.128 255.255.255.0\n"
                f"  Linux: sudo ip addr add 192.168.1.128/24 dev <iface>"
            ) from None
        except OSError as exc:
            raise SystemExit(
                f"[ERROR] Could not bind UDP port {port} ({exc}). "
                f"Make sure other TFTP utilities (Tftpd64, etc.) are closed."
            ) from exc
        return sock

    def stop(self) -> None:
        self.stop_event.set()
        for sock in (self._tftp_sock, self._handshake_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    # ------------------------------------------------------------- listeners

    def _listen_tftp(self) -> None:
        assert self._tftp_sock is not None
        while not self.stop_event.is_set():
            try:
                data, addr = self._tftp_sock.recvfrom(2048)
            except OSError:
                break  # socket closed during shutdown
            threading.Thread(
                target=self._handle_request, args=(data, addr), daemon=True
            ).start()

    def _listen_handshake(self) -> None:
        assert self._handshake_sock is not None
        while not self.stop_event.is_set():
            try:
                data, addr = self._handshake_sock.recvfrom(2048)
            except OSError:
                break
            try:
                client_ip = addr[0]
                if data[:4] == SWKH_MAGIC:
                    with self._lock:
                        flashed = client_ip in self._recently_flashed
                    if flashed:
                        log_handshake(
                            f"[SWKH] Knock from {client_ip} detected, but ignored (recently flashed).\n"
                            f"Wait 10-20 seconds, then power cycle the camera. "
                            f"It should then boot up with its default IP address."
                        )
                    else:
                        log_handshake(
                            f"[SWKH] Knock detected from {client_ip}. Echo sent."
                        )
                        self._handshake_sock.sendto(data, addr)
            except OSError:
                continue

    def _cleanup_loop(self) -> None:
        while not self.stop_event.wait(CLEANUP_SECS):
            now = time.monotonic()
            with self._lock:
                expired = [
                    ip
                    for ip, t in self._recently_flashed.items()
                    if now - t > BLACKLIST_SECS
                ]
                for ip in expired:
                    del self._recently_flashed[ip]
            for ip in expired:
                log_info(f"[{ip}] Removed from 'recently flashed' list.")

    # ------------------------------------------------------------- TFTP

    def _handle_request(self, packet: bytes, addr: tuple[str, int]) -> None:
        try:
            if len(packet) < 4:
                return
            opcode = (packet[0] << 8) | packet[1]
            if opcode != OP_RRQ:
                return
            nul = packet.find(b"\x00", 2)
            if nul == -1:
                return
            filename = packet[2:nul].decode("ascii", errors="replace")
            client_ip = addr[0]

            with self._lock:
                if client_ip in self._active:
                    return
                flashed = client_ip in self._recently_flashed
            if flashed:
                log_warning(
                    f"[TFTP] File request from {client_ip} ignored (recently flashed).\n"
                    f"Wait 10-20 seconds, then power cycle the camera. "
                    f"It should then boot up with its default IP address."
                )
                return

            log_info(f"[TFTP] Camera {addr[0]}:{addr[1]} requested file: '{filename}'")
            file_path = self._resolve_path(filename)
            if file_path is None or not os.path.isfile(file_path):
                log_warning(
                    f"[TFTP] File '{filename}' not found. Replying error code to redirect camera."
                )
                self._send_error(addr, 0, "No such file or directory")
                return

            with self._lock:
                self._active.add(client_ip)
            threading.Thread(
                target=self._run_session, args=(file_path, addr, client_ip), daemon=True
            ).start()
        except Exception as exc:  # never let the listener thread die
            log_error(f"Critical error in TFTP handler: {exc}")

    def _resolve_path(self, filename: str) -> str | None:
        """Join filename under root_dir, refusing path traversal."""
        full = os.path.abspath(os.path.normpath(os.path.join(self.root_dir, filename)))
        try:
            if os.path.commonpath([full, self.root_dir]) != self.root_dir:
                return None
        except ValueError:  # different drives on Windows
            return None
        return full

    def _send_error(self, addr: tuple[str, int], code: int, message: str) -> None:
        # Fresh ephemeral socket, same as the C# implementation — the camera
        # accepts the redirect error from a different source port.
        payload = (
            b"\x00\x05" + code.to_bytes(2, "big") + message.encode("ascii") + b"\x00"
        )
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(payload, addr)
        except OSError:
            pass

    @staticmethod
    def _next_block(block: int) -> int:
        """16-bit TFTP block counter wrap: 65535 -> 0 (RFC 1350 rollover).

        This wrap is what makes firmware files larger than ~32 MB possible.
        """
        return (block + 1) & 0xFFFF

    def _run_session(
        self, file_path: str, addr: tuple[str, int], client_ip: str
    ) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with open(file_path, "rb") as fh:
                file_bytes = fh.read()
            total = len(file_bytes)
            name = os.path.basename(file_path)
            log_info(
                f"[{addr[0]}] Uploading '{name}' ({total / 1024 / 1024:.2f} MB)..."
            )
            started = time.monotonic()
            last_percent = -1
            offset = 0
            block = 1
            sock.settimeout(ACK_TIMEOUT)
            while offset < total:
                chunk = file_bytes[offset : offset + BLOCK_SIZE]
                packet = b"\x00\x03" + block.to_bytes(2, "big") + chunk
                retries = 0
                while True:
                    sock.sendto(packet, addr)
                    try:
                        buf, _ = sock.recvfrom(2048)
                    except socket.timeout:
                        retries += 1
                        if retries >= MAX_RETRIES:
                            with _print_lock:
                                print()
                            log_error(
                                f"[{addr[0]}] No ACK on block {block}. Transfer failed."
                            )
                            return
                        continue
                    if len(buf) >= 4 and buf[0] == 0 and buf[1] == OP_ACK:
                        ack_block = (buf[2] << 8) | buf[3]
                        if ack_block == block:
                            break
                        # ACK for a stale/unknown block: keep waiting (same as C#)
                offset += len(chunk)
                percent = int(offset / total * 100)
                if percent != last_percent or offset >= total:
                    last_percent = percent
                    self._draw_progress(addr[0], percent, offset, total)
                block = self._next_block(block)

            elapsed = time.monotonic() - started
            speed = (total / 1024 / 1024) / elapsed if elapsed > 0 else 0.0
            with _print_lock:
                print()
            log_info(
                f"[{addr[0]}] Success: '{name}' uploaded in {elapsed:.1f}s! "
                f"Speed: {speed:.2f} MB/s"
            )
        except Exception as exc:
            with _print_lock:
                print()
            log_error(f"Session error for {addr[0]}: {exc}")
        finally:
            sock.close()
            # Mirror C# behavior: any finished session (success or not) puts the
            # camera on the bootloop-prevention blacklist.
            with self._lock:
                self._active.discard(client_ip)
                self._recently_flashed[client_ip] = time.monotonic()

    @staticmethod
    def _draw_progress(ip: str, percent: int, sent: int, total: int) -> None:
        bar_width = 25
        filled = int(percent / 100 * bar_width)
        bar = "\u2588" * filled + "-" * (bar_width - filled)
        with _print_lock:
            sys.stdout.write(
                f"\r[{ip}] Progress: [{bar}] {percent}% "
                f"({sent // 1024} KB / {total // 1024} KB)"
            )
            sys.stdout.flush()


# --------------------------------------------------- ISAPI: running-camera mode

UPGRADE_ENDPOINT = "/ISAPI/System/updateFirmware"
REBOOT_ENDPOINT = "/ISAPI/System/reboot"
UPLOAD_CHUNK = 65536


def _upload_progress(sent: int, total: int) -> None:
    with _print_lock:
        sys.stdout.write(f"\r  sent {sent / 1048576:.1f} / {total / 1048576:.1f} MB")
        sys.stdout.flush()


def xml_field(xml: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", xml, re.S)
    return m.group(1) if m else None


def camera_opener(
    host: str, user: str, password: str
) -> tuple[urllib.request.OpenerDirector, str]:
    base = f"http://{host}"
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, user, password)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    return opener, base


# --- manual Digest auth for large uploads -----------------------------------
#
# urllib.request.HTTPDigestAuthHandler sends the FULL body unauthenticated and
# only replays after the 401 challenge. For a multi-MB PUT the camera answers
# 401 immediately and closes the connection mid-upload -> broken pipe. This is
# the curl --digest flow instead: fetch the challenge with headers only
# (Expect: 100-continue, no body), compute the Authorization header, then send
# the real PUT with body in one shot.


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _parse_www_auth(header: str) -> dict[str, str]:
    rest = (
        header.strip()[len("Digest") :].strip()
        if header.strip().lower().startswith("digest")
        else header.strip()
    )
    params: dict[str, str] = {}
    for m in re.finditer(r'([A-Za-z]+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', rest):
        params[m.group(1).lower()] = (
            m.group(2) if m.group(2) is not None else m.group(3)
        )
    return params


def digest_authorization(
    method: str, uri: str, user: str, password: str, challenge: str
) -> str:
    p = _parse_www_auth(challenge)
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    offered = [q.strip() for q in p.get("qop", "").split(",") if q.strip()]
    qop = "auth" if "auth" in offered else (offered[0] if offered else "")
    algorithm = p.get("algorithm", "MD5")
    cnonce = _md5_hex(os.urandom(16).hex() + str(time.time()))
    nc = "00000001"
    ha1 = _md5_hex(f"{user}:{realm}:{password}")
    ha2 = _md5_hex(f"{method}:{uri}")
    if "auth" in qop:
        response = _md5_hex(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        fields = (
            f'username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'cnonce="{cnonce}", nc={nc}, qop={qop}, response="{response}", '
            f"algorithm={algorithm}"
        )
    else:
        response = _md5_hex(f"{ha1}:{nonce}:{ha2}")
        fields = (
            f'username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'response="{response}", algorithm={algorithm}'
        )
    if "opaque" in p:
        fields += f', opaque="{p["opaque"]}"'
    return f"Digest {fields}"


def isapi_reboot(host: str, user: str, password: str) -> tuple[int, str]:
    """Digest PUT /ISAPI/System/reboot (empty body); returns (http_status, body)."""
    uri = REBOOT_ENDPOINT
    conn = http.client.HTTPConnection(host, timeout=15)
    try:
        conn.putrequest("PUT", uri, skip_accept_encoding=True)
        conn.putheader("Content-Length", "0")
        conn.putheader("Connection", "close")
        conn.endheaders()
        resp = conn.getresponse()
        status1 = resp.status
        challenge = resp.getheader("WWW-Authenticate") or ""
        resp.read()
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"no auth challenge from camera: {exc}") from exc
    finally:
        conn.close()
    if status1 != 401 or "nonce=" not in challenge:
        raise RuntimeError(f"expected 401 Digest challenge, got HTTP {status1}")
    auth = digest_authorization("PUT", uri, user, password, challenge)
    conn = http.client.HTTPConnection(host, timeout=30)
    try:
        conn.request(
            "PUT", uri, body=b"", headers={"Authorization": auth, "Content-Length": "0"}
        )
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"reboot request failed: {exc}") from exc
    finally:
        conn.close()


def poll_until_back_online(
    opener: urllib.request.OpenerDirector, base: str, previous_fw: str, minutes: int = 8
) -> None:
    print("Polling until the camera comes back online (typical: 2-5 minutes)...")
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        time.sleep(10)
        try:
            xml = fetch_device_info(opener, base)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            print(".", end="", flush=True)
            continue
        print()
        new_fw = xml_field(xml, "firmwareVersion") or "?"
        if new_fw != previous_fw:
            print(f"{C_GREEN}Camera is back online. Firmware now: {new_fw}{C_RESET}")
        else:
            print(
                f"{C_YELLOW}Camera is back online but still reports {new_fw}. If the web UI is "
                f"fixed, the flash worked anyway; otherwise re-run the push once.{C_RESET}"
            )
        return
    print(
        f"{C_YELLOW}Camera did not respond within {minutes} minutes. Check SADP / your router,\n"
        f"or use bootloader TFTP recovery mode (sudo python3 {os.path.basename(sys.argv[0])}).{C_RESET}"
    )


def put_firmware_digest(
    host: str, user: str, password: str, payload: bytes, timeout: float = 600.0
) -> tuple[int, str]:
    """Two-phase Digest PUT of the firmware; returns (http_status, body)."""
    uri = UPGRADE_ENDPOINT
    # Phase 1: headers only -> collect the 401 challenge.
    conn = http.client.HTTPConnection(host, timeout=30)
    try:
        conn.putrequest("PUT", uri, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(len(payload)))
        conn.putheader("Expect", "100-continue")
        conn.putheader("Connection", "close")
        conn.endheaders()
        resp = conn.getresponse()
        status1 = resp.status
        challenge = resp.getheader("WWW-Authenticate") or ""
        resp.read()
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"no auth challenge from camera: {exc}") from exc
    finally:
        conn.close()
    if status1 != 401 or "nonce=" not in challenge:
        raise RuntimeError(f"expected 401 Digest challenge, got HTTP {status1}")

    # Phase 2: replay with Authorization + full body (chunked, with progress).
    auth = digest_authorization("PUT", uri, user, password, challenge)
    conn = http.client.HTTPConnection(host, timeout=timeout)
    try:
        conn.putrequest("PUT", uri, skip_accept_encoding=True)
        conn.putheader("Authorization", auth)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(len(payload)))
        conn.endheaders()
        total = len(payload)
        sent = 0
        try:
            for off in range(0, total, UPLOAD_CHUNK):
                conn.send(payload[off : off + UPLOAD_CHUNK])
                sent = min(off + UPLOAD_CHUNK, total)
                _upload_progress(sent, total)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # Camera aborted mid-upload — typically it validated the firmware
            # header, rejected the file, and closed instead of draining the
            # rest. Its error response is often already in the receive
            # buffer; try to surface it before giving up.
            with _print_lock:
                print()
            try:
                if conn.sock is not None:
                    conn.sock.settimeout(10)
                resp = conn.getresponse()
                return resp.status, resp.read().decode("utf-8", errors="replace")
            except (OSError, http.client.HTTPException):
                raise RuntimeError(
                    f"connection closed by camera after {sent / 1048576:.1f} MB "
                    f"with no error response — the file was most likely rejected "
                    f"as invalid/encrypted/wrong-model at header validation."
                ) from exc
        resp = conn.getresponse()
        with _print_lock:
            print()
        return resp.status, resp.read().decode("utf-8", errors="replace")
    finally:
        conn.close()


def fetch_device_info(opener: urllib.request.OpenerDirector, base: str) -> str:
    req = urllib.request.Request(f"{base}/ISAPI/System/deviceInfo")
    with opener.open(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def print_device_info(xml: str) -> None:
    for tag in (
        "deviceName",
        "model",
        "serialNumber",
        "firmwareVersion",
        "ipAddress",
        "macAddress",
    ):
        val = xml_field(xml, tag)
        if val:
            print(f"  {tag}: {val}")


def resolve_camera_host(raw: str | None) -> str:
    host = raw or os.environ.get("HIK_HOST") or ""
    host = host.removeprefix("http://").removeprefix("https://").rstrip("/")
    if not host:
        raise SystemExit(
            "No camera host given. Pass it as an argument or set HIK_HOST."
        )
    return host


def resolve_camera_password(args: argparse.Namespace, user: str, host: str) -> str:
    password = getattr(args, "password", None) or os.environ.get("HIK_PASS")
    if password is None:
        if not sys.stdin.isatty():
            raise SystemExit(
                "No password available. Set --password, or the HIK_PASS env var "
                "(non-interactive sessions cannot prompt)."
            )
        password = getpass.getpass(f"Camera password for {user}@{host}: ")
    return password


def run_camera_command(args: argparse.Namespace) -> None:
    host = resolve_camera_host(getattr(args, "host", None))
    user = getattr(args, "user", None) or os.environ.get("HIK_USER") or "admin"
    password = resolve_camera_password(args, user, host)
    opener, base = camera_opener(host, user, password)

    try:
        info_xml = fetch_device_info(opener, base)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print(
                f"{C_RED}Authentication failed for {user}@{host} (HTTP 401). "
                f"Check --user/--password or HIK_USER/HIK_PASS.{C_RESET}"
            )
        else:
            print(f"{C_RED}HTTP {exc.code} from camera: {exc.reason}{C_RESET}")
        raise SystemExit(1)
    except (urllib.error.URLError, OSError) as exc:
        print(f"{C_RED}Cannot reach camera at {host}: {exc}{C_RESET}")
        raise SystemExit(1)

    print(f"{C_GREEN}Connected to camera {host} (HTTP Digest auth OK):{C_RESET}")
    print_device_info(info_xml)
    if args.command == "info":
        return

    # ---- push: upload digicap.dav to the running camera ----
    firmware = getattr(args, "firmware", None) or os.path.join(args.dir, FIRMWARE_NAME)
    if not os.path.isfile(firmware):
        print(
            f"{C_RED}Firmware not found: {firmware}\n"
            f"Pass --firmware /path/to/digicap.dav or place it next to this script.{C_RESET}"
        )
        raise SystemExit(1)
    size_mb = os.path.getsize(firmware) / 1024 / 1024
    model = xml_field(info_xml, "model") or "?"
    cur_fw = xml_field(info_xml, "firmwareVersion") or "?"

    print(
        f"\nAbout to flash '{os.path.basename(firmware)}' ({size_mb:.2f} MB) "
        f"to {model} (current firmware {cur_fw})."
    )
    print(
        f"{C_YELLOW}Use the official firmware matching this exact model and region.\n"
        f"The camera rejects wrong-model files, but NEVER power it off mid-flash.{C_RESET}"
    )
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print(
                f"{C_RED}Confirmation required: rerun with --yes for unattended use.{C_RESET}"
            )
            raise SystemExit(1)
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return

    with open(firmware, "rb") as fh:
        payload = fh.read()
    print(f"\nUploading {size_mb:.2f} MB to http://{host}{UPGRADE_ENDPOINT} ...")
    try:
        status, result = put_firmware_digest(host, user, password, payload)
    except (RuntimeError, OSError, http.client.HTTPException) as exc:
        print(f"{C_RED}Upload failed: {exc}{C_RESET}")
        raise SystemExit(1)
    if status != 200:
        print(f"{C_RED}Upload rejected: HTTP {status}{C_RESET}")
        _print_upgrade_response(result)
        raise SystemExit(1)

    code = xml_field(result, "statusCode")
    if code not in ("1", "7"):
        print(f"{C_RED}Camera refused the firmware:{C_RESET}")
        _print_upgrade_response(result)
        raise SystemExit(1)

    if code == "7":
        # Documented success path: package staged, reboot applies it.
        print(
            f"{C_GREEN}Camera accepted and staged the firmware (reboot required). "
            f"Triggering reboot...{C_RESET}"
        )
        try:
            _, rbody = isapi_reboot(host, user, password)
            rcode = xml_field(rbody, "statusCode")
            if rcode == "1":
                print(
                    f"{C_GREEN}Reboot triggered. The camera applies the firmware on boot.{C_RESET}"
                )
            else:
                print(
                    f"{C_YELLOW}Reboot endpoint answered statusCode={rcode}. If the camera is not "
                    f"down within a minute, power-cycle it manually — the staged "
                    f"firmware applies on boot.{C_RESET}"
                )
        except (RuntimeError, OSError, http.client.HTTPException) as exc:
            print(
                f"{C_YELLOW}Could not send the reboot command ({exc}).\n"
                f"Power-cycle the camera manually — the staged firmware applies on boot.{C_RESET}"
            )
    else:
        print(
            f"{C_GREEN}Camera accepted the firmware and is now flashing. "
            f"Do NOT power it off.{C_RESET}"
        )
    poll_until_back_online(opener, base, cur_fw)


def _print_upgrade_response(xml: str) -> None:
    code = xml_field(xml, "statusCode")
    status = xml_field(xml, "statusString")
    sub = xml_field(xml, "subStatusCode")
    if code or status:
        print(f"  statusCode={code} statusString={status} subStatusCode={sub}")
    if sub == "badDevType":
        print(
            f"{C_YELLOW}  This firmware is for a different camera model or region.\n"
            f"  Download the package for exactly this model from the Hikvision portal.{C_RESET}"
        )


# ------------------------------------------------------------------ main flow


def print_banner() -> None:
    print("=" * 73)
    print("Hikvision Custom Recovery TFTP Server (Python port of Den34DK's tool)")
    print("with support for firmware files > 32 MB")
    print("=" * 73)


def check_environment(root_dir: str) -> None:
    target = os.path.join(root_dir, FIRMWARE_NAME)
    if not os.path.isfile(target):
        print(
            f"{C_RED}WARNING: File '{FIRMWARE_NAME}' was not found in the application directory!{C_RESET}"
        )
        print(f"Search directory: {root_dir}")
        print(
            f"Please copy the firmware file into this folder and rename it to '{FIRMWARE_NAME}'."
        )
    else:
        size_mb = os.path.getsize(target) / 1024 / 1024
        print(
            f"{C_GREEN}Firmware file detected: {FIRMWARE_NAME} ({size_mb:.2f} MB){C_RESET}"
        )

    print("\nWhich local IP would be used to reach the camera bootloader:")
    match = False
    for camera_ip in CAMERA_IPS:
        src = source_ip_for(camera_ip)
        if src in SERVER_IPS:
            print(
                f"{C_GREEN} -> route to {camera_ip} uses {src}  (MATCH! Correct IP for Hikvision recovery){C_RESET}"
            )
            match = True
        else:
            used = src if src else "no route"
            print(f" - route to {camera_ip} would use: {used}")
    if not match:
        print(
            f"{C_YELLOW}\n[WARNING] Neither '{SERVER_IPS[0]}' nor '{SERVER_IPS[1]}' is configured on any active network adapter!"
        )
        print(
            "Hikvision bootloaders look for a TFTP server at one of these exact IP addresses."
        )
        print(
            "Set your network adapter IP statically to 192.168.1.128 or 192.0.0.128 (Subnet Mask: 255.255.255.0)."
        )
        print("macOS example:  sudo ifconfig en0 alias 192.168.1.128 255.255.255.0")
        print("Linux example:  sudo ip addr add 192.168.1.128/24 dev eth0")

    all_ips = list_ipv4_addresses()
    if all_ips:
        print("\nAll IPv4 addresses on this machine: " + ", ".join(all_ips))

    if sys.platform == "darwin":
        print(
            "\n[Recommendation] If macOS asks to allow 'python3' to accept incoming network"
        )
        print(
            "connections, click Allow. If you see SWKH knocks but no replies, check the"
        )
        print("application firewall (System Settings > Network > Firewall).")
    elif sys.platform == "linux":
        print(
            "\n[Recommendation] Ensure your firewall (ufw/firewalld) allows incoming UDP on ports 69 and 9978."
        )
    else:
        print(
            "\n[Recommendation] Ensure Windows Defender Firewall is TEMPORARILY DISABLED."
        )
        print("Firewalls commonly block incoming UDP packets on port 69 and 9978.")
    print("=" * 73 + "\n")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hikvision firmware tool: ISAPI flash for running cameras "
        "(no sudo) + bootloader TFTP recovery server."
    )
    parser.add_argument(
        "--dir",
        default=os.environ.get("HIK_DIR") or os.path.dirname(os.path.abspath(__file__)),
        help="directory containing digicap.dav "
        "(default: this script's folder, or HIK_DIR)",
    )
    parser.add_argument(
        "--tftp-port",
        type=int,
        default=None,
        help="TFTP UDP port (default 69; env HIK_TFTP_PORT; "
        "must stay 69 for real recovery; overridable for testing)",
    )
    parser.add_argument(
        "--handshake-port",
        type=int,
        default=None,
        help="SWKH handshake UDP port (default 9978; env HIK_HANDSHAKE_PORT)",
    )

    sub = parser.add_subparsers(dest="command", metavar="command")

    def add_auth(p: argparse.ArgumentParser) -> None:
        p.add_argument("host", nargs="?", help="camera IP/host (default: env HIK_HOST)")
        p.add_argument(
            "--user", help="camera username (default: env HIK_USER or 'admin')"
        )
        p.add_argument(
            "--password", help="camera password (default: env HIK_PASS, else prompt)"
        )

    p_info = sub.add_parser(
        "info", help="print camera model/firmware via ISAPI (no sudo)"
    )
    add_auth(p_info)

    p_push = sub.add_parser(
        "push", help="flash digicap.dav to a RUNNING camera via ISAPI (no sudo)"
    )
    add_auth(p_push)
    p_push.add_argument(
        "--firmware", help="path to digicap.dav (default: <dir>/digicap.dav)"
    )
    p_push.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )

    args = parser.parse_args()
    if args.tftp_port is None:
        args.tftp_port = _env_int("HIK_TFTP_PORT", 69)
    if args.handshake_port is None:
        args.handshake_port = _env_int("HIK_HANDSHAKE_PORT", 9978)
    return args


def main() -> None:
    if sys.platform == "win32":
        os.system("")  # enable ANSI colors on Windows 10+ consoles
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    args = parse_args()

    if args.command in ("info", "push"):
        # ISAPI mode: plain HTTP to a running camera — no root, no IP changes.
        run_camera_command(args)
        return

    root_dir = os.path.abspath(args.dir)

    global _logfile
    _logfile = open(
        os.path.join(root_dir, "log.txt"),
        "w",
        buffering=1,
        encoding="utf-8",
        errors="replace",
    )

    print_banner()
    check_environment(root_dir)

    try:
        server = HikvisionRecoveryServer(root_dir, args.tftp_port, args.handshake_port)
        server.start()
    except SystemExit as exc:
        print(exc)
        return

    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    print(
        f"{C_WHITE}\nServer is active. You can connect multiple cameras sequentially."
    )
    if sys.stdin.isatty():
        print("Press [ENTER] at any time to stop the server and exit...")
    print(C_RESET, end="")
    try:
        if sys.stdin.isatty():
            input()
        else:
            while not server.stopped:
                time.sleep(0.5)
    except (KeyboardInterrupt, EOFError):
        pass

    print(f"{C_YELLOW}\nShutting down server...")
    for i in (3, 2, 1):
        sys.stdout.write(
            f"\rClosing network listeners in {i} seconds to let active transfers finish..."
        )
        sys.stdout.flush()
        time.sleep(1)
    print()
    print(C_RESET, end="")
    server.stop()
    print(f"{C_GREEN}\nServer stopped. All logs saved to 'log.txt'.{C_RESET}")


if __name__ == "__main__":
    main()
