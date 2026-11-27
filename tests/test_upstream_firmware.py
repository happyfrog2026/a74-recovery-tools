"""Public k8 resource decoder integrity checks with independently built frames."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[1]
KEY_HEADER = bytes.fromhex('e3b0c44298fc1c149afbf4c8996fb924')
KEY_K8 = bytes.fromhex('434589432d319010f1132727353286ce4d7ede8b9c4f7217a0e0226e0f0edc69')


def decode(source, *, output):
    # Exercise the public resource decoder in an isolated subprocess.
    code = """import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('stock_extractor',sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.decode(Path(sys.argv[2]),output=Path(sys.argv[3]))))
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(ROOT/'tools/upstream_firmware.py'),
                             str(source), str(output)], text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr)
    return json.loads(result.stdout)


def container(*, bad_frame=False, bad_header=False, bad_bounds=False):
    header = bytearray(512)
    header[:8] = b'UDTRFIRM'
    header[32:34] = b'\x05\x01'
    struct.pack_into('<I', header, 36, 0x01030081)
    struct.pack_into('<III', header, 48, 512, 4000 if bad_bounds else 2700, 0)
    struct.pack_into('<I', header, 8, zlib.crc32(header[12:]) ^ int(bad_header))
    payload = bytes(header) + b'A'*2700
    frames = bytearray()
    for offset in range(0, len(payload), 1020):
        content = payload[offset:offset+1020]
        flags = len(content) | (0x8000 if offset+1020 >= len(payload) else 0)
        body = struct.pack('<H', flags)+content.ljust(1020, b'\xff')
        checksum = sum(struct.unpack('<511H', body)) & 0xffff
        if bad_frame and offset >= 2040:
            checksum ^= 1
        frames.extend(struct.pack('<H', checksum)+body)
    iv = bytes(range(16))
    ecb = Cipher(algorithms.AES(KEY_HEADER), modes.ECB()).encryptor()
    cbc = Cipher(algorithms.AES(KEY_K8), modes.CBC(iv)).encryptor()
    encrypted = ecb.update(frames[:512])+cbc.update(frames[512:])
    fdat = encrypted+iv+bytes(256)
    raw = b'\x89UFU\r\n\x1a\n'+struct.pack('>I4sI', 4, b'DATV', 1)+struct.pack('>I4s', len(fdat), b'FDAT')+fdat
    return raw+struct.pack('>I4sI', 4, b'DEND', zlib.crc32(raw)), payload


class UpstreamFirmwareTest(unittest.TestCase):
    def test_k8_complete_frame_and_plaintext_proof(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".local") as folder:
            raw, payload = container()
            source, plain = Path(folder) / "source.DAT", Path(folder) / "plain.bin"
            source.write_bytes(raw)
            report = decode(source, output=plain)
            self.assertEqual(plain.read_bytes(), payload)
            self.assertEqual(report["plaintext_sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(report["plaintext_size"], len(payload))
            self.assertEqual(report["blocks_verified"], 4)
            self.assertFalse(report["camera_io"])
            self.assertFalse(report["rsa_verified"])

    def test_later_frame_and_plain_header_are_checked(self):
        for options, error in [({"bad_frame": True}, "Block checksum"),
                               ({"bad_header": True}, "Header CRC")]:
            with self.subTest(options=options), tempfile.TemporaryDirectory(dir=ROOT / ".local") as folder:
                raw, _ = container(**options)
                source, plain = Path(folder) / "source.DAT", Path(folder) / "plain.bin"
                source.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, error):
                    decode(source, output=plain)

    def test_outer_crc_is_mandatory(self):
        raw, _ = container()
        with tempfile.TemporaryDirectory(dir=ROOT / ".local") as folder:
            source = Path(folder) / "source.DAT"
            source.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            with self.assertRaisesRegex(ValueError, "DEND CRC"):
                decode(source, output=Path(folder) / "plain.bin")

    def test_plaintext_region_bounds_are_checked_after_valid_crc(self):
        raw, _ = container(bad_bounds=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".local") as folder:
            source = Path(folder) / "source.DAT"
            source.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "Invalid firmware bounds"):
                decode(source, output=Path(folder) / "plain.bin")


if __name__ == "__main__":
    unittest.main()
