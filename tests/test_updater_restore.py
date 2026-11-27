"""Offline sparse write simulation. No real USB backend is opened."""

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import updater_restore as restore
from recovery_tool.test_stock_sd import settings_fixture


def fixture():
    current = bytearray(8 * 512)
    current[:3] = b"\xeb\x3c\x90"
    current[510:512] = b"\x55\xaa"
    oem = bytearray(current)
    oem[512:1024] = b"A" * 512
    oem[2 * 512:3 * 512] = b"B" * 512
    oem[6 * 512:7 * 512] = b"C" * 512
    return bytes(current), bytes(oem)


def sector0():
    data = bytearray(512)
    data[:4] = b"8246"
    struct.pack_into("<4I", data, 32, 128, 8, 5, 1)
    return bytes(data)


class FakeBackend:
    def __init__(self):
        self.writes = []
        self.replies = []

    def write(self, data, timeout):
        self.writes.append(data)
        return len(data)

    def read(self, count, timeout):
        return self.replies.pop(0)


class FakeTransport:
    instances = []

    def __init__(self, current, target, fail_after=None, wrong_flash_info=False, settings=None):
        self.current, self.target = current, target
        self.settings = current if settings is None else settings
        self.backend = FakeBackend()
        self.commands = []
        self.fail_after = fail_after
        self.wrong_flash_info = wrong_flash_info
        self.flash_count = 0
        FakeTransport.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def region_size(self, region):
        assert region in ("nflasha1", "nflasha2")
        return len(self.settings if region == "nflasha2" else self.current)

    def read_region(self, region, output, **kwargs):
        Path(output).write_bytes(sector0() if region == "nflasha" else
                                 (self.settings if region == "nflasha2" else
                                  (self.current if self.flash_count == 0 else self.target)))

    def _send(self, command):
        self.commands.append(command)
        if command.startswith("download:"):
            self.backend.writes.clear()
        if command == "flash:nflasha1":
            self.flash_count += 1
            if self.fail_after and self.flash_count == self.fail_after:
                raise restore.RestoreError("simulated disconnect during flash")
            sparse = b"".join(self.backend.writes)
            _, _, _, header, chunk_header, _, _, count, _ = struct.unpack_from("<IHHHHIIII", sparse)
            pos, written = header, 0
            for _ in range(count):
                kind, _, sectors, size = struct.unpack_from("<HHII", sparse, pos)
                if kind == restore.RAW:
                    written += sectors
                pos += size
            reported = 0 if self.wrong_flash_info else written
            self.backend.replies = [f"INFOwrote {reported} sectors to nflasha1".encode(), b"OKAY"]

    def _reply(self, allowed=(b"OKAY",)):
        if allowed == (b"DATA",):
            command = self.commands[-1]
            return b"DATA", command.split(":")[1].encode()
        return b"OKAY", b""


class RestoreTests(unittest.TestCase):
    def scoped(self, current, oem):
        settings = settings_fixture(version=b"\x04\x06")
        return (patch.object(restore, "SIZE", len(current)),
                patch.object(restore, "CURRENT_SHA256", restore.sha256(current)),
                patch.object(restore, "OEM_SHA256", restore.sha256(oem)),
                patch.object(restore, "SDM2_SECTOR0_SHA256", restore.sha256(sector0())),
                patch.object(restore, "SETTINGS_SIZE", len(settings)),
                patch.object(restore, "SETTINGS_SHA256", restore.sha256(settings)))

    def test_sparse_replay_only_changes_target_sectors(self):
        current, oem = fixture()
        patches = self.scoped(current, oem)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            parts, plan = restore.build_plan(current, oem)
            self.assertEqual(plan["changed_sectors"], 3)
            self.assertEqual(len(parts), 2)
            self.assertEqual(restore.replay(current, parts), oem)
            self.assertTrue(all(len(part.data) <= restore.MAX_DOWNLOAD for part in parts))
            reverse_parts, reverse_plan = restore.build_plan(oem, current, reverse=True)
            self.assertEqual(reverse_plan["direction"], "reapply-reviewed-current")
            self.assertEqual(restore.replay(oem, reverse_parts), current)

    def test_settings_semantic_guard(self):
        baseline = settings_fixture(version=b"\x04\x06")
        live = bytearray(baseline)
        live[3584] ^= 1  # BACKUP.BIN data
        with patch.object(restore, "SETTINGS_SIZE", len(baseline)), \
             patch.object(restore, "SETTINGS_SHA256", restore.sha256(baseline)):
            result = restore.validate_settings(baseline, bytes(live))
            self.assertEqual(result["changed_bytes"], 1)
            live[4608] ^= 1  # UPDATER/DAT4 data
            with self.assertRaisesRegex(restore.RestoreError, "recorded version"):
                restore.validate_settings(baseline, bytes(live))

    def test_fresh_no_history_602_settings_guard(self):
        baseline = settings_fixture(version=b"\x02\x06")
        personal_change = bytearray(baseline)
        personal_change[3584] ^= 1
        with patch.object(restore, "SETTINGS_SIZE", len(baseline)):
            allowed = restore.validate_settings(
                baseline, bytes(personal_change),
                expected_sha=restore.sha256(baseline),
                expected_versions=("6.02", "6.04"))
            self.assertEqual(allowed["version"], "6.02")
            unknown_version = bytearray(baseline)
            unknown_version[4608] = 0x05
            with self.assertRaisesRegex(restore.RestoreError, "recorded version"):
                restore.validate_settings(
                    baseline, bytes(unknown_version),
                    expected_sha=restore.sha256(baseline),
                    expected_versions=("6.02", "6.04"))
            unknown_file = bytearray(baseline)
            unknown_file[4609] ^= 1
            with self.assertRaises(restore.RestoreError):
                restore.validate_settings(
                    baseline, bytes(unknown_file),
                    expected_sha=restore.sha256(baseline),
                    expected_versions=("6.02", "6.04"))

    def test_apply_readback_and_no_automatic_retry(self):
        current, oem = fixture()
        patches = self.scoped(current, oem)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, before = root / "oem.bin", root / "current.bin"
            settings = settings_fixture(version=b"\x04\x06")
            settings_path = root / "settings.bin"
            settings_path.write_bytes(settings)
            source.write_bytes(oem)
            before.write_bytes(current)
            target = root / "evidence"
            result = restore.apply(source, before, settings_path, target,
                                   transport_factory=lambda: FakeTransport(current, oem, settings=settings))
            self.assertEqual(result["status"], "VERIFIED")
            self.assertEqual(result["fragments_confirmed"], 2)
            self.assertEqual([c for c in FakeTransport.instances[-1].commands if c.startswith("flash:")],
                             ["flash:nflasha1", "flash:nflasha1"])
            failed = root / "failed"
            with self.assertRaisesRegex(restore.RestoreError, "simulated disconnect"):
                restore.apply(source, before, settings_path, failed,
                              transport_factory=lambda: FakeTransport(current, oem, fail_after=1, settings=settings))
            receipt = (failed / "restore-receipt.json").read_text()
            self.assertIn("UNKNOWN_AFTER_WRITE", receipt)
            self.assertIn('"fragments_confirmed": 0', receipt)
            self.assertFalse((failed / "updater.readback.bin").exists())
            wrong_info = root / "wrong-info"
            with self.assertRaisesRegex(restore.RestoreError, "confirm expected sector write"):
                restore.apply(source, before, settings_path, wrong_info,
                              transport_factory=lambda: FakeTransport(current, oem, wrong_flash_info=True, settings=settings))
            self.assertIn("UNKNOWN_AFTER_WRITE", (wrong_info / "restore-receipt.json").read_text())

    def test_live_mismatch_stops_before_download(self):
        current, oem = fixture()
        patches = self.scoped(current, oem)
        wrong = bytearray(current)
        wrong[4 * 512] = 1
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, before = root / "oem.bin", root / "current.bin"
            settings = settings_fixture(version=b"\x04\x06")
            settings_path = root / "settings.bin"
            settings_path.write_bytes(settings)
            source.write_bytes(oem)
            before.write_bytes(current)
            with self.assertRaisesRegex(restore.RestoreError, "live updater differs"):
                restore.apply(source, before, settings_path, root / "evidence",
                              transport_factory=lambda: FakeTransport(bytes(wrong), oem, settings=settings))
            self.assertEqual(FakeTransport.instances[-1].commands, [])
            self.assertIn("NO_WRITE", (root / "evidence" / "restore-receipt.json").read_text())


if __name__ == "__main__":
    unittest.main()
