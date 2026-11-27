"""Offline GUI contract checks; no serial or USB device is opened."""

from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from recovery_tool.task_backend import Backend, _check_manifest_pin, SD_READY_PLAN_DIGEST
from recovery_tool.task_worker import handle
from recovery_tool.transport import (FastbootError, FastbootEnumerationUnavailable,
                                     FastbootIdentityError, USBReadError)
from recovery_tool import write_transaction as tx
from recovery_tool.stock_common import StockRecoveryError
from test_scoped_restore_plan import same_body_fixture


class BackendContract(unittest.TestCase):


    def test_capability_restore_nor_only_is_explicit_partial_success_without_flash(self):
        target = self.root / "target"
        same_body_fixture(target)
        backend = Backend(transport_factory=lambda: self.fail("camera hardware prohibited"))
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"):
            backend.prepare("scoped_restore", {"target_snapshot": str(target),
                                               "restore_profile": "capability"}, self.root / "job")
        def capture(folder, **_):
            same_body_fixture(folder, nor_change=True)
        with patch("recovery_tool.task_backend.snapshot.capture", capture), \
             patch("recovery_tool.task_backend.decrypt_snapshot.decrypt"), \
             patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"), \
             patch.object(Backend, "apply", side_effect=AssertionError("flash prohibited")):
            result = backend.restore_run(self.root / "job")
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["success_with_skips"])
        self.assertFalse(result["fully_matches_snapshot"])
        self.assertEqual(result["restored_regions"], [])
        self.assertEqual(result["skipped_regions"][0]["name"], "cp-nor")
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"):
            checked = backend.verify(self.root / "job")
        self.assertEqual(checked["status"], "VERIFIED")
        self.assertTrue(checked["success_with_skips"])

    def test_verified_capture_keeps_unreset_retry_count_for_host_phase(self):
        target, job = self.root / "retry-target", self.root / "retry-job"
        same_body_fixture(target)
        backend = Backend(transport_factory=lambda: self.fail("no USB expected"))
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"):
            backend.prepare("scoped_restore", {"target_snapshot": str(target),
                                               "restore_profile": "capability"}, job)
        current = job / "current-snapshot"
        same_body_fixture(current)
        receipt_path = current / "capture-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["retry_consecutive_used"] = 2
        tx.durable_json(receipt_path, receipt)
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"):
            result = backend.restore_run(job)
        self.assertEqual(result["status"], "VERIFIED")
        policy = json.loads((job / "restore-session-policy.json").read_text())
        self.assertEqual(policy["retries_used"], 2)

    def test_restore_accepts_snapshot_folder_and_immediate_parent(self):
        task_folder = self.root / "full_backup-task"
        snapshot_folder = task_folder / "snapshot"
        (snapshot_folder / "decrypted").mkdir(parents=True)
        for name in ("verified.json", "capture-receipt.json", "decrypted/manifest.json"):
            (snapshot_folder / name).write_text("{}")
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot") as verify:
            for label, selected in (("direct", snapshot_folder), ("parent", task_folder)):
                job = self.root / f"restore-{label}"
                ready = Backend().prepare("scoped_restore", {"target_snapshot": str(selected)}, job)
                self.assertEqual(ready["status"], "READY")
                task = json.loads((job / "task.json").read_text())
                self.assertEqual(task["inputs"]["target_snapshot"], str(snapshot_folder.resolve()))
            self.assertEqual(verify.call_count, 2)

    def test_restore_rejects_invalid_or_ambiguous_folder_before_verification(self):
        invalid = self.root / "unrelated"
        invalid.mkdir()
        ambiguous = self.root / "ambiguous"
        (ambiguous / "snapshot").mkdir(parents=True)
        (ambiguous / "verified.json").write_text("{}")
        (ambiguous / "snapshot/verified.json").write_text("{}")
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot") as verify:
            with self.assertRaisesRegex(ValueError, "verified snapshot folder or its immediate parent"):
                Backend().prepare("scoped_restore", {"target_snapshot": str(invalid)}, self.root / "bad")
            with self.assertRaisesRegex(ValueError, "Both selected folder and its snapshot child"):
                Backend().prepare("scoped_restore", {"target_snapshot": str(ambiguous)}, self.root / "ambiguous-job")
            verify.assert_not_called()

    def test_backup_guard_result_reports_current_checkpoint_without_historical_blockers(self):
        from recovery_tool import snapshot
        job = self.root / "guarded-backup"
        backend = Backend(transport_factory=lambda: self.fail("hardware prohibited"))
        backend.prepare("full_backup", {}, job)
        failure = snapshot.CaptureFailure(
            origin="safety_guard", region="cp-nor", attempt="second",
            offset=16_777_216, path=job / "snapshot/segments/cp-nor.second/0001000000.bin",
            cause=RuntimeError("automatic RAM reentry blocked by current backup checkpoint"))
        receipt = job / "snapshot/capture-receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(json.dumps({"retry_events": [{"retry": 1}, {"retry": 2}],
                                       "retry_consecutive_used": 1}))
        blockers = [{"job_dir": str(self.root / "prior-stock"),
                     "reason": "REBOOT_ACKNOWLEDGED"}]
        with patch("recovery_tool.task_backend.snapshot.capture", side_effect=failure), \
             patch("recovery_tool.card_connection._blocking_tasks", return_value=blockers):
            result = backend.apply(job)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertNotIn("blocking_tasks", result)
        self.assertEqual((result["backup_retry_used"], result["backup_retry_total"],
                          result["backup_retry_limit"]), (1, 2, 5))
        self.assertEqual(result["backup_failure_origin"], "safety_guard")

    def test_windows_full_backup_passes_guarded_reentry_and_pnp_to_capture(self):
        job = self.root / "windows-direct-backup"
        backend = Backend(transport_factory=lambda: self.fail("no camera USB expected"),
                          backup_fastboot_state=lambda: "present")
        backend.prepare("full_backup", {}, job)
        with patch.object(Backend, "_transport", side_effect=AssertionError("bridge prohibited")), \
             patch("recovery_tool.task_backend.snapshot.capture",
                   return_value={"status": "VERIFIED", "capture_regions": {"nflashaB0": 512}}) as capture, \
             patch("recovery_tool.task_backend.decrypt_snapshot.decrypt",
                   return_value={"status": "VERIFIED", "decryption_status": "UNSUPPORTED",
                                 "partitions": [], "decryption_error": "user area unavailable"}):
            result = backend.apply(job)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertIs(capture.call_args.kwargs["transport_factory"], backend.transport_factory)
        self.assertTrue(callable(capture.call_args.kwargs["reentry_factory"]))
        self.assertEqual(capture.call_args.kwargs["backup_fastboot_state"](), "present")

    def test_windows_fresh_restore_observes_old_uncertain_write_without_replay(self):
        from recovery_tool import card_connection, nor_rebuild_flow
        target = self.root / "saved"
        same_body_fixture(target)
        old = self.root / "scoped_restore-old"
        stage = old / "scoped-stage"
        stage.mkdir(parents=True)
        tx.durable_json(old / "task.json", {"kind": "scoped_restore"})
        journal = {"status": "WRITE_INFLIGHT", "inflight": {"position": 2}}
        tx.durable_json(stage / "journal.json", journal)
        job = self.root / "scoped_restore-new"
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"):
            Backend().prepare("scoped_restore", {"target_snapshot": str(target)}, job)
        with patch.object(card_connection, "TASK_ROOT", self.root / "unused"), \
             patch.object(card_connection, "TASK_REGISTRY", self.root / "unused-registry.json"), \
             patch.object(Backend, "_transport") as camera, \
             patch.object(tx, "apply_with_reconnect") as write, \
             patch.object(nor_rebuild_flow, "admission", side_effect=RuntimeError("test capture boundary")) as next_step:
            for normal, pnp in ((None, "departed"), ({"status": "MSC"}, "present")):
                backend = Backend(backup_fastboot_state=lambda: pnp)
                with patch("recovery_tool.task_backend.observe_normal_camera_usb", return_value=normal):
                    result = backend.restore_run(job)
                self.assertEqual(result["status"], "NEEDS_USER_ACTION")
                self.assertTrue(result["restore_previous_pending"])
                self.assertFalse(result["write_may_have_occurred"])
                next_step.assert_not_called()
            backend = Backend(backup_fastboot_state=lambda: "departed")
            with patch("recovery_tool.task_backend.observe_normal_camera_usb", return_value={"status": "MSC"}):
                backend.restore_run(job)
            next_step.assert_called_once()
            self.assertTrue((job / "prior-restore-observation.json").is_file())
        camera.assert_not_called()
        write.assert_not_called()
        self.assertEqual(json.loads((stage / "journal.json").read_text()), journal)

    def test_manual_backup_resume_checks_live_anchor_and_persists_drift(self):
        from contextlib import nullcontext
        from recovery_tool import snapshot
        job = self.root / "resume-drift"
        reader = object()
        backend = Backend(transport_factory=lambda: nullcontext(reader))
        backend.prepare("full_backup", {}, job)
        folder = job / "snapshot"
        folder.mkdir()
        receipt = {"status": "INCOMPLETE", "segments": {"nflasha": {"first": [{"offset": 0}]}}}
        snapshot.save(folder / "capture-receipt.json", receipt)
        mismatch = snapshot.BackupAnchorMismatch(
            {"region": "nflasha", "offset": 0, "bytes": 512, "sha256": "a" * 64}, "b" * 64)
        with patch.object(snapshot, "_verify_resume_identity", side_effect=mismatch) as verify, \
             patch.object(snapshot, "capture") as capture:
            result = backend.apply(job)
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[:2], (reader, folder.resolve()))
        self.assertEqual(verify.call_args.args[2]["segments"], receipt["segments"])
        capture.assert_not_called()
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["backup_anchor_mismatch"], mismatch.evidence)
        saved = json.loads((folder / "capture-receipt.json").read_text())
        self.assertEqual(saved["failure"]["anchor_mismatch"], mismatch.evidence)

    def test_full_backup_success_keeps_structured_retry_fields(self):
        job = self.root / "successful-backup"
        backend = Backend(transport_factory=lambda: self.fail("no camera USB expected"))
        backend.prepare("full_backup", {}, job)
        with patch("recovery_tool.task_backend.snapshot.capture",
                   return_value={"status": "VERIFIED", "capture_regions": {"nflashaB0": 512}, "retry_consecutive_used": 1,
                                 "retry_events": [{"retry": 1}, {"retry": 2}]}), \
             patch("recovery_tool.task_backend.decrypt_snapshot.decrypt", return_value={"status": "VERIFIED", "decryption_status": "UNSUPPORTED", "partitions": [], "decryption_error": "user area unavailable"}) as decrypt:
            result = backend.apply(job)
        decrypt.assert_called_once()
        self.assertEqual(decrypt.call_args.args[0].resolve(), (job / "snapshot").resolve())
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual((result["backup_retry_used"], result["backup_retry_total"],
                          result["backup_retry_limit"]), (1, 2, 5))


    def test_backup_camera_usb_failure_origin_and_remaining_budget(self):
        from recovery_tool import snapshot
        job = self.root / "usb-backup"
        backend = Backend(transport_factory=lambda: self.fail("hardware prohibited"))
        backend.prepare("full_backup", {}, job)
        failure = snapshot.CaptureFailure(
            origin="camera_usb_read", region="cp-nor", attempt="first", offset=33_554_432,
            path=job / "snapshot/segments/cp-nor.first/segment.bin",
            cause=RuntimeError("camera USB bulk read failed: [Errno None] Other error"))
        receipt = job / "snapshot/capture-receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(json.dumps({"retry_events": [{"retry": 1}, {"retry": 2}]}))
        with patch("recovery_tool.task_backend.snapshot.capture", side_effect=failure):
            result = backend.apply(job)
        self.assertEqual(result["backup_failure_origin"], "camera_usb_read")
        self.assertEqual((result["backup_retry_used"], result["backup_retry_limit"]), (2, 5))
        self.assertNotIn("blocking_tasks", result)

    def test_backup_retry_uses_current_checkpoint_despite_historical_unknown(self):
        from contextlib import nullcontext
        from recovery_tool import snapshot
        job = self.root / "fresh-backup"
        backend = Backend(transport_factory=nullcontext)
        backend.prepare("full_backup", {}, job)
        folder = job / "snapshot"
        folder.mkdir()
        verified, reads = {}, {}
        for name in ("nflashaB0", "nflashaB1"):
            data = (name.encode() + bytes(512))[:512]
            digest = hashlib.sha256(data).hexdigest()
            verified[name] = {"bytes": 512, "sha256": digest}
            for suffix in ("raw", "repeat"):
                (folder / f"{name}.{suffix}.bin").write_bytes(data)
            reads[name] = {attempt: {"file": f"{name}.{suffix}.bin", "bytes": 512,
                                     "sha256": digest}
                           for attempt, suffix in (("first", "raw"), ("second", "repeat"))}
        segment = folder / "first-user.bin"
        segment.write_bytes(bytes(512))
        tx.durable_json(folder / "verified.json", verified)
        tx.durable_json(folder / "capture-receipt.json", {
            "status": "IN_PROGRESS", "reads": reads,
            "segments": {"nflasha": {"first": [{"offset": 0, "bytes": 512,
                                                    "file": segment.name,
                                                    "sha256": tx.digest(segment)}]}}})
        prior = self.root / "old-restore"
        prior.mkdir()
        (prior / "scoped-stage").mkdir()
        tx.durable_json(prior / "task.json", {"kind": "scoped_restore"})
        tx.durable_json(prior / "scoped-stage/journal.json", {
            "status": "UNKNOWN_AFTER_WRITE", "write_may_have_occurred": True})
        calls = []
        def fake_capture(_folder, **kwargs):
            with kwargs["reentry_factory"]():
                pass
            return {"retry_consecutive_used": 1, "retry_events": [{"retry": 1}]}
        with patch.object(snapshot, "_verify_resume_identity"), \
             patch.dict(snapshot.REGIONS, {"nflashaB0": 512, "nflashaB1": 512}), \
             patch.object(snapshot, "DEFAULT_CHUNK_BYTES", 512), \
             patch.object(backend, "_transport", side_effect=lambda *_: (calls.append(1), nullcontext())[1]), \
             patch("recovery_tool.task_backend.snapshot.capture", side_effect=fake_capture), \
             patch("recovery_tool.task_backend.decrypt_snapshot.decrypt"):
            result = backend.apply(job)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["backup_retry_used"], 1)
        self.assertEqual(calls, [1])
        (folder / "nflashaB0.raw.bin").write_bytes(b"tampered")
        with patch.object(snapshot, "_verify_resume_identity"), \
             patch.dict(snapshot.REGIONS, {"nflashaB0": 512, "nflashaB1": 512}), \
             patch.object(snapshot, "DEFAULT_CHUNK_BYTES", 512), \
             patch.object(backend, "_transport", side_effect=AssertionError("must not reset")), \
             patch("recovery_tool.task_backend.snapshot.capture", side_effect=fake_capture):
            refused = backend.apply(job)
        self.assertEqual(refused["status"], "INCOMPLETE")
        self.assertIn("current backup checkpoint", refused["summary"])


    def test_stock_ack_time_uses_durable_field_or_legacy_journal_mtime(self):
        from recovery_tool.task_backend import _stock_ack_time
        journal = self.root / "legacy-ack.json"
        tx.durable_json(journal, {"status": "REBOOT_ACKNOWLEDGED"})
        legacy = _stock_ack_time(journal, {"status": "REBOOT_ACKNOWLEDGED"})
        self.assertEqual(legacy["stock_reboot_timestamp_source"], "journal_mtime")
        self.assertTrue(legacy["stock_reboot_acknowledged_utc"].endswith("+00:00"))
        pinned = _stock_ack_time(journal, {"status": "REBOOT_ACKNOWLEDGED",
                                           "reboot_acknowledged_utc": "2026-11-15T01:01:05+00:00"})
        self.assertEqual(pinned["stock_reboot_acknowledged_utc"], "2026-11-15T01:01:05+00:00")
        self.assertEqual(pinned["stock_reboot_timestamp_source"], "journal_field")
        self.assertEqual(_stock_ack_time(journal, {"status": "REBOOT_UNCERTAIN"}), {})

    def test_stale_stock_preimage_requires_new_task_without_write(self):
        job = self.root / "stale-stock"
        job.mkdir()
        tx.durable_json(job / "task.json", {"kind": "stock_sd_recovery",
                                           "composite_plan_digest": "pinned-plan"})
        tx.durable_json(job / "journal.json", {"status": "NO_WRITE",
                                              "schema": "write-transaction-state-v1",
                                              "write_may_have_occurred": False,
                                              "reboot_may_have_occurred": False,
                                              "confirmed_fragments": 0})
        tx.durable_json(job / "stock-authorization.json", {"plan_digest": "pinned-plan"})
        backend = Backend(transport_factory=lambda: self.fail("camera must not open"))
        with patch.object(backend, "_composite_stock_plan", return_value={
                 "updater_manifest_sha256": None}), \
             patch("recovery_tool.task_backend._check_manifest_pin"), \
             patch("recovery_tool.task_backend.tx.apply",
                   side_effect=ValueError("unrecognized live sectors in nflasha2: 9")) as apply:
            result = backend.stock_run(job)
        self.assertEqual(result["status"], "NO_WRITE")
        self.assertTrue(result["stale_plan"])
        self.assertIn("new stock task", result["next_action"])
        self.assertFalse(json.loads((job / "journal.json").read_text())["write_may_have_occurred"])
        apply.assert_called_once()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Restore entry now checks the global task registry before camera
        # access. Synthetic contract jobs must see only this isolated fixture.
        for name, value in (("TASK_ROOT", self.root / "Tasks"),
                            ("TASK_REGISTRY", self.root / "registry.json")):
            isolated = patch(f"recovery_tool.card_connection.{name}", value)
            isolated.start()
            self.addCleanup(isolated.stop)
        # Contract fixtures below use synthetic captures without a Fastboot
        # sector-zero file. Session ownership/probing has dedicated tests.
        class LocalOnlySession:
            def run_host(self, work):
                from threading import Event
                return work(Event(), lambda _event: None)
        session_patch = patch.object(Backend, "_restore_idle_session",
                                     return_value=LocalOnlySession())
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def test_board_prepare_result_and_worker_json_lines(self):
        uf2 = self.root / "bridge.uf2"
        uf2.write_bytes(b"reviewed")
        backend = Backend()
        with patch("recovery_tool.task_backend.board_install.validate_uf2",
                   return_value={"path": str(uf2), "sha256": "abc", "bytes": 8, "blocks": 1}):
            output = io.StringIO()
            with redirect_stdout(output):
                handle({"action": "prepare", "kind": "board_install",
                        "inputs": {"uf2": str(uf2)},
                        "output_dir": str(self.root / "job")}, backend)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[0]["type"], "progress")
        self.assertEqual(lines[-1]["type"], "result")
        result = lines[-1]["result"]
        self.assertEqual(result["kind"], "board_install")
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["requires_confirmation"])
        for key in ("job_dir", "summary", "next_action", "plan_digest", "capability_verified"):
            self.assertIn(key, result)

    def test_stock_apply_requires_read_only_candidate_first(self):
        job = self.root / "stock"
        job.mkdir()
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "stock_sd_recovery", "inputs": {"dat": str(self.root / "dat")},
                        "plan_digest": "dat-hash"})
        backend = Backend(transport_factory=lambda: self.fail("hardware opened"))
        result = backend.apply(job)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertTrue((job / "task.json").exists())

    def test_stock_sd_ready_prepare_requires_explicit_confirmation_without_local_dat(self):
        backend = Backend(transport_factory=lambda: self.fail("hardware opened"))
        with self.assertRaisesRegex(ValueError, "Confirm official Sony"):
            backend.prepare("stock_sd_recovery", {}, self.root / "unconfirmed")
        result = backend.prepare("stock_sd_recovery", {"sd_ready": True}, self.root / "ready")
        self.assertEqual(result["status"], "READY")
        task = json.loads((self.root / "ready/task.json").read_text())
        self.assertEqual(task["inputs"], {"sd_ready": True})

    def test_optional_sd_copy_receipt_is_pinned_without_requiring_one(self):
        receipt = self.root / "sd-copy.json"
        tx.durable_json(receipt, {"schema": "sd-card-prepare-v1", "status": "VERIFIED",
                                  "source_sha256": "a" * 64, "target_sha256": "a" * 64,
                                  "source_bytes": 123, "target_bytes": 123,
                                  "readback_verified": True,
                                  "volume_identity": {"device_identifier": "disk-fixture-1",
                                                      "mount_point": "/Volumes/Fixture"}})
        job = self.root / "with-receipt"
        Backend().prepare("stock_sd_recovery", {"sd_ready": True,
                         "sd_preparation_receipt": str(receipt)}, job)
        task = json.loads((job / "task.json").read_text())
        self.assertEqual(task["sd_preparation_evidence"]["receipt_sha256"], tx.digest(receipt))
        self.assertFalse(task["sd_preparation_evidence"]["sd_slot_1_presence_verified"])
        with self.assertRaisesRegex(ValueError, "not a verified byte match"):
            bad = self.root / "bad-copy.json"
            tx.durable_json(bad, {"schema": "sd-card-prepare-v1", "status": "FAILED"})
            Backend().prepare("stock_sd_recovery", {"sd_ready": True,
                             "sd_preparation_receipt": str(bad)}, self.root / "bad-receipt")
        self.assertEqual(task["plan_digest"], SD_READY_PLAN_DIGEST)

    def test_stock_sd_ready_candidate_pins_live_plan_without_dat_file(self):
        backend = Backend(transport_factory=lambda: self.fail("hardware opened"))
        job = self.root / "ready"
        backend.prepare("stock_sd_recovery", {"sd_ready": True}, job)

        class FakeTransport:
            def read_region(self, name, path, *, length=None, **_):
                Path(path).write_bytes({"nflasha": b"M" * 512,
                                        "nflasha1": b"U" * 512,
                                        "nflasha2": b"S" * 1024}[name])

        def candidate(settings, updater, dat_path, **_):
            self.assertIsNone(dat_path)
            return b"sparse", b"S" * 512 + b"T" * 512, {
                "candidate_sha256": hashlib.sha256(b"S" * 512 + b"T" * 512).hexdigest(),
                "sparse_sectors": [1],
                "sd_slot_1_file_verified_by_host": False}

        with patch("recovery_tool.task_backend.verify_stock_sdm2"), \
             patch("recovery_tool.task_backend.build_stock_trigger", side_effect=candidate), \
             patch("recovery_tool.task_backend.UPDATER_SIZE", 512), \
             patch("recovery_tool.task_backend.OEM_SHA256", hashlib.sha256(b"U" * 512).hexdigest()):
            backend._stock_transaction(job, json.loads((job / "task.json").read_text()), FakeTransport())
        manifest = json.loads((job / "manifest.json").read_text())
        self.assertEqual(len(manifest["input_files"]), 1)
        self.assertTrue(manifest["metadata"]["sd_slot_1_user_confirmed"])
        self.assertFalse(manifest["metadata"]["sd_slot_1_file_verified_by_host"])
        _check_manifest_pin(job, json.loads((job / "task.json").read_text()))
        self.assertEqual(json.loads((job / "task.json").read_text())["plan_digest"],
                         tx.digest(job / "manifest.json"))

    def test_sd_ready_reconstructs_from_bundled_assets_without_dat_pin(self):
        backend = Backend(transport_factory=lambda: self.fail("hardware opened"))
        job = self.root / "ready"
        backend.prepare("stock_sd_recovery", {"sd_ready": True}, job)

        class FakeTransport:
            def read_region(self, name, path, *, length=None, **_):
                Path(path).write_bytes({"nflasha": b"M" * 512,
                                        "nflasha1": b"E" * 512,
                                        "nflasha2": b"S" * 1024}[name])

        source_module = ModuleType("recovery_tool.stock_updater_source")
        seen = []
        def reconstruct(live, dat):
            seen.append((live, dat))
            return b"O" * 512, {"source": "bundled", "dat_sha256": None}
        source_module.reconstruct = reconstruct
        with patch.dict(sys.modules, {source_module.__name__: source_module}), \
             patch("recovery_tool.task_backend.verify_stock_sdm2"), \
             patch("recovery_tool.task_backend.UPDATER_SIZE", 512), \
             patch("recovery_tool.task_backend.OEM_SHA256", "different-oem"), \
             patch("recovery_tool.task_backend.inspect_sd_environment", side_effect=[StockRecoveryError("UPDATER_PROFILE", "custom fixture"), {"stock": True}]), \
             patch("recovery_tool.task_backend.validate_settings", return_value={}), \
             patch("recovery_tool.task_backend.tx.reconcile", return_value={"status": "READY"}):
            result = backend._stock_updater_stage(job, json.loads((job / "task.json").read_text()),
                                                  FakeTransport(), None)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(seen, [(b"E" * 512, None)])
        manifest = json.loads((job / "updater-stage/manifest.json").read_text())
        self.assertEqual(len(manifest["input_files"]), 1)
        self.assertIsNone(manifest["metadata"]["source_proof"]["dat_sha256"])


    def test_latest_real_settings_flag_slots_cross_sector_boundary(self):
        saved = (Path.home() / "Library/Application Support/Camera Recovery/Tasks"
                 / "stock_sd_recovery-2026-11-14T23-42-41Z-A45C4A/stock-inputs/attempt-001")
        files = {"nflasha": saved / "sector0.bin", "nflasha1": saved / "updater.bin",
                 "nflasha2": saved / "settings.bin"}
        if not all(path.is_file() for path in files.values()):
            self.skipTest("latest private read-only task unavailable")

        class SavedReadTransport:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def region_size(self, name): return files[name].stat().st_size
            def read_region(self, name, path, *, length=None, offset=0, **_):
                with files[name].open("rb") as source, Path(path).open("wb") as output:
                    source.seek(offset)
                    output.write(source.read(length))
                return Path(path).stat().st_size

        backend = Backend(transport_factory=SavedReadTransport)
        job = self.root / "latest-boundary"
        backend.prepare("stock_sd_recovery", {"sd_ready": True}, job)
        result = backend.stock_plan(job)
        self.assertEqual(result["status"], "READY")
        self.assertIn("sectors 133, 134, 197", result["summary"])
        self.assertFalse((job / "updater-stage/manifest.json").exists())
        plan = json.loads((job / "stock-composite-plan.json").read_text())
        self.assertEqual(plan["settings_sectors"], [133, 134, 197])
        self.assertEqual(plan["settings_changed_sectors"], 3)
        self.assertEqual(json.loads((job / "manifest.json").read_text())["regions"][0]["scope"],
                         [[133, 135], [197, 198]])
        self.assertFalse((job / "stock-authorization.json").exists())

    def test_board_reconcile_runtime_ping_skips_write_confirmation(self):
        job = self.root / "board"
        job.mkdir()
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "board_install", "inputs": {}, "plan_digest": "uf2-hash"})

        class PingBridge:
            def __init__(self, _port):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def ping(self):
                return b"uart_boot 1"

        with patch("recovery_tool.task_backend.board_install.inspect",
                   return_value={"ports": ["/dev/cu.fake"], "bootsel": []}), \
             patch("recovery_tool.task_backend.Bridge", PingBridge):
            result = Backend().reconcile(job)
        self.assertEqual(result["status"], "ALREADY_INSTALLED")
        self.assertFalse(result["requires_confirmation"])
        self.assertTrue(result["control_ping_verified"])

    def test_board_reconcile_bootsel_plus_runtime_port_is_ambiguous_without_ping(self):
        job = self.root / "board-mixed"
        job.mkdir()
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "board_install", "inputs": {}, "plan_digest": "uf2-hash"})
        with patch("recovery_tool.task_backend.board_install.inspect",
                   return_value={"ports": ["/dev/cu.fake"],
                                 "bootsel": [{"volume": "/Volumes/RPI-RP2"}]}), \
             patch("recovery_tool.task_backend.Bridge",
                   side_effect=AssertionError("Mixed candidates must not PING")):
            result = Backend().reconcile(job)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertFalse(result["control_ping_verified"])

    def test_board_reconcile_unrecognized_runtime_guides_manual_bootsel(self):
        job = self.root / "board-unrecognized"
        job.mkdir()
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "board_install", "inputs": {}, "plan_digest": "uf2-hash"})

        class OtherFirmware:
            def __init__(self, _port):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def ping(self):
                return b"other firmware"

        with patch("recovery_tool.task_backend.board_install.inspect",
                   return_value={"ports": ["/dev/cu.fake"], "bootsel": []}), \
             patch("recovery_tool.task_backend.Bridge", OtherFirmware):
            result = Backend().reconcile(job)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertIn("did not answer known bridge PING", result["summary"])
        self.assertFalse(result["control_ping_verified"])

    def test_board_reconcile_multiple_bootsel_volumes_exposes_manual_selection(self):
        job = self.root / "board-multiple"
        job.mkdir()
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "board_install", "inputs": {}, "plan_digest": "uf2-hash"})
        with patch("recovery_tool.task_backend.board_install.inspect",
                   return_value={"ports": [], "bootsel": ["/Volumes/RPI-RP2",
                                                         "/Volumes/RPI-RP2 1"]}):
            result = Backend(transport_factory=lambda: self.fail("USB prohibited")).reconcile(job)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertIn("Multiple", result["summary"])
        self.assertFalse(result["requires_confirmation"])

    def test_reboot_uncertainty_prevents_reconcile_hardware(self):
        job = self.root / "stock"
        job.mkdir()
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "stock_sd_recovery", "inputs": {}, "plan_digest": "dat-hash"})
        tx.durable_json(job / "journal.json", {"reboot_may_have_occurred": True,
                                                "status": "REBOOT_UNCERTAIN"})
        backend = Backend(transport_factory=lambda: self.fail("hardware opened"))
        result = backend.reconcile(job)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")

    def test_relocated_verified_snapshot_paths_keep_supported_capability(self):
        current, target = self.root / "after", self.root / "before"
        same_body_fixture(current, changed_slots=(2, 5))
        same_body_fixture(target)
        result = Backend().prepare("scoped_restore",
                    {"current_snapshot": str(current), "target_snapshot": str(target)},
                    self.root / "task")
        self.assertEqual(result["status"], "READY")
        manifest = json.loads((self.root / "task/manifest.json").read_text())
        self.assertEqual(manifest["metadata"]["validation_mode"], "fragment-readback-v1")
        self.assertEqual([row["name"] for row in manifest["regions"]], ["nflasha2", "nflasha5"])
        for row in manifest["regions"]:
            self.assertEqual(Path(row["target"]).parent, (target / "decrypted").resolve())
            self.assertEqual(row["target_sha256"], tx.digest(Path(row["target"])))

    def test_worker_quarantines_legacy_capture_prints(self):
        class NoisyBackend:
            def apply(self, job_dir, progress):
                print("nflasha first: reading 64 MiB")
                progress({"stage": "capture", "message": "segment saved"})
                return {"kind": "full_backup", "job_dir": str(job_dir),
                        "status": "VERIFIED", "summary": "done",
                        "next_action": "review", "requires_confirmation": False,
                        "plan_digest": None, "capability_verified": True}

        protocol, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(protocol), redirect_stderr(diagnostics):
            handle({"action": "apply", "job_dir": str(self.root / "job")}, NoisyBackend())
        lines = [json.loads(line) for line in protocol.getvalue().splitlines()]
        self.assertEqual([line["type"] for line in lines], ["progress", "result"])
        self.assertIn("nflasha first", diagnostics.getvalue())

    def test_connection_probe_keeps_bootsel_and_data_independent(self):
        class Data:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def verify_product(self): return "ILCE-7M4"
            def read_region(self, region, path, *, length):
                self_region = region
                assert self_region == "nflasha" and length == 512
                Path(path).write_bytes(b"S" * 512)
                return 512
        with patch("recovery_tool.task_backend.list_ports", return_value=[]), \
             patch("recovery_tool.task_backend.board_install.bootsel_volumes",
                   return_value=[{"volume": "/Volumes/RPI-RP2"}]), \
             patch("recovery_tool.task_backend.Bridge") as bridge:
            result = Backend(transport_factory=Data).connection_probe()
        self.assertEqual(result["control"]["status"], "BOOTSEL")
        self.assertEqual(result["data"]["status"], "READY")
        self.assertEqual(result["data"]["mode"], "FASTBOOT")
        self.assertEqual(result["data"]["short_read_bytes"], 512)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertIn("Board Install", result["next_action"])
        bridge.assert_not_called()

    def test_connection_probe_pings_board_without_reset_or_fastboot_reentry(self):
        class PingBridge:
            def __init__(self, port): self.port = port
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def ping(self): return b"uart_boot 1"
            def reset(self): raise AssertionError("camera reset prohibited")
        def missing_data(): raise FastbootError("ILCE-7M4 RAM Fastboot USB device unavailable")
        with patch("recovery_tool.task_backend.list_ports", return_value=["/dev/cu.fake"]), \
             patch("recovery_tool.task_backend.board_install.bootsel_volumes", return_value=[]), \
             patch("recovery_tool.task_backend.observe_normal_camera_usb", return_value=None), \
             patch("recovery_tool.task_backend.Bridge", PingBridge):
            backend = Backend(transport_factory=missing_data)
            backend._transport = lambda *_: self.fail("bridge boot prohibited")
            output = io.StringIO()
            with redirect_stdout(output):
                handle({"action": "connection_probe"}, backend)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        result = lines[-1]["result"]
        self.assertEqual(result["control"]["status"], "READY")
        self.assertEqual(result["control"]["ping"], "uart_boot 1")
        self.assertEqual(result["data"]["status"], "UNAVAILABLE")
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")

    def test_connection_probe_distinguishes_normal_msc_from_fastboot(self):
        class PingBridge:
            def __init__(self, port): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def ping(self): return b"uart_boot 1"
            def reset(self): raise AssertionError("camera reset prohibited")
        def missing_fastboot():
            raise FastbootError("ILCE-7M4 RAM Fastboot USB device unavailable")
        normal = {"status": "MSC", "product": "ILCE-7M4", "vid": 0x054c,
                  "pid": 0x0da5, "interface_classes": [8]}
        with patch("recovery_tool.task_backend.list_ports", return_value=["/dev/cu.fake"]), \
             patch("recovery_tool.task_backend.board_install.bootsel_volumes", return_value=[]), \
             patch("recovery_tool.task_backend.Bridge", PingBridge), \
             patch("recovery_tool.task_backend.observe_normal_camera_usb", return_value=normal):
            result = Backend(transport_factory=missing_fastboot).connection_probe()
        self.assertEqual(result["control"]["status"], "READY")
        self.assertEqual(result["data"]["status"], "MSC")
        self.assertTrue(result["data"]["camera_usb_detected"])
        self.assertFalse(result["data"]["fastboot_usb_enumerated"])
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertIn("normal USB is detected", result["next_action"])

    def test_connection_probe_requires_fastboot_short_read(self):
        class Data:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def verify_product(self): return "ILCE-7M4"
            def read_region(self, region, path, *, length):
                Path(path).write_bytes(b"short")
                return 5
        with patch("recovery_tool.task_backend.list_ports", return_value=[]), \
             patch("recovery_tool.task_backend.board_install.bootsel_volumes", return_value=[]):
            result = Backend(transport_factory=Data).connection_probe()
        self.assertEqual(result["data"]["status"], "ERROR")
        self.assertNotEqual(result["status"], "READY")

    def test_connection_ram_entry_requires_explicit_confirmation_and_observed_msc(self):
        backend = Backend(transport_factory=lambda: self.fail("real USB prohibited"))
        with patch.object(backend, "connection_probe", side_effect=AssertionError("unconfirmed probe")):
            self.assertEqual(backend.connection_enter_ram()["status"], "NO_WRITE")
        before = {"kind": "connection_probe", "status": "NEEDS_USER_ACTION",
                  "control": {"status": "READY"}, "data": {"status": "MSC"}}
        after = {"kind": "connection_probe", "status": "READY",
                 "control": {"status": "READY"}, "data": {"status": "READY"}}
        class Entered:
            def __enter__(self): return self
            def __exit__(self, *_): pass
        with patch.object(backend, "connection_probe", side_effect=[before, after]) as probe, \
             patch.object(backend, "_transport", return_value=Entered()) as entry:
            result = backend.connection_enter_ram(confirmed=True)
        self.assertEqual(result, after)
        self.assertEqual(probe.call_count, 2)
        entry.assert_called_once()

    def test_connection_ram_entry_rejects_unverified_control_and_worker_false_confirmation(self):
        backend = Backend(transport_factory=lambda: self.fail("real USB prohibited"))
        before = {"kind": "connection_probe", "status": "NEEDS_USER_ACTION",
                  "control": {"status": "ERROR"}, "data": {"status": "MSC"}}
        with patch.object(backend, "connection_probe", return_value=before), \
             patch.object(backend, "_transport", side_effect=AssertionError("camera reset prohibited")):
            self.assertEqual(backend.connection_enter_ram(confirmed=True), before)
        output = io.StringIO()
        with redirect_stdout(output), patch.object(backend, "connection_probe",
                                            side_effect=AssertionError("unconfirmed probe")):
            handle({"action": "connection_enter_ram", "confirmed": False}, backend)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["result"]["status"], "NO_WRITE")

    def test_connection_ram_entry_recovers_when_normal_usb_never_enumerates(self):
        backend = Backend(transport_factory=lambda: self.fail("real USB prohibited"))
        before = {"kind": "connection_probe", "status": "NEEDS_USER_ACTION",
                  "control": {"status": "READY"}, "data": {"status": "UNAVAILABLE"}}
        after = {"kind": "connection_probe", "status": "READY",
                 "control": {"status": "READY"}, "data": {"status": "READY"}}
        class Entered:
            def __enter__(self): return self
            def __exit__(self, *_): pass
        with patch.object(backend, "connection_probe", side_effect=[before, after]), \
             patch.object(backend, "_transport", return_value=Entered()) as entry:
            result = backend.connection_enter_ram(confirmed=True)
        self.assertEqual(result, after)
        entry.assert_called_once()

    def test_uart_ready_without_data_raises_typed_enumeration_timeout(self):
        backend = Backend(transport_factory=lambda: self.fail("deadline already expired"))
        payload = self.root / "payload.bin"
        payload.write_bytes(b"mock payload")
        clock = iter([0, 11])
        with patch("recovery_tool.task_backend.Bridge") as bridge, \
             patch("recovery_tool.task_backend._BridgeDiagnostic"), \
             patch("recovery_tool.task_backend.time.monotonic", side_effect=lambda: next(clock)):
            bridge.return_value.__enter__.return_value.wait_uart.return_value = True
            with self.assertRaisesRegex(FastbootEnumerationUnavailable, "did not enumerate"):
                backend._transport({"inputs": {"port": "mock", "payload": str(payload)}},
                                   None, force_bridge=True)

    def test_connection_ram_entry_distinguishes_uart_and_data_failures(self):
        backend = Backend(transport_factory=lambda: self.fail("real USB prohibited"))
        before = {"kind": "connection_probe", "status": "NEEDS_USER_ACTION",
                  "control": {"status": "READY"}, "data": {"status": "UNAVAILABLE"}}
        cases = [("RAM payload did not report fastboot ready", "UART_NOT_READY", "MULTI"),
                 ("RAM Fastboot did not enumerate after bridge load", "FASTBOOT_USB_MISSING", "DATA USB")]
        for error, stage, hint in cases:
            with self.subTest(stage=stage), \
                 patch.object(backend, "connection_probe", return_value=before), \
                 patch.object(backend, "_transport", side_effect=RuntimeError(error)):
                result = backend.connection_enter_ram(confirmed=True)
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(result["ram_entry_stage"], stage)
            self.assertIn(hint, result["next_action"])

    def test_ram_entry_missing_uart_marker_accepts_late_verified_fastboot_usb(self):
        opens = []
        marker = object()
        def factory():
            opens.append(1)
            if len(opens) == 1:
                raise FastbootEnumerationUnavailable("RAM Fastboot USB device unavailable")
            return marker
        class FakeBridge:
            boots = 0
            def __init__(self, _port): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def boot_payload(self, _payload, **_): type(self).boots += 1
            def wait_uart(self, _marker, _timeout, **_): return False
        backend = Backend(transport_factory=factory)
        with patch("recovery_tool.task_backend.list_ports", return_value=["fake-bridge"]), \
             patch("recovery_tool.task_backend.Bridge", FakeBridge):
            result = backend._transport({"inputs": {}}, None)
        self.assertIs(result, marker)
        self.assertEqual(FakeBridge.boots, 1)
        self.assertEqual(len(opens), 2)

    def test_typed_usb_permission_unavailable_text_never_enters_bridge(self):
        import usb.core
        failure = USBReadError("USB unavailable: access denied",
                               usb.core.USBError("access denied", -3, errno=13),
                               stage="usb_open")
        backend = Backend(transport_factory=lambda: (_ for _ in ()).throw(failure))
        with patch("recovery_tool.task_backend.list_ports") as ports:
            with self.assertRaises(USBReadError):
                backend._transport({"inputs": {}}, None)
            ports.assert_not_called()
        fatal = FastbootIdentityError("Fastboot serial descriptor access failed: unavailable")
        backend = Backend(transport_factory=lambda: (_ for _ in ()).throw(fatal))
        with patch("recovery_tool.task_backend.list_ports") as ports:
            with self.assertRaises(FastbootIdentityError):
                backend._transport({"inputs": {}}, None)
            ports.assert_not_called()

    def test_bridge_diagnostic_caps_raw_uart_and_records_ack_stages(self):
        from recovery_tool.task_backend import _BridgeDiagnostic, BRIDGE_UART_TRACE_LIMIT
        trace = _BridgeDiagnostic("fake-bridge", b"payload", folder=self.root / "diagnostics")
        trace.stage("ping_ack", b"uart_boot 1")
        trace.stage("record_ack", b"")
        trace.stage("record_ack", b"")
        trace.uart(b"A" * (BRIDGE_UART_TRACE_LIMIT + 7))
        receipt = Path(trace.finish(marker=False, outcome="RamPayloadNotReady"))
        report = json.loads(receipt.read_text())
        self.assertEqual(report["uart_bytes"], BRIDGE_UART_TRACE_LIMIT + 7)
        self.assertEqual(report["uart_saved_bytes"], BRIDGE_UART_TRACE_LIMIT)
        self.assertTrue(report["uart_truncated"])
        self.assertEqual(report["stages"][1]["count"], 2)
        self.assertEqual(Path(report["raw_uart_path"]).read_bytes(), b"A" * BRIDGE_UART_TRACE_LIMIT)

    def test_stock_single_confirmation_pins_both_stages_before_writing(self):
        job = self.root / "stock-single"
        job.mkdir()
        dat = self.root / "sony.dat"
        dat.write_bytes(b"official")
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "stock_sd_recovery", "inputs": {"dat": str(dat)},
                        "plan_digest": tx.digest(dat)})
        live = {"nflasha": b"M" * 512, "nflasha1": b"E" * 512,
                "nflasha2": b"S" * (198 * 512)}
        target_updater = self.root / "updater-target.bin"
        target_updater.write_bytes(b"O" * 512)
        baseline = self.root / "settings-baseline.bin"
        baseline.write_bytes(live["nflasha2"])

        class FakeTransport:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read_region(self, name, path, *, length=None, **_):
                Path(path).write_bytes(live[name][:length] if length else live[name])

        def updater_stage(_backend, directory, task, transport, progress):
            stage = directory / "updater-stage"
            if not (stage / "manifest.json").exists():
                tx.prepare(stage, kind="updater_restore",
                           regions=[{"name": "nflasha1", "original": self.root / "updater-live.bin",
                                     "target": target_updater}], identity=[], input_files=[dat],
                           metadata={"settings_profile": "captured-live-6.02-or-6.04",
                                     "settings_baseline": str(baseline)})
            return {"status": "READY"}

        (self.root / "updater-live.bin").write_bytes(live["nflasha1"])
        def candidate(settings, updater, path, **kw):
            self.assertEqual(updater, target_updater.read_bytes())
            self.assertEqual(settings, live["nflasha2"])
            out = bytearray(settings)
            offsets = list(range(133*512, 133*512 + 42)) + [197*512, 197*512 + 1]
            for offset in offsets:
                out[offset] = 1
            return b"fragment", bytes(out), {
                "candidate_sha256": hashlib.sha256(out).hexdigest(),
                "sparse_sectors": [133, 197],
                "updater_environment": {"profile_id": "mocked602"},
                "plan": {"changed_offsets": offsets, "changed_bytes": 44}}

        with patch("recovery_tool.task_backend.verify_stock_sdm2"), \
             patch("recovery_tool.task_backend.build_stock_trigger", candidate), \
             patch("recovery_tool.task_backend.inspect_sd_environment", return_value={"stock": True}), \
             patch("recovery_tool.task_backend.UPDATER_SIZE", 512), \
             patch.object(Backend, "_stock_updater_stage", updater_stage), \
             patch.object(Backend, "_transport", return_value=FakeTransport()):
            backend = Backend(transport_factory=lambda: self.fail("no device writes"))
            plan = backend.stock_plan(job)
            repeated = backend.stock_plan(job)
        self.assertEqual(plan["status"], "READY")
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(plan["plan_digest"], repeated["plan_digest"])
        self.assertEqual(json.loads((job / "stock-composite-plan.json").read_text())
                         ["settings_changed_sectors"], 2)
        self.assertEqual(json.loads((job / "manifest.json").read_text())
                         ["regions"][0]["scope"], [[133, 134], [197, 198]])
        unconfirmed = backend.stock_run(job)
        self.assertEqual(unconfirmed["status"], "NO_WRITE")
        self.assertEqual(backend.stock_run(job, confirmed_plan_digest="wrong")["status"], "NO_WRITE")
        manifest = job / "manifest.json"
        original_manifest = manifest.read_bytes()
        manifest.write_bytes(original_manifest + b" ")
        self.assertEqual(backend.stock_run(job, confirmed_plan_digest=plan["plan_digest"])
                         ["status"], "NO_WRITE")
        self.assertFalse((job / "stock-authorization.json").exists())
        manifest.write_bytes(original_manifest)
        calls = []
        def interrupted_apply(directory, **_):
            calls.append(Path(directory).name)
            state = json.loads((Path(directory) / "journal.json").read_text())
            state["status"] = "UNKNOWN_AFTER_WRITE"
            state["write_may_have_occurred"] = True
            tx.durable_json(Path(directory) / "journal.json", state)
            raise RuntimeError("lost updater ACK")
        with patch("recovery_tool.task_backend.tx.apply", interrupted_apply):
            interrupted = backend.stock_run(job, confirmed_plan_digest=plan["plan_digest"])
        self.assertEqual(interrupted["status"], "UNKNOWN_AFTER_WRITE")
        self.assertEqual(interrupted["phase"], "updater")
        self.assertEqual(interrupted["error_code"], "RuntimeError")
        self.assertTrue(interrupted["write_may_have_occurred"])
        self.assertIn("lost updater ACK", interrupted["failure_cause"])
        self.assertEqual(calls, ["updater-stage"])
        self.assertTrue((job / "stock-authorization.json").exists())
        calls.clear()
        def fake_apply(directory, **_):
            calls.append(Path(directory).name)
            state = json.loads((Path(directory) / "journal.json").read_text())
            state["status"] = ("VERIFIED" if Path(directory).name == "updater-stage"
                               else "REBOOT_ACKNOWLEDGED")
            if Path(directory).name != "updater-stage":
                state["reboot_may_have_occurred"] = True
            tx.durable_json(Path(directory) / "journal.json", state)
            return state
        with patch("recovery_tool.task_backend.tx.apply", fake_apply):
            result = backend.stock_run(job)
        self.assertEqual(calls, ["updater-stage", "stock-single"], result)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertFalse(backend.stock_plan(job)["requires_confirmation"])
        self.assertEqual(backend.stock_run(job)["status"], "NEEDS_USER_ACTION")
        self.assertEqual(calls, ["updater-stage", "stock-single"])


    def test_single_target_restore_capture_interruption_is_incomplete(self):
        target = self.root / "backup-interrupt"
        (target / "decrypted").mkdir(parents=True)
        for name in ("verified.json", "capture-receipt.json", "decrypted/manifest.json"):
            (target / name).write_text("{}")
        backend = Backend(transport_factory=lambda: self.fail("hardware prohibited"))
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"):
            backend.prepare("scoped_restore", {"target_snapshot": str(target)}, self.root / "job-interrupt")
        with patch("recovery_tool.task_backend.snapshot.capture",
                   side_effect=RuntimeError("segment interrupted")):
            result = backend.restore_run(self.root / "job-interrupt")
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse((self.root / "job-interrupt/scoped-stage/manifest.json").exists())

    @patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot")
    def test_partial_restore_capture_verified_region_resumes_capture(self, _verify):
        target = self.root / "partial-target"
        same_body_fixture(target)
        job = self.root / "partial-job"
        backend = Backend(transport_factory=lambda: self.fail("device access prohibited"))
        backend.prepare("scoped_restore", {"target_snapshot": str(target),
                                           "restore_profile": "capability"}, job)
        current = job / "current-snapshot"
        current.mkdir()
        (current / "verified.json").write_text('{"nflashaB0":{"bytes":1024}}')
        (current / "capture-receipt.json").write_text(json.dumps({
            "status": "INCOMPLETE", "capture_profile": "restore-current-no-cp-nor-v1",
            "not_captured_regions": ["cp-nor"]}))
        calls = []
        def interrupted_capture(folder, **options):
            calls.append((folder, options["profile"]))
            raise RuntimeError("simulated USB interruption")
        with patch("recovery_tool.task_backend.snapshot.capture", side_effect=interrupted_capture):
            result = backend.restore_run(job)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(calls, [(current.resolve(), "restore_current")])

    def test_restore_capture_uses_bounded_resume_and_reports_retry_state_without_write(self):
        from recovery_tool import snapshot
        target = self.root / "backup-retry-target"
        (target / "decrypted").mkdir(parents=True)
        for name in ("verified.json", "capture-receipt.json", "decrypted/manifest.json"):
            (target / name).write_text("{}")
        backend = Backend(transport_factory=lambda: object())
        with patch("recovery_tool.task_backend.decrypt_snapshot.verify_snapshot"):
            backend.prepare("scoped_restore", {"target_snapshot": str(target)}, self.root / "retry-restore")
        calls = []
        events = []
        def fake_capture(folder, **options):
            calls.append(options)
            self.assertTrue(options["auto_retry_usb"])
            self.assertIsNotNone(options["reentry_factory"])
            options["progress"]({"stage": "backup_retry", "retry": 2,
                                 "max_retries": 5, "total_retries": 3})
            folder.mkdir(parents=True)
            (folder / "capture-receipt.json").write_text(json.dumps({
                "status": "INCOMPLETE", "retry_consecutive_used": 2,
                "retry_events": [{"retry": 1}, {"retry": 2}, {"retry": 2}]}))
            raise snapshot.CaptureFailure(
                origin="camera_usb_read", region="nflasha", attempt="first",
                offset=1_520_631_808, path=folder / "segments/nflasha.first/chunk.bin",
                cause=RuntimeError("simulated errno 5 USB read"))
        with patch("recovery_tool.task_backend.snapshot.capture", side_effect=fake_capture), \
             patch.object(Backend, "apply", side_effect=AssertionError("flash prohibited")):
            result = backend.restore_run(self.root / "retry-restore", progress=events.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["restore_failure_origin"], "camera_usb_read")
        self.assertEqual((result["restore_retry_used"], result["restore_retry_total"],
                          result["restore_retry_limit"]), (2, 3, 5))
        self.assertIn("restore_capture_retry", [event["stage"] for event in events])
        self.assertFalse((self.root / "retry-restore/scoped-stage/manifest.json").exists())


    def test_stock_index_recovers_after_candidate_commit(self):
        job = self.root / "stock"
        job.mkdir()
        dat = self.root / "sony.dat"
        dat.write_bytes(b"fixture")
        before, after = self.root / "before.bin", self.root / "after.bin"
        before.write_bytes(b"A" * 512)
        after.write_bytes(b"B" * 512)
        tx.prepare(job, kind="stock_sd_recovery",
                   regions=[{"name": "nflasha2", "original": before, "target": after}],
                   identity=[], input_files=[dat], reboot=True)
        task = {"schema": "camera-task-v1", "kind": "stock_sd_recovery",
                "inputs": {"dat": str(dat)}, "plan_digest": tx.digest(dat)}
        tx.durable_json(job / "task.json", task)
        with patch.dict("recovery_tool.task_backend.STOCK_CATALOG", {"packages": {tx.digest(dat): {"version": "6.02", "bytes": len(dat.read_bytes())}}}), \
             patch("recovery_tool.task_backend.tx.load", wraps=tx.load) as checked:
            _check_manifest_pin(job, task)
            checked.assert_called_once_with(job)
        self.assertEqual(json.loads((job / "task.json").read_text())["plan_digest"],
                         tx.digest(job / "manifest.json"))

    def test_stock_candidate_reconcile_is_two_stage_and_resume_safe(self):
        job = self.root / "stock"
        job.mkdir()
        dat = self.root / "sony.dat"
        dat.write_bytes(b"fixture")
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "stock_sd_recovery", "inputs": {"dat": str(dat)},
                        "plan_digest": tx.digest(dat)})
        reads = []

        class FakeTransport:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read_region(self, name, path, *, length=None, **_):
                reads.append(name)
                Path(path).write_bytes({"nflasha": b"S" * 512,
                                        "nflasha1": b"U" * 512,
                                        "nflasha2": b"A" * 1024}[name])

        def candidate(settings, updater, path, **_):
            self.assertEqual(settings, b"A" * 1024)
            self.assertEqual(updater, b"U" * 512)
            self.assertEqual(path, dat.resolve())
            output = b"A" * 512 + b"B" * 512
            return b"fragment", output, {"candidate_sha256": hashlib.sha256(output).hexdigest(),
                                        "sparse_sectors": [1]}

        # tx.reconcile is independently covered by sector-level fake USB
        # tests; here exercise the backend's live candidate staging boundary.
        with patch.dict("recovery_tool.task_backend.STOCK_CATALOG", {"packages": {tx.digest(dat): {"version": "6.02", "bytes": len(dat.read_bytes())}}}), \
             patch("recovery_tool.task_backend.verify_stock_sdm2"), \
             patch("recovery_tool.task_backend.build_stock_trigger", candidate), \
             patch("recovery_tool.task_backend.inspect_sd_environment", return_value={"stock": True}), \
             patch("recovery_tool.task_backend.UPDATER_SIZE", 512), \
             patch("recovery_tool.task_backend.OEM_SHA256", hashlib.sha256(b"U" * 512).hexdigest()), \
             patch("recovery_tool.task_backend.tx.reconcile", return_value={"status": "READY"}), \
             patch.object(Backend, "_transport", return_value=FakeTransport()):
            backend = Backend()
            first = backend.reconcile(job)
            second = backend.reconcile(job)
        self.assertEqual((first["status"], second["status"]), ("READY", "READY"),
                         (first, second))
        self.assertEqual(reads.count("nflasha2"), 1)
        self.assertGreaterEqual(reads.count("nflasha1"), 2)
        self.assertEqual(len(list((job / "stock-inputs").glob("attempt-*"))), 1)
        self.assertEqual(json.loads((job / "task.json").read_text())["plan_digest"],
                         tx.digest(job / "manifest.json"))

    def test_erlla_stock_gui_task_advances_updater_then_trigger(self):
        job = self.root / "composite-stock"
        job.mkdir()
        dat, oem, baseline = (self.root / name for name in
                              ("sony.dat", "oem-updater.bin", "settings-baseline.bin"))
        dat.write_bytes(b"D")
        oem.write_bytes(b"O" * 512)
        baseline.write_bytes(b"S" * 1024)
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "stock_sd_recovery",
                        "inputs": {"dat": str(dat.resolve()),
                                   "oem_updater": str(oem.resolve()),
                                   "settings_baseline": str(baseline.resolve())},
                        "plan_digest": tx.digest(dat)})
        live = {"nflasha": b"M" * 512, "nflasha1": b"E" * 512,
                "nflasha2": b"S" * 1024}
        calls = []

        class FakeTransport:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read_region(self, name, path, *, length=None, **_):
                Path(path).write_bytes(live[name][:length] if length else live[name])

        def fake_reconcile(path, **_):
            if Path(path).name == "updater-stage":
                state = json.loads((Path(path) / "journal.json").read_text())
                return {"status": "VERIFIED" if state["status"] == "VERIFIED" else "READY"}
            return {"status": "READY"}

        def fake_apply(path, **_):
            calls.append(Path(path).name)
            if Path(path).name == "updater-stage":
                if calls.count("updater-stage") == 1:
                    state = json.loads((Path(path) / "journal.json").read_text())
                    state["status"] = "UNKNOWN_AFTER_WRITE"
                    state["write_may_have_occurred"] = True
                    tx.durable_json(Path(path) / "journal.json", state)
                    raise RuntimeError("simulated lost updater flash ACK")
                live["nflasha1"] = oem.read_bytes()
                state = json.loads((Path(path) / "journal.json").read_text())
                state["status"] = "VERIFIED"
                tx.durable_json(Path(path) / "journal.json", state)
                return state
            return {"status": "REBOOT_ACKNOWLEDGED"}

        def trigger(settings, updater, selected_dat, **_):
            self.assertEqual(updater, oem.read_bytes())
            self.assertEqual(selected_dat, dat.resolve())
            candidate = settings[:512] + b"T" * 512
            return b"sparse", candidate, {"candidate_sha256":
                                             hashlib.sha256(candidate).hexdigest(),
                                           "sparse_sectors": [1]}

        with patch.dict("recovery_tool.task_backend.STOCK_CATALOG", {"packages": {tx.digest(dat): {"version": "6.02", "bytes": len(dat.read_bytes())}}}), \
             patch("recovery_tool.task_backend.UPDATER_SIZE", 512), \
             patch("recovery_tool.task_backend.BASELINE_SETTINGS_SIZE", 1024), \
             patch("recovery_tool.task_backend.OEM_SHA256", tx.digest(oem)), \
             patch("recovery_tool.task_backend.CURRENT_SHA256",
                   hashlib.sha256(live["nflasha1"]).hexdigest()), \
             patch("recovery_tool.task_backend.SETTINGS_SHA256", tx.digest(baseline)), \
             patch("recovery_tool.task_backend.verify_stock_sdm2"), \
             patch("recovery_tool.task_backend.validate_settings", return_value={}), \
             patch("recovery_tool.task_backend.build_updater_plan", return_value=([], {})), \
             patch("recovery_tool.task_backend.build_stock_trigger", trigger), \
             patch("recovery_tool.task_backend.tx.reconcile", side_effect=fake_reconcile), \
             patch("recovery_tool.task_backend.tx.apply", side_effect=fake_apply), \
             patch.object(Backend, "_transport", return_value=FakeTransport()):
            backend = Backend()
            staged = backend.reconcile(job)
            unknown = backend.apply(job)
            resumed = backend.reconcile(job)
            stock_ready = backend.apply(job)
            checked_again = backend.reconcile(job)
            result = backend.apply(job)
        self.assertEqual(staged["status"], "READY")
        self.assertIn("Erlla", staged["summary"])
        self.assertEqual(unknown["status"], "UNKNOWN_AFTER_WRITE")
        self.assertEqual(resumed["status"], "READY")
        self.assertEqual(stock_ready["status"], "READY")
        self.assertIn("sectors 133 and 197", stock_ready["summary"])
        self.assertEqual(checked_again["status"], "READY")
        self.assertEqual(calls, ["updater-stage", "updater-stage", "composite-stock"])
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertTrue((job / "updater-stage/manifest.json").exists())
        self.assertTrue((job / "manifest.json").exists())

    def test_no_history_stock_stages_reconstructed_target_from_live(self):
        job = self.root / "no-history"
        job.mkdir()
        dat = self.root / "sony.dat"
        dat.write_bytes(b"D")
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "stock_sd_recovery", "inputs": {"dat": str(dat.resolve())},
                        "plan_digest": tx.digest(dat)})
        class FakeTransport:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read_region(self, name, path, *, length=None, **_):
                Path(path).write_bytes({"nflasha": b"M" * 512,
                                        "nflasha1": b"E" * 512,
                                        "nflasha2": b"S" * 1024}[name])
        source_module = ModuleType("recovery_tool.stock_updater_source")
        source_module.reconstruct = lambda live, selected: (
            b"O" * 512, {"profile": "stock-components", "preserved_live_bytes": True})
        with patch.dict(sys.modules, {source_module.__name__: source_module}), \
             patch.dict("recovery_tool.task_backend.STOCK_CATALOG", {"packages": {tx.digest(dat): {"version": "6.02", "bytes": len(dat.read_bytes())}}}), \
             patch("recovery_tool.task_backend.UPDATER_SIZE", 512), \
             patch("recovery_tool.task_backend.OEM_SHA256", "different-reviewed-oem"), \
             patch("recovery_tool.task_backend.verify_stock_sdm2"), \
             patch("recovery_tool.task_backend.inspect_sd_environment", side_effect=[StockRecoveryError("UPDATER_PROFILE", "custom fixture"), {"stock": True}]), \
             patch("recovery_tool.task_backend.validate_settings", return_value={}) as validated, \
             patch("recovery_tool.task_backend.tx.reconcile", return_value={"status": "READY"}), \
             patch.object(Backend, "_transport", return_value=FakeTransport()):
            result = Backend().reconcile(job)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(json.loads((job / "updater-stage/manifest.json").read_text())
                         ["metadata"]["settings_profile"], "captured-live-6.02-or-6.04")
        self.assertEqual((job / "updater-probe/attempt-001/settings.bin").read_bytes(), b"S" * 1024)
        self.assertEqual(validated.call_args.kwargs["expected_versions"], ("6.02", "6.04"))
        self.assertFalse((job / "manifest.json").exists())

    def test_no_history_unknown_updater_stops_read_only(self):
        job = self.root / "unknown"
        job.mkdir()
        dat = self.root / "sony.dat"
        dat.write_bytes(b"D")
        tx.durable_json(job / "task.json", {"schema": "camera-task-v1",
                        "kind": "stock_sd_recovery", "inputs": {"dat": str(dat.resolve())},
                        "plan_digest": tx.digest(dat)})
        class FakeTransport:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read_region(self, name, path, *, length=None, **_):
                Path(path).write_bytes({"nflasha": b"M" * 512,
                                        "nflasha1": b"?" * 512}[name])
        source_module = ModuleType("recovery_tool.stock_updater_source")
        def reject(*_): raise ValueError("unsupported Erlla profile")
        source_module.reconstruct = reject
        with patch.dict(sys.modules, {source_module.__name__: source_module}), \
             patch.dict("recovery_tool.task_backend.STOCK_CATALOG", {"packages": {tx.digest(dat): {"version": "6.02", "bytes": len(dat.read_bytes())}}}), \
             patch("recovery_tool.task_backend.OEM_SHA256", "reviewed-oem"), \
             patch("recovery_tool.task_backend.verify_stock_sdm2"), \
             patch.object(Backend, "_transport", return_value=FakeTransport()):
            result = Backend().reconcile(job)
        self.assertEqual(result["status"], "NO_WRITE")
        self.assertFalse((job / "updater-stage/manifest.json").exists())
        self.assertFalse((job / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
