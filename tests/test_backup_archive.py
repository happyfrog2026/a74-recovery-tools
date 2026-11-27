"""Verify archive assembly using synthetic local data and read-only transport fakes."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import backup_archive, cli, snapshot, task_worker
from recovery_tool.task_backend import Backend, _restore_snapshot_folder
from recovery_tool.transport import PRODUCT, USBReadError


class OfflineBackupArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        regions = {region: 512 for region in snapshot.REGIONS}
        patches = (
            patch.object(snapshot, "REGIONS", regions),
            patch.object(Backend, "_transport", side_effect=AssertionError("bridge prohibited")),
        )
        for fixture in patches:
            fixture.start()
            self.addCleanup(fixture.stop)

    def captures(self, *, original_has_nor=True):
        original = self.root / "prior"
        original.mkdir()
        reads = {}
        manifest = {}
        for region in snapshot.REGIONS:
            if region == "cp-nor" and not original_has_nor:
                continue
            data = region.encode().ljust(512, b"\0")
            digest = hashlib.sha256(data).hexdigest()
            manifest[region] = {"bytes": len(data), "sha256": digest}
            reads[region] = {}
            for attempt, suffix in backup_archive.PASSES:
                file = f"{region}.{suffix}.bin"
                (original / file).write_bytes(data)
                reads[region][attempt] = {"file": file, **manifest[region],
                                          "checked_utc": "2026-11-19T10:00:00+00:00"}
        snapshot.save(original / "verified.json", manifest)
        snapshot.save(original / "capture-receipt.json", {
            "status": "INCOMPLETE", "started_utc": "2026-11-19T09:59:00+00:00",
            "reads": reads, "capture_regions": {region: 512 for region in reads},
            "region_coverage": {region: {"status": "VERIFIED"} for region in reads},
            "failure": {"anchor_mismatch": {"region": "nflasha", "offset": 0, "bytes": 512,
                                            "saved_sha256": manifest["nflasha"]["sha256"],
                                            "live_sha256": "a" * 64}},
        })
        emmc = self.root / "emmc"
        self.capture_region(original, emmc, "nflasha")
        return original, emmc

    def capture_region(self, original, output, expected_region):
        measured = []

        class Reader:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def verify_product(self):
                return PRODUCT

            def region_size(self, region):
                assert region == expected_region
                return 512

            def read_region(self, region, path, *, offset, length):
                assert region == expected_region
                measured.append(region)
                path.write_bytes(b"N" * length)
                return length

        result = Backend(transport_factory=Reader).backup_recapture(
            original, output, same_camera_confirmed=True, region=expected_region)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(measured, [expected_region, expected_region])

    def cli_command(self, backend, *args):
        """Use the public CLI and locked JSONL worker, with no real USB or mode exit."""
        original_handler = task_worker.handle

        def request(action, **fields):
            stream = io.StringIO()
            with (
                patch.object(task_worker, "APP_ROOT", self.root / "worker"),
                patch.object(task_worker, "handle", side_effect=lambda payload: original_handler(payload, backend)),
                patch.object(task_worker, "observe_normal_camera_usb", return_value=None),
                patch.object(task_worker, "return_normal", return_value={"reboot_sent": False}),
                patch.object(sys, "stdin", io.StringIO(json.dumps({"action": action, **fields}) + "\n")),
                redirect_stdout(stream),
            ):
                task_worker.main()
            final = json.loads(stream.getvalue().splitlines()[-1])
            return final.get("result", final)

        output = io.StringIO()
        with patch.object(cli, "worker_request", side_effect=request), redirect_stdout(output):
            code = cli.main([str(arg) for arg in args])
        return code, json.loads(output.getvalue())

    def resume_fixture(self, *, drift=False, pnp="present", interrupt_nor=True):
        original, _ = self.captures(original_has_nor=False)
        receipt = json.loads((original / "capture-receipt.json").read_text())
        receipt.pop("failure")
        snapshot.save(original / "capture-receipt.json", receipt)
        failed = False
        reads = []

        class USBError(OSError):
            backend_error_code = -9

        class Reader:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def verify_product(self):
                return PRODUCT

            def region_size(self, region):
                return snapshot.REGIONS[region]

            def read_region(self, region, output, *, offset=0, length=None):
                nonlocal failed
                reads.append((region, output.parent.name))
                if interrupt_nor and region == "cp-nor" and not failed:
                    failed = True
                    output.with_name(output.name + ".partial").write_bytes(b"interrupted")
                    raise USBReadError("fixture PIPE", USBError(32, "Pipe error"), stage="usb_bulk_read")
                data = (b"C" * 512 if drift and region == "nflasha"
                        else region.encode().ljust(512, b"\0"))
                output.write_bytes(data[offset:offset + length])
                return length

        backend = Backend(transport_factory=Reader, backup_fastboot_state=lambda: pnp)
        job = self.root / "task"
        backend.prepare("full_backup", {}, job)
        original = original.rename(job / "snapshot")
        return backend, job, original, reads

    def test_cli_windows_resume_reads_only_the_missing_region(self):
        backend, job, original, reads = self.resume_fixture()
        previous = (original / "nflasha.raw.bin").read_bytes()
        with patch.object(backend, "_transport", side_effect=lambda *_: backend.transport_factory()) as reconcile:
            code, result = self.cli_command(backend, "backup", "--resume", job)
        self.assertEqual((code, result["status"]), (0, "VERIFIED"))
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual((original / "nflasha.raw.bin").read_bytes(), previous)
        self.assertEqual([region for region, _ in reads], ["cp-nor"] * 3)
        receipt = json.loads((original / "capture-receipt.json").read_text())
        self.assertEqual(len(receipt["retry_events"]), 1)
        self.assertFalse(receipt["retry_events"][0]["reentry_requested"])

    def test_cli_windows_resume_preserves_verified_emmc_when_live_content_changed(self):
        backend, job, original, reads = self.resume_fixture(drift=True)
        preserved = {path: path.read_bytes() for path in original.glob("nflasha*.bin")}
        with patch.object(backend, "_transport", side_effect=lambda *_: backend.transport_factory()):
            code, result = self.cli_command(backend, "backup", "--resume", job)
        self.assertEqual((code, result["status"]), (0, "VERIFIED"))
        self.assertEqual([region for region, _ in reads], ["cp-nor"] * 3)
        for path, data in preserved.items():
            self.assertEqual(path.read_bytes(), data)

    def test_cli_recapture_missing_region_archive_and_corruption(self):
        backend, _, original, _ = self.resume_fixture(drift=True, interrupt_nor=False)
        # Independent archive reconstruction accepts a documented historical drift.
        # Normal backup resume now keeps verified pairs rather than producing this failure.
        receipt = json.loads((original / "capture-receipt.json").read_text())
        receipt["failure"] = {"anchor_mismatch": {
            "region": "nflasha", "offset": 0, "bytes": 512,
            "saved_sha256": receipt["reads"]["nflasha"]["first"]["sha256"],
            "live_sha256": hashlib.sha256(b"C" * 512).hexdigest(),
        }}
        snapshot.save(original / "capture-receipt.json", receipt)
        original_evidence = {path: path.read_bytes() for path in original.rglob("*") if path.is_file()}
        emmc, nor, archive = (self.root / name for name in ("new-emmc", "new-nor", "archive"))
        code, result = self.cli_command(backend, "backup-recapture", original, "--region", "nflasha",
                                        "--output", emmc, "--same-camera")
        self.assertEqual((code, result["status"], result["verified_regions"]), (2, "PARTIAL", ["nflasha"]))
        command = ("backup-archive", original, "--emmc", emmc, "--output", archive, "--same-camera")
        code, result = self.cli_command(backend, *command)
        self.assertEqual((code, result["status"], result["missing_regions"]), (1, "INCOMPLETE", ["cp-nor"]))
        self.assertFalse(archive.exists())
        code, result = self.cli_command(backend, "backup-recapture", original, "--region", "cp-nor",
                                        "--output", nor, "--same-camera")
        self.assertEqual((code, result["status"]), (2, "PARTIAL"))
        code, result = self.cli_command(backend, *command, "--supplement", nor)
        self.assertEqual((code, result["status"], result["restorable"]), (0, "VERIFIED", False))
        code, result = self.cli_command(backend, "backup-archive-verify", archive)
        self.assertEqual((code, result["status"]), (0, "VERIFIED"))
        for path, evidence in original_evidence.items():
            self.assertEqual(path.read_bytes(), evidence)
        (archive / "cp-nor.raw.bin").write_bytes(b"X" * 512)
        code, result = self.cli_command(backend, "backup-archive-verify", archive)
        self.assertEqual((code, result["status"]), (1, "INCOMPLETE"))

    def test_cli_windows_departure_reenters_and_retains_verified_pairs(self):
        backend, job, original, reads = self.resume_fixture(pnp="departed")
        preserved = {path: path.read_bytes() for path in original.glob("nflasha*.bin")}
        with patch.object(backend, "_transport", side_effect=lambda *_: backend.transport_factory()) as reconnect:
            code, result = self.cli_command(backend, "backup", "--resume", job)
        self.assertEqual((code, result["status"]), (0, "VERIFIED"))
        self.assertEqual(reconnect.call_count, 2)
        self.assertEqual([region for region, _ in reads], ["cp-nor"] * 3)
        receipt = json.loads((original / "capture-receipt.json").read_text())
        self.assertEqual(len(receipt["retry_events"]), 1)
        self.assertTrue(receipt["retry_events"][0]["reentry_requested"])
        self.assertEqual(receipt["retry_consecutive_used"], 0)
        for path, data in preserved.items():
            self.assertEqual(path.read_bytes(), data)

    def test_windows_saved_drift_does_not_block_resume_or_open_a_reader(self):
        backend, job, original, _ = self.resume_fixture(drift=True)
        backend.transport_factory = lambda: self.fail("documented drift inspection opened USB")
        ready = backend.backup_resume_status(job)
        self.assertEqual(ready["status"], "READY")
        receipt = json.loads((original / "capture-receipt.json").read_text())
        receipt["failure"] = {"anchor_mismatch": {"region": "nflasha", "offset": 0}}
        snapshot.save(original / "capture-receipt.json", receipt)
        ready = backend.backup_resume_status(job)
        self.assertEqual(ready["status"], "READY")
        self.assertNotIn("backup_anchor_mismatch", ready)
        self.assertEqual(json.loads((original / "capture-receipt.json").read_text()), receipt)

    def test_archive_keeps_both_passes_and_independent_provenance(self):
        original, emmc = self.captures()
        inputs = {path: path.read_bytes() for folder in (original, emmc)
                  for path in folder.rglob("*") if path.is_file()}
        output = self.root / "archive"
        result = backup_archive.assemble(original, emmc, [], output, same_camera_confirmed=True)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["complete_region_set"])
        self.assertFalse(result["restorable"])
        self.assertFalse(result["atomic_camera_snapshot"])
        self.assertEqual(Path(result["regions"]["nflasha"]["source"]), emmc)
        self.assertEqual(Path(result["regions"]["cp-nor"]["source"]), original)
        self.assertEqual((output / "nflasha.raw.bin").read_bytes(), b"N" * 512)
        self.assertEqual((output / "nflasha.repeat.bin").read_bytes(), b"N" * 512)
        for path, data in inputs.items():
            self.assertEqual(path.read_bytes(), data)
        with self.assertRaisesRegex(ValueError, "verified snapshot folder"):
            _restore_snapshot_folder(output)
        original.rename(self.root / "old-offline")
        emmc.rename(self.root / "emmc-offline")
        self.assertEqual(backup_archive.verify(output)["status"], "VERIFIED")
        (output / "nflasha.repeat.bin").write_bytes(b"X" * 512)
        with self.assertRaisesRegex(ValueError, "does not match"):
            backup_archive.verify(output)

    def test_missing_nor_requires_supplement_and_explicit_confirmation(self):
        original, emmc = self.captures(original_has_nor=False)
        output = self.root / "archive"
        with self.assertRaisesRegex(ValueError, "same physical camera"):
            backup_archive.assemble(original, emmc, [], output, same_camera_confirmed=False)
        missing = backup_archive.assemble(original, emmc, [], output, same_camera_confirmed=True)
        self.assertEqual(missing["missing_regions"], ["cp-nor"])
        self.assertFalse(missing["complete_region_set"])
        self.assertFalse(output.exists())
        nor = self.root / "nor"
        self.capture_region(original, nor, "cp-nor")
        result = backup_archive.assemble(original, emmc, [nor], output, same_camera_confirmed=True)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(Path(result["regions"]["cp-nor"]["source"]), nor)

    def test_archive_worker_is_offline_and_checks_copied_provenance(self):
        original, emmc = self.captures()
        output = self.root / "archive"
        backend = Backend(transport_factory=lambda: self.fail("archive opened a camera"))
        request = {"action": "backup_archive", "prior_snapshot": str(original),
                   "emmc_capture": str(emmc), "output_dir": str(output),
                   "same_camera_confirmed": True}
        for payload in (request, {"action": "backup_archive_verify", "archive_dir": str(output)}):
            stream = io.StringIO()
            with patch.object(task_worker, "return_normal", side_effect=AssertionError("reboot prohibited")), \
                 redirect_stdout(stream):
                task_worker.handle(payload, backend)
            result = json.loads(stream.getvalue().splitlines()[-1])["result"]
            self.assertEqual(result["status"], "VERIFIED")
        metadata = output / "source-receipts/nflasha/recapture.json"
        metadata.write_bytes(metadata.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "source metadata hash mismatch"):
            backup_archive.verify(output)

    def test_source_manifest_mismatch_and_duplicate_destination_stop_before_copy(self):
        original, emmc = self.captures()
        output = self.root / "archive"
        output.mkdir()
        (output / "sentinel.txt").write_text("preserve")
        with self.assertRaises(FileExistsError):
            backup_archive.assemble(original, emmc, [], output, same_camera_confirmed=True)
        self.assertEqual(list(output.iterdir()), [output / "sentinel.txt"])
        source = json.loads((original / "verified.json").read_text())
        source["cp-nor"]["sha256"] = "b" * 64
        snapshot.save(original / "verified.json", source)
        new_output = self.root / "new-archive"
        with self.assertRaisesRegex(ValueError, "double-read manifest mismatch"):
            backup_archive.assemble(original, emmc, [], new_output, same_camera_confirmed=True)
        self.assertFalse(new_output.exists())

    def test_damaged_original_pair_or_incomplete_recapture_is_not_certified(self):
        original, emmc = self.captures()
        output = self.root / "archive"
        file = original / "nflashaB1.repeat.bin"
        file.write_bytes(b"X" * 512)
        result = backup_archive.assemble(original, emmc, [], output, same_camera_confirmed=True)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["complete_region_set"])
        with self.assertRaisesRegex(ValueError, "no complete verified region set"):
            backup_archive.verify(output)
        provenance = json.loads((emmc / "recapture.json").read_text())
        provenance["status"] = "INCOMPLETE"
        snapshot.save(emmc / "recapture.json", provenance)
        with self.assertRaisesRegex(ValueError, "unverified or unrelated"):
            backup_archive.assemble(original, emmc, [], self.root / "another",
                                    same_camera_confirmed=True)
