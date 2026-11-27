"""Temporary-volume tests for the optional SD staging action; no device access."""

import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import sd_card_prepare


class SdCardPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.card = self.root / "Volumes" / "SD CARD"
        self.card.mkdir(parents=True)
        self.source = self.root / "Sony-602.DAT"
        self.source.write_bytes(b"older DAT payload with arbitrary bytes")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        root_patch = patch.object(sd_card_prepare, "VOLUME_ROOT", self.card.parent)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.inspector = lambda _volume: "disk-fixture-1"

    def call(self, **kwargs):
        return sd_card_prepare.prepare(self.source, self.card,
                                       volume_inspector=self.inspector, **kwargs)

    def test_official_catalog_identifies_model_and_version_with_exact_size(self):
        for digest, package in sd_card_prepare.CATALOG["packages"].items():
            meta = sd_card_prepare.package_metadata(digest, package["bytes"])
            self.assertEqual((meta["model"], meta["version"]), ("ILCE-7M4", package["version"]))
        versions = set()
        for digest, (version, size) in sd_card_prepare.RM5_PACKAGES.items():
            self.assertEqual(sd_card_prepare.package_metadata(digest, size),
                             {"status": "catalog_match", "model": "ILCE-7RM5", "version": version})
            self.assertIsNone(sd_card_prepare.package_metadata(digest, size - 1)["model"])
            versions.add(version)
        self.assertEqual(versions, {"2.00", "2.01", "2.02", "3.00", "3.01", "4.00"})

    def test_archive_catalog_covers_every_available_dat_model_and_version(self):
        expected = {
            "ILCE-7M4": "1.05 1.10 1.11 2.00 2.01 3.00 3.01 3.02 4.00 5.00 5.01 6.00 6.01 6.02",
            "ILCE-7RM5": "2.00 2.01 2.02 3.00 3.01 4.00",
            "ILCE-7SM3": "3.02 4.00 5.00 5.01",
            "ILCE-9M3": "2.00 2.01 3.00 3.01 4.00",
            "ILCE-1": "2.02 3.00 3.01 4.00",
            "ILCE-1M2": "2.00 2.01 3.00 4.00",
            "ILME-FX2": "1.01", "ILME-FX3": "4.00 5.00 6.00 6.02 7.00 7.01 7.02",
            "ILME-FX3A": "2.00 2.01 2.02",
            "ILME-FX30": "1.02 1.05 2.00 3.00 4.00 5.00 5.02 6.00 6.01",
            "ILME-FX6": "2.00 4.00 5.01 5.02 6.02",
        }
        actual = {}
        for digest, entry in sd_card_prepare.SD_PACKAGES.items():
            actual.setdefault(entry["model"], set()).add(entry["version"])
            self.assertEqual(sd_card_prepare.package_metadata(digest, entry["bytes"]),
                             {"status": "catalog_match", "model": entry["model"],
                              "version": entry["version"]})
            self.assertIsNone(sd_card_prepare.package_metadata(digest, entry["bytes"] + 1)["model"])
        self.assertEqual(actual, {model: set(versions.split()) for model, versions in expected.items()})
        self.assertEqual(len(sd_card_prepare.SD_PACKAGES), 62)
        self.assertIsNone(sd_card_prepare.package_metadata("0" * 64, 100)["model"])

    def test_additional_models_stage_only_matching_catalog_identity(self):
        for model in sd_card_prepare.SD_CAMERA_MODELS:
            with self.subTest(model=model), patch.dict(sd_card_prepare.SD_PACKAGES, {
                    self.digest: {"model": model, "version": "1.00",
                                  "bytes": self.source.stat().st_size}}):
                target = self.card / "BODYDATA.DAT"
                target.unlink(missing_ok=True)
                other = "ILCE-7RM5" if model == "ILCE-7M4" else "ILCE-7M4"
                self.assertEqual(self.call(camera_model=other)["status"], "MODEL_MISMATCH")
                self.assertFalse(target.exists())
                for status in ("VERIFIED", "ALREADY_PRESENT"):
                    result = self.call(camera_model=model)
                    self.assertEqual(result["status"], status)
                    self.assertTrue(result["next_action"].endswith(
                        "slot B" if model == "ILME-FX6" else "slot 1"))
                self.assertEqual(target.read_bytes(), self.source.read_bytes())

    def test_known_other_model_keeps_card_contents_even_with_replace_requested(self):
        target = self.card / "BODYDATA.DAT"
        target.write_bytes(b"existing package")
        with patch.dict(sd_card_prepare.RM5_PACKAGES,
                        {self.digest: ("2.01", self.source.stat().st_size)}):
            result = self.call(camera_model="ILCE-7M4", confirm_replace=True,
                               receipt_dir=self.root / "receipts")
            self.assertEqual(result["status"], "MODEL_MISMATCH")
            self.assertEqual(target.read_bytes(), b"existing package")
            self.assertEqual(json.loads(Path(result["receipt_path"]).read_text())["dat_metadata"]["model"],
                             "ILCE-7RM5")
            target.unlink()
            self.assertEqual(self.call(camera_model="ILCE-7RM5")["status"], "VERIFIED")
            self.assertEqual(self.call(camera_model="ILCE-7RM5")["status"], "ALREADY_PRESENT")

    def test_builtin_sd_reader_is_accepted_but_disk_image_is_rejected(self):
        from types import SimpleNamespace
        card = self.card.resolve()
        info = {"MountPoint": str(card), "BusProtocol": "Secure Digital",
                "Internal": True, "RemovableMediaOrExternalDevice": True,
                "Ejectable": True, "WritableVolume": True,
                "DeviceIdentifier": "disk6s1"}
        with patch("recovery_tool.sd_card_prepare.subprocess.run",
                   return_value=SimpleNamespace(stdout=plistlib.dumps(info))):
            self.assertEqual(sd_card_prepare._inspect_volume(card), "disk6s1")
        info["BusProtocol"] = "Disk Image"
        with patch("recovery_tool.sd_card_prepare.subprocess.run",
                   return_value=SimpleNamespace(stdout=plistlib.dumps(info))):
            with self.assertRaisesRegex(ValueError, "mounted SD or USB"):
                sd_card_prepare._inspect_volume(card)

    def test_arbitrary_nonempty_dat_atomic_copy_and_idempotent_readback(self):
        result = self.call()
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual((self.card / "BODYDATA.DAT").read_bytes(), self.source.read_bytes())
        self.assertEqual(result["source_dat_sha256"], self.digest)
        self.assertFalse(result["sd_slot_1_presence_verified"])
        self.assertEqual(self.call()["status"], "ALREADY_PRESENT")
        self.assertFalse(list(self.card.glob("*.tmp-*")))

    def test_persistent_receipt_records_source_volume_and_final_card_readback(self):
        result = self.call(receipt_dir=self.root / "receipts")
        saved = json.loads(Path(result["receipt_path"]).read_text())
        self.assertEqual(saved["schema"], "sd-card-prepare-v1")
        self.assertEqual(saved["status"], "VERIFIED")
        self.assertEqual(saved["source_path"], str(self.source.resolve()))
        self.assertEqual(saved["source_bytes"], self.source.stat().st_size)
        self.assertEqual(saved["source_sha256"], self.digest)
        self.assertEqual(saved["target_path"], str(self.card.resolve() / "BODYDATA.DAT"))
        self.assertEqual(saved["target_bytes"], self.source.stat().st_size)
        self.assertEqual(saved["target_sha256"], self.digest)
        self.assertEqual(saved["volume_identity"], {"device_identifier": "disk-fixture-1",
                                                    "mount_point": str(self.card.resolve())})
        self.assertEqual(saved["dat_metadata"]["status"], "not_parsed")
        self.assertFalse(saved["sd_slot_1_presence_verified"])

    def test_different_card_file_requires_pinned_confirmation_then_replaces_old(self):
        old = b"older firmware"
        (self.card / "BODYDATA.DAT").write_bytes(old)
        preview = self.call()
        self.assertEqual(preview["status"], "NEEDS_CONFIRMATION")
        self.assertEqual((self.card / "BODYDATA.DAT").read_bytes(), old)
        with self.assertRaisesRegex(ValueError, "changed since confirmation"):
            self.call(confirm_replace=True, expected_existing_sha256="wrong",
                      expected_volume_device_identifier=preview["volume_device_identifier"])
        result = self.call(confirm_replace=True,
                           expected_existing_sha256=preview["existing_sha256"],
                           expected_volume_device_identifier=preview["volume_device_identifier"])
        self.assertEqual(result["status"], "VERIFIED")
        self.assertNotIn("preserved_path", result)
        self.assertEqual((self.card / "BODYDATA.DAT").read_bytes(), self.source.read_bytes())
        self.assertEqual(sorted(p.name for p in self.card.iterdir()), ["BODYDATA.DAT"])

    def test_copy_failure_keeps_existing_file_and_removes_temporary(self):
        old = b"older firmware"
        (self.card / "BODYDATA.DAT").write_bytes(old)
        preview = self.call()
        with patch.object(sd_card_prepare, "_copy_verified", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
                self.call(confirm_replace=True,
                          expected_existing_sha256=preview["existing_sha256"],
                          expected_volume_device_identifier=preview["volume_device_identifier"])
        self.assertEqual((self.card / "BODYDATA.DAT").read_bytes(), old)
        self.assertEqual(sorted(p.name for p in self.card.iterdir()), ["BODYDATA.DAT"])

    def test_failure_receipt_does_not_claim_target_after_replace_without_readback(self):
        old = b"old card bytes"
        target = self.card / "BODYDATA.DAT"
        target.write_bytes(old)
        preview = self.call()
        original_hash = sd_card_prepare.file_sha256
        def fail_final_readback(path):
            if Path(path).name == "BODYDATA.DAT" and Path(path).read_bytes() == self.source.read_bytes():
                return "bad-final-readback"
            return original_hash(path)
        receipts = self.root / "receipts"
        with patch.object(sd_card_prepare, "file_sha256", side_effect=fail_final_readback):
            with self.assertRaisesRegex(ValueError, "readback differs"):
                self.call(confirm_replace=True,
                          expected_existing_sha256=preview["existing_sha256"],
                          expected_volume_device_identifier=preview["volume_device_identifier"],
                          receipt_dir=receipts)
        saved = json.loads(next(receipts.glob("*.json")).read_text())
        self.assertEqual(saved["status"], "FAILED")
        self.assertTrue(saved["copy_replaced"])
        self.assertFalse(saved["readback_verified"])
        self.assertIsNone(saved["target_sha256"])
        self.assertIsNone(saved["target_bytes"])
        self.assertIsNone(saved["card_dat_sha256"])
        self.assertEqual(saved["existing_sha256"], hashlib.sha256(old).hexdigest())

    def test_wrong_source_or_changed_volume_stops_before_write(self):
        self.source.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "nonempty DAT"):
            self.call()
        self.assertFalse((self.card / "BODYDATA.DAT").exists())
        self.source.write_bytes(b"older DAT payload with arbitrary bytes")
        (self.card / "BODYDATA.DAT").write_bytes(b"older firmware")
        preview = self.call()
        with self.assertRaisesRegex(ValueError, "changed since confirmation"):
            self.call(confirm_replace=True, expected_existing_sha256=preview["existing_sha256"],
                      expected_volume_device_identifier="different-disk")
        self.assertEqual((self.card / "BODYDATA.DAT").read_bytes(), b"older firmware")

    def test_corrupted_staged_copy_rejected_without_replacing_existing(self):
        old = b"older card bytes"
        (self.card / "BODYDATA.DAT").write_bytes(old)
        preview = self.call()
        def corrupt_copy(_source, output, *, length):
            output.write(b"corrupted transfer")
        with patch.object(sd_card_prepare.shutil, "copyfileobj", side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, "Copied file differs"):
                self.call(confirm_replace=True,
                          expected_existing_sha256=preview["existing_sha256"],
                          expected_volume_device_identifier=preview["volume_device_identifier"])
        self.assertEqual((self.card / "BODYDATA.DAT").read_bytes(), old)
        self.assertEqual(sorted(p.name for p in self.card.iterdir()), ["BODYDATA.DAT"])

    def test_source_is_target_is_idempotent_without_temp_or_write(self):
        target = self.card / "BODYDATA.DAT"
        target.write_bytes(b"existing DAT")
        result = sd_card_prepare.prepare(target, self.card, volume_inspector=self.inspector)
        self.assertEqual(result["status"], "ALREADY_PRESENT")
        self.assertEqual(target.read_bytes(), b"existing DAT")
        self.assertEqual(sorted(p.name for p in self.card.iterdir()), ["BODYDATA.DAT"])

    def test_differently_named_dat_on_same_card_can_be_source(self):
        source_on_card = self.card / "BODYDATA (5).DAT"
        source_on_card.write_bytes(b"older source payload")
        result = sd_card_prepare.prepare(source_on_card, self.card,
                                         volume_inspector=self.inspector)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual((self.card / "BODYDATA.DAT").read_bytes(), source_on_card.read_bytes())
        self.assertEqual(sorted(p.name for p in self.card.iterdir()),
                         ["BODYDATA (5).DAT", "BODYDATA.DAT"])


if __name__ == "__main__":
    unittest.main()
