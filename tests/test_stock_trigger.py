"""Offline one-shot settings trigger contract; no USB device is opened."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import stock_sd, stock_trigger
from recovery_tool.test_stock_sd import settings_fixture
from recovery_tool.updater_restore import RestoreError


class FakeTransport:
    instances = []

    def __init__(self, settings, updater, fail_flash=False):
        self.settings, self.updater = settings, updater
        self.fail_flash = fail_flash
        self.flashes = 0
        self.reboots = 0
        self.commands = []
        self.candidate = stock_sd.make_trigger(settings)[0]
        FakeTransport.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def region_size(self, region):
        return len(self.settings if region == "nflasha2" else self.updater)

    def read_region(self, region, output, **kwargs):
        if region == "nflasha":
            data = b"8246" + bytes(508)
        elif region == "nflasha1":
            data = self.updater
        else:
            data = self.candidate if self.flashes else self.settings
        Path(output).write_bytes(data)

    def _send(self, command):
        self.commands.append(command)
        if command == "reboot":
            self.reboots += 1

    def _reply(self):
        return b"OKAY", b""


class TriggerTests(unittest.TestCase):
    def test_recorded_400_uses_structure_not_numeric_gate(self):
        settings = settings_fixture(b"\x00\x04")
        updater = b"reviewed-stock-updater"
        with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
             patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}):
            fragment, candidate, report = stock_trigger.build(
                settings, updater, expected_updater_sha256=stock_trigger.sha256(updater))
        self.assertEqual(report["plan"]["current"], "4.00")
        self.assertEqual(report["plan"]["admission_basis"],
                         "validated FAT settings structure and pinned updater environment")
        self.assertEqual(stock_trigger.replay(settings, [fragment]), candidate)

    def test_recorded_400_still_rejects_conflicting_update_mode(self):
        settings = bytearray(settings_fixture(b"\x00\x04"))
        settings[4128:4139] = b"MODE1      "
        settings[4139] = 0x20
        updater = b"reviewed-stock-updater"
        with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
             patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}):
            with self.assertRaisesRegex(Exception, "Another update mode"):
                stock_trigger.build(bytes(settings), updater,
                                    expected_updater_sha256=stock_trigger.sha256(updater))

    def test_zero_recorded_version_recreates_missing_flags_with_dynamic_scope(self):
        settings = settings_fixture(b"\0\0")
        updater = b"reviewed-stock-updater"
        with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
             patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}):
            fragment, candidate, report = stock_trigger.build(
                settings, updater, expected_updater_sha256=stock_trigger.sha256(updater))
        self.assertEqual(report["plan"]["current"], "0.00")
        self.assertEqual(report["plan"]["created_flags"], ["MODE", "MODE5"])
        self.assertEqual(report["sparse_sectors"], [8])
        self.assertEqual(stock_trigger.replay(settings, [fragment]), candidate)

    def test_zero_recorded_version_with_flags_present_is_safe_noop(self):
        settings = settings_fixture(b"\0\0")
        with patch.object(stock_sd, "SETTINGS_SIZE", len(settings)):
            triggered, _ = stock_sd.make_trigger(settings)
            unchanged, plan = stock_sd.make_trigger(triggered)
        self.assertEqual(unchanged, triggered)
        self.assertEqual(plan["changed_bytes"], 0)
        updater = b"reviewed-stock-updater"
        with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
             patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}):
            fragment, candidate, report = stock_trigger.build(
                triggered, updater, expected_updater_sha256=stock_trigger.sha256(updater))
        self.assertIsNone(fragment)
        self.assertEqual(candidate, triggered)
        self.assertEqual(report["sparse_sectors"], [])

    def test_zero_version_does_not_override_structural_checks(self):
        settings = bytearray(settings_fixture(b"\0\0"))
        settings[4128:4139] = b"MODE1      "
        settings[4139] = 0x20
        updater = b"reviewed-stock-updater"
        with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
             patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}):
            with self.assertRaisesRegex(Exception, "Another update mode"):
                stock_trigger.build(bytes(settings), updater,
                                    expected_updater_sha256=stock_trigger.sha256(updater))

    def test_sd_ready_build_reports_unseen_card_file(self):
        settings = settings_fixture(b"\x02\x06")
        updater = b"personalized-stock-updater"
        with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
             patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
             patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}):
            _, _, plan = stock_trigger.build(settings, updater, expected_updater_sha256=stock_trigger.sha256(updater))
        self.assertIsNone(plan["host_dat_sha256"])
        self.assertFalse(plan["sd_slot_1_file_verified_by_host"])

    def test_pinned_reconstructed_updater_hash_can_differ_from_legacy_oem(self):
        settings = settings_fixture(b"\x02\x06")
        updater = b"personalized-stock-updater"
        dat = b"reviewed-sony-dat"
        with tempfile.TemporaryDirectory() as folder:
            dat_path = Path(folder) / "BODYDATA.DAT"
            dat_path.write_bytes(dat)
            with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
                 patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
                 patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
                 patch.object(stock_trigger, "inspect_package", return_value={"sha256": stock_trigger.sha256(dat), "version": "6.02"}), \
                 patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}):
                expected = stock_trigger.sha256(updater)
                stock_trigger.build(settings, updater, dat_path,
                                    expected_updater_sha256=expected)
                with self.assertRaisesRegex(RestoreError, "pinned OEM"):
                    stock_trigger.build(settings, updater, dat_path,
                                        expected_updater_sha256="wrong-hash")

    def test_build_and_one_shot_apply(self):
        settings = settings_fixture(b"\x04\x06")
        updater = b"reviewed-stock-updater"
        dat = b"reviewed-sony-dat"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dat_path = root / "BODYDATA.DAT"
            dat_path.write_bytes(dat)
            with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
                 patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
                 patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
                 patch.object(stock_trigger, "OEM_SHA256", stock_trigger.sha256(updater)), \
                 patch.object(stock_trigger, "inspect_package", return_value={"sha256": stock_trigger.sha256(dat), "version": "6.02"}), \
                 patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}), \
                 patch.object(stock_trigger, "_verify_sdm2", return_value=None), \
                 patch.object(stock_trigger, "_download_flash") as flash:
                def action(transport, part, region):
                    self.assertEqual(region, "nflasha2")
                    transport.flashes += 1
                flash.side_effect = action
                fragment, candidate, plan = stock_trigger.build(settings, updater, dat_path)
                self.assertEqual(plan["sparse_sectors"], [8, 9])
                self.assertEqual(stock_trigger.replay(settings, [fragment]), candidate)
                result = stock_trigger.apply(dat_path, root / "live",
                                             transport_factory=lambda: FakeTransport(settings, updater))
                self.assertEqual(result["status"], "REBOOT_ACKNOWLEDGED")
                self.assertEqual(FakeTransport.instances[-1].reboots, 1)
                self.assertEqual(FakeTransport.instances[-1].flashes, 1)
                self.assertEqual((root / "live" / "settings.readback.bin").read_bytes(), candidate)

    def test_failure_after_flash_never_reboots(self):
        settings = settings_fixture(b"\x04\x06")
        updater = b"reviewed-stock-updater"
        dat = b"reviewed-sony-dat"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dat_path = root / "BODYDATA.DAT"
            dat_path.write_bytes(dat)
            with patch.object(stock_trigger, "SETTINGS_SIZE", len(settings)), \
                 patch.object(stock_sd, "SETTINGS_SIZE", len(settings)), \
                 patch.object(stock_trigger, "UPDATER_SIZE", len(updater)), \
                 patch.object(stock_trigger, "OEM_SHA256", stock_trigger.sha256(updater)), \
                 patch.object(stock_trigger, "inspect_package", return_value={"sha256": stock_trigger.sha256(dat), "version": "6.02"}), \
                 patch.object(stock_trigger, "inspect_sd_environment", return_value={"stock": True}), \
                 patch.object(stock_trigger, "_verify_sdm2", return_value=None), \
                 patch.object(stock_trigger, "_download_flash", side_effect=RestoreError("simulated failure")):
                with self.assertRaisesRegex(RestoreError, "simulated failure"):
                    stock_trigger.apply(dat_path, root / "live",
                                        transport_factory=lambda: FakeTransport(settings, updater))
                self.assertEqual(FakeTransport.instances[-1].reboots, 0)
                self.assertIn("UNKNOWN_AFTER_WRITE", (root / "live" / "trigger-receipt.json").read_text())


if __name__ == "__main__":
    unittest.main()
