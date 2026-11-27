"""Synthetic, device-free checks for the stock SD recovery planner."""

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from . import stock_sd
from .stock_common import StockRecoveryError
from .stock_fat import Fat


def settings_fixture(version=b"\x02\x06"):
    image = bytearray(128 * 512)
    image[:3] = b"\xeb\x3c\x90"
    struct.pack_into("<HBHBHHBHH", image, 11, 512, 1, 1, 2, 64, 128, 0xF8, 1, 32)
    image[510:512] = b"\x55\xaa"
    fat = bytearray(512)
    fat[:3] = b"\xf8\xff\xff"
    for cluster in (2, 3, 4):
        offset = cluster * 3 // 2
        value = int.from_bytes(fat[offset:offset + 2], "little")
        value = (value & 0xF) | 0xFFF0 if cluster & 1 else (value & 0xF000) | 0xFFF
        fat[offset:offset + 2] = value.to_bytes(2, "little")
    image[512:1024] = image[1024:1536] = fat

    def entry(offset, name, attr, cluster, size):
        image[offset:offset + 11] = name
        image[offset + 11] = attr
        struct.pack_into("<H", image, offset + 26, cluster)
        struct.pack_into("<I", image, offset + 28, size)

    entry(1536, b"BACKUP  BIN", 0x20, 2, 128)
    entry(1568, b"UPDATER    ", 0x10, 3, 0)
    image[3584:3712] = b"synthetic-personalization".ljust(128, b"\0")
    entry(4096, b"DAT4       ", 0x20, 4, 2)
    image[4608:4610] = version
    return bytes(image)


class StockSdOfflineTests(unittest.TestCase):
    def test_candidate_has_only_declared_changes_and_preserves_personalization(self):
        original = settings_fixture()
        with patch.object(stock_sd, "SETTINGS_SIZE", len(original)):
            candidate, plan = stock_sd.make_trigger(original)
        changed = [i for i, (before, after) in enumerate(zip(original, candidate)) if before != after]
        self.assertEqual(changed, plan["changed_offsets"])
        self.assertEqual(plan["created_flags"], ["MODE", "MODE5"])
        self.assertEqual(plan["current"], "6.02")
        self.assertEqual(Fat(candidate).read("updater/dat4"), b"\0\0")
        self.assertEqual(Fat(candidate).read("Backup.bin"), Fat(original).read("Backup.bin"))
        self.assertEqual(candidate[512:3584], original[512:3584])

    def test_other_update_mode_is_rejected(self):
        original = bytearray(settings_fixture())
        original[4128:4139] = b"MODE1      "
        original[4139] = 0x20
        with patch.object(stock_sd, "SETTINGS_SIZE", len(original)):
            with self.assertRaisesRegex(StockRecoveryError, "Another update mode"):
                stock_sd.make_trigger(bytes(original))

    def test_dry_run_requires_matching_card_and_never_writes_candidate(self):
        original = settings_fixture()
        dat = b"synthetic stock DAT"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings, updater, sd = [root / name for name in ("settings.bin", "updater.bin", "BODYDATA.DAT")]
            settings.write_bytes(original)
            updater.write_bytes(b"synthetic updater")
            sd.write_bytes(dat)
            with patch.object(stock_sd, "SETTINGS_SIZE", len(original)), \
                 patch.object(stock_sd, "inspect_package", return_value={"sha256": hashlib.sha256(dat).hexdigest(), "version": "6.02"}), \
                 patch.object(stock_sd, "inspect_sd_environment", return_value={"updater_environment_known": True}):
                result = stock_sd.dry_run(settings, updater, sd)
            self.assertEqual(result["status"], "OFFLINE_PLAN_ONLY")
            self.assertFalse(result["camera_write"] or result["camera_reboot"])
            self.assertFalse(result["sd_slot_1_presence_verified"])
            self.assertEqual(sorted(path.name for path in root.iterdir()),
                             ["BODYDATA.DAT", "settings.bin", "updater.bin"])

    def test_wrong_card_is_rejected_before_settings_processing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sd = root / "BODYDATA.DAT"
            sd.write_bytes(b"wrong DAT")
            with self.assertRaisesRegex(StockRecoveryError, "not one of the verified official packages"):
                stock_sd.dry_run(root / "missing-settings", root / "missing-updater", sd)

    def test_recorded_version_uses_updater_and_settings_structure_not_numeric_allowlist(self):
        dat = b"synthetic stock DAT"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings, updater, sd = [root / name for name in ("settings.bin", "updater.bin", "BODYDATA.DAT")]
            updater.write_bytes(b"synthetic updater")
            sd.write_bytes(dat)
            with patch.object(stock_sd, "SETTINGS_SIZE", len(settings_fixture())), \
                 patch.object(stock_sd, "inspect_package", return_value={"sha256": hashlib.sha256(dat).hexdigest(), "version": "6.02"}):
                settings.write_bytes(settings_fixture(b"\x04\x06"))
                with patch.object(stock_sd, "inspect_sd_environment", side_effect=StockRecoveryError("UPDATER_PROFILE", "custom updater")):
                    with self.assertRaisesRegex(StockRecoveryError, "custom updater"):
                        stock_sd.dry_run(settings, updater, sd)
                with patch.object(stock_sd, "inspect_sd_environment", return_value={"updater_environment_known": True}):
                    self.assertEqual(stock_sd.dry_run(settings, updater, sd)["plan"]["current"], "6.04")
                    settings.write_bytes(settings_fixture(b"\x03\x06"))
                    self.assertEqual(stock_sd.dry_run(settings, updater, sd)["plan"]["current"], "6.03")
                    settings.write_bytes(settings_fixture(b"\x00\x04"))
                    self.assertEqual(stock_sd.dry_run(settings, updater, sd)["plan"]["current"], "4.00")


if __name__ == "__main__":
    unittest.main()
