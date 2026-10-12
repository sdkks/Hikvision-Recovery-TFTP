"""Tests for hikvision_firmware_tool.py — unit + loopback integration.

Run:  python3 -m unittest discover -s tests -v

No camera, no root, no third-party packages: pure stdlib. The TFTP tests
bind ephemeral high ports on loopback and talk to the real server class.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hikvision_firmware_tool as tool  # noqa: E402

SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "hikvision_firmware_tool.py")
)

# Real challenge shape observed on a DS-2CV2121G2-IDW (realm/nonce anonymized).
HIKVISION_CHALLENGE = (
    'Digest qop="auth", realm="IP Camera(FS311)", '
    'nonce="336566343a33313031653761333aa0abe58e2b6c9bcfb760b075572a85bd", '
    'stale="FALSE"'
)


class TestWwwAuthParsing(unittest.TestCase):
    def test_hikvision_style_challenge(self):
        p = tool._parse_www_auth(HIKVISION_CHALLENGE)
        self.assertEqual(p["qop"], "auth")
        self.assertEqual(p["realm"], "IP Camera(FS311)")
        self.assertEqual(p["stale"], "FALSE")
        self.assertTrue(p["nonce"])

    def test_unquoted_and_quoted_mix(self):
        p = tool._parse_www_auth('Digest algorithm=MD5, nonce=abc123, opaque="xyz"')
        self.assertEqual(p["algorithm"], "MD5")
        self.assertEqual(p["nonce"], "abc123")
        self.assertEqual(p["opaque"], "xyz")


class TestDigestAuthorization(unittest.TestCase):
    RFC_CHALLENGE = (
        'Digest realm="testrealm@host.com", qop="auth,auth-int", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
    )

    def test_rfc2617_vector_with_qop(self):
        """RFC 2617 §3.5 example inputs; response recomputed independently."""
        header = tool.digest_authorization(
            "GET", "/dir/index.html", "Mufasa", "Circle Of Life", self.RFC_CHALLENGE
        )
        cnonce = re.search(r'cnonce="([^"]+)"', header).group(1)
        ha1 = hashlib.md5(b"Mufasa:testrealm@host.com:Circle Of Life").hexdigest()
        ha2 = hashlib.md5(b"GET:/dir/index.html").hexdigest()
        expected = hashlib.md5(
            f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:00000001:{cnonce}:auth:{ha2}".encode()
        ).hexdigest()
        self.assertIn(f'response="{expected}"', header)
        self.assertIn('username="Mufasa"', header)
        self.assertIn('realm="testrealm@host.com"', header)
        self.assertIn('opaque="5ccc069c403ebaf9f0171e9517f40e41"', header)
        self.assertIn("qop=auth", header)

    def test_rfc2069_style_no_qop(self):
        header = tool.digest_authorization(
            "GET", "/x", "u", "p", 'Digest realm="r", nonce="abc123"'
        )
        ha1 = hashlib.md5(b"u:r:p").hexdigest()
        ha2 = hashlib.md5(b"GET:/x").hexdigest()
        expected = hashlib.md5(f"{ha1}:abc123:{ha2}".encode()).hexdigest()
        self.assertIn(f'response="{expected}"', header)
        self.assertNotIn("qop=", header)
        self.assertNotIn("cnonce=", header)

    def test_hikvision_challenge_selects_auth_qop(self):
        header = tool.digest_authorization(
            "PUT", "/ISAPI/System/updateFirmware", "admin", "pw", HIKVISION_CHALLENGE
        )
        self.assertIn('username="admin"', header)
        self.assertIn('realm="IP Camera(FS311)"', header)
        self.assertIn("qop=auth,", header)  # followed by response=
        self.assertIn("algorithm=MD5", header)


class TestXmlField(unittest.TestCase):
    def test_extracts_simple_field(self):
        xml = "<ResponseStatus><statusCode>7</statusCode></ResponseStatus>"
        self.assertEqual(tool.xml_field(xml, "statusCode"), "7")

    def test_strips_whitespace_and_handles_missing(self):
        self.assertEqual(tool.xml_field("<a>  text </a>", "a"), "text")
        self.assertIsNone(tool.xml_field("<a>x</a>", "b"))


class TestResolvePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = tool.HikvisionRecoveryServer(self.tmp.name)

    def test_normal_filename_stays_inside_root(self):
        path = self.server._resolve_path("digicap.dav")
        self.assertEqual(path, os.path.join(self.tmp.name, "digicap.dav"))

    def test_model_specific_filename(self):
        path = self.server._resolve_path("digicap_E10.dav")
        self.assertEqual(path, os.path.join(self.tmp.name, "digicap_E10.dav"))

    def test_traversal_is_refused(self):
        self.assertIsNone(self.server._resolve_path("../evil.dav"))
        self.assertIsNone(self.server._resolve_path("sub/../../evil.dav"))

    def test_absolute_path_is_refused(self):
        self.assertIsNone(self.server._resolve_path("/etc/passwd"))


class TestBlockWrap(unittest.TestCase):
    """The 65535 -> 0 wrap is what enables >32MB firmware transfers."""

    def test_sequence(self):
        nxt = tool.HikvisionRecoveryServer._next_block
        self.assertEqual(nxt(1), 2)
        self.assertEqual(nxt(65534), 65535)
        self.assertEqual(nxt(65535), 0)
        self.assertEqual(nxt(0), 1)


class TestTftpLoopback(unittest.TestCase):
    """Integration tests against the real server on ephemeral loopback ports."""

    FIRMWARE = os.urandom(1200)  # 3 blocks: 512 + 512 + 176

    @classmethod
    def setUpClass(cls):
        for name in ("log_info", "log_warning", "log_error", "log_handshake"):
            setattr(tool, name, lambda msg: None)  # quiet logs during tests
        cls.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(cls.tmp.name, "digicap.dav"), "wb") as fh:
            fh.write(cls.FIRMWARE)
        cls.server = tool.HikvisionRecoveryServer(
            cls.tmp.name, tftp_port=0, handshake_port=0
        )
        cls.server.start()
        cls.tftp_port = cls.server._tftp_sock.getsockname()[1]
        cls.hs_port = cls.server._handshake_sock.getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def setUp(self):
        # One server instance serves the whole class; per-IP state must be
        # cleared or the transfer test's 'recently flashed' blacklist would
        # (correctly!) silence every later test from 127.0.0.1.
        self.server._recently_flashed.clear()
        self.server._active.clear()

    def _socket(self, timeout=3.0):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.bind(("127.0.0.1", 0))
        self.addCleanup(s.close)
        return s

    def test_swkh_knock_is_echoed_byte_identically(self):
        s = self._socket()
        payload = b"SWKH\x01\x02\x03trailing"
        s.sendto(payload, ("127.0.0.1", self.hs_port))
        echoed, _ = s.recvfrom(2048)
        self.assertEqual(echoed, payload)

    def test_non_swkh_handshake_is_ignored(self):
        s = self._socket(timeout=0.6)
        s.sendto(b"NOPE\x01", ("127.0.0.1", self.hs_port))
        with self.assertRaises(socket.timeout):
            s.recvfrom(2048)

    def test_missing_file_answers_tftp_error_zero(self):
        s = self._socket()
        s.sendto(b"\x00\x01digicap_E10.dav\x00", ("127.0.0.1", self.tftp_port))
        resp, _ = s.recvfrom(2048)
        self.assertEqual(resp[:4], b"\x00\x05\x00\x00")
        self.assertEqual(resp.rstrip(b"\x00")[4:], b"No such file or directory")

    def test_wrq_opcode_is_ignored(self):
        s = self._socket(timeout=0.6)
        s.sendto(b"\x00\x02file.dav\x00", ("127.0.0.1", self.tftp_port))
        with self.assertRaises(socket.timeout):
            s.recvfrom(2048)

    def test_full_transfer_three_blocks(self):
        s = self._socket()
        s.sendto(b"\x00\x01digicap.dav\x00", ("127.0.0.1", self.tftp_port))
        received = bytearray()
        expected_block = 1
        while len(received) < len(self.FIRMWARE):
            data, addr = s.recvfrom(2048)
            self.assertEqual(data[:2], b"\x00\x03")  # DATA opcode
            block = (data[2] << 8) | data[3]
            self.assertEqual(block, expected_block)
            received += data[4:]
            s.sendto(b"\x00\x04" + block.to_bytes(2, "big"), addr)
            expected_block += 1
        self.assertEqual(bytes(received), self.FIRMWARE)
        self.assertEqual(expected_block, 4)  # blocks 1..3 served

        # Wait until the session thread fully wound down, then assert the
        # bootloop-prevention blacklist engaged for this client IP.
        deadline = time.monotonic() + 2
        while (
            time.monotonic() < deadline
            and "127.0.0.1" not in self.server._recently_flashed
        ):
            time.sleep(0.01)
        self.assertIn("127.0.0.1", self.server._recently_flashed)


class TestCliSmoke(unittest.TestCase):
    def test_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stdout)
        self.assertIn("info", proc.stdout)
        self.assertIn("push", proc.stdout)


if __name__ == "__main__":
    unittest.main()
