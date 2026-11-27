"""Verify worker lifecycle routing with fakes only; never opens camera USB."""

from contextlib import redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import task_worker
from recovery_tool import card_connection
from recovery_tool.write_transaction import durable_json
from recovery_tool.task_backend import Backend
from recovery_tool.task_backend import _stock_failure_evidence
from recovery_tool.transport import FastbootError, FastbootEnumerationUnavailable


class FakeBackend:
    transport_factory = staticmethod(lambda: None)
    def connection_enter_ram(self, **_):
        return {"kind": "connection_probe", "status": "READY", "data": {"status": "READY"}}
    def verify(self, _job, _progress):
        return {"kind": "full_backup", "status": "VERIFIED"}
    def restore_run(self, _job, _progress):
        return {"kind": "scoped_restore", "status": "VERIFIED"}
    def stock_run(self, _job, _progress, _digest):
        return {"kind": "stock_sd_recovery", "status": "NEEDS_USER_ACTION",
                "stock_reboot_acknowledged": True}


class WorkerModeExitTests(unittest.TestCase):
    def test_backup_recapture_worker_has_no_camera_mode_exit(self):
        backend = Backend()
        output = io.StringIO()
        with patch.object(backend, "backup_recapture",
                          return_value={"kind": "full_backup", "status": "PARTIAL"}) as recapture, \
             patch.object(task_worker, "return_normal", side_effect=AssertionError("reboot prohibited")), \
             redirect_stdout(output):
            task_worker.handle({"action": "backup_recapture", "prior_snapshot": "/old/snapshot",
                                "output_dir": "/new/snapshot", "region": "nflasha",
                                "same_camera_confirmed": True}, backend)
        self.assertEqual(json.loads(output.getvalue())["result"]["status"], "PARTIAL")
        self.assertEqual(recapture.call_args.args, (Path("/old/snapshot"), Path("/new/snapshot")))
        self.assertIs(recapture.call_args.kwargs["same_camera_confirmed"], True)
        self.assertEqual(recapture.call_args.kwargs["region"], "nflasha")

    def test_active_worker_file_lock_rejects_second_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fd = os.open(root / "camera-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                output = io.StringIO()
                with patch.object(task_worker, "APP_ROOT", root), \
                     patch.object(sys, "stdin", io.StringIO('{"action":"unknown"}\n')), \
                     redirect_stdout(output):
                    task_worker.main()
                event = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(event["type"], "error")
                self.assertIn("Resource temporarily unavailable", event["error"])
            finally:
                os.close(fd)

    def test_verified_backup_uses_task_local_exit_and_failed_read_sends_no_reboot(self):
        class BackupBackend(FakeBackend):
            def apply(self, _job, _progress):
                return {"kind": "full_backup", "status": "INCOMPLETE",
                        "backup_failure_origin": "camera_usb_read"}
        backend = BackupBackend()
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(task_worker, "Backend", BackupBackend), \
             patch.object(task_worker, "return_normal", return_value={
                 "normal_mode_status": "READY", "current_mode": "MSC"}) as exit_mode, \
             patch.object(task_worker, "observe_normal_camera_usb", return_value=None):
            for action in ("apply", "verify", "verify"):
                output = io.StringIO()
                with redirect_stdout(output):
                    task_worker.handle({"action": action, "job_dir": folder}, backend)
                result = json.loads(output.getvalue().splitlines()[-1])["result"]
                if action == "apply":
                    self.assertEqual(result["status"], "INCOMPLETE")
                    self.assertFalse(result["reboot_sent"])
            self.assertEqual(exit_mode.call_count, 2)
            for call in exit_mode.call_args_list:
                self.assertEqual(call.kwargs["journal_path"], Path(folder) / "post-backup-normal.json")
                self.assertTrue(call.kwargs["ignore_historical_tasks"])

    def test_stock_current_unknown_write_never_bridges_on_missing_fastboot(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            durable_json(job / "journal.json", {
                "status": "UNKNOWN_AFTER_WRITE", "write_may_have_occurred": True,
                "reboot_may_have_occurred": False, "confirmed_fragments": 1,
                "inflight": {"region": "nflasha2", "fragment": 0}})
            backend = Backend(transport_factory=lambda: (_ for _ in ()).throw(
                FastbootEnumerationUnavailable("ILCE-7M4 RAM Fastboot USB device unavailable")))
            with patch("recovery_tool.task_backend.Bridge",
                       side_effect=AssertionError("bridge reset forbidden")):
                with self.assertRaisesRegex(FastbootError, "direct Fastboot readback"):
                    backend._stock_transport(job, {"inputs": {}}, None)

    def test_stock_corrupt_current_journal_is_unknown_not_no_write(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            (job / "manifest.json").write_text("{}")
            (job / "journal.json").write_text("{broken")
            evidence = _stock_failure_evidence(job)
            self.assertFalse(evidence["journal_valid"])
            self.assertIsNone(evidence["write_may_have_occurred"])
            self.assertFalse(task_worker._stock_plan_mode_exit(job, Backend(), None)["reboot_sent"])
            durable_json(job / "journal.json", {
                "schema": "write-transaction-state-v1", "status": "READY",
                "write_may_have_occurred": False,
                "reboot_may_have_occurred": False,
                "confirmed_fragments": 0, "inflight": "unreadable"})
            self.assertFalse(_stock_failure_evidence(job)["journal_valid"])
            with patch.object(task_worker, "return_normal",
                              side_effect=AssertionError("malformed inflight must not reboot")):
                self.assertFalse(task_worker._stock_plan_mode_exit(job, Backend(), None)["reboot_sent"])

    def test_stock_planner_failure_preserves_ram_even_when_current_task_has_no_write(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder) / "current"
            job.mkdir()
            durable_json(job / "journal.json", {
                "schema": "write-transaction-state-v1", "status": "READY",
                "write_may_have_occurred": False,
                "reboot_may_have_occurred": False, "confirmed_fragments": 0})
            with patch.object(task_worker, "return_normal",
                              side_effect=AssertionError("planner failure must not reboot")) as exit_mode:
                result = task_worker._stock_plan_mode_exit(job, Backend(), None)
            self.assertEqual(result["normal_mode_status"], "NOT_EXITED")
            self.assertFalse(result["reboot_sent"])
            exit_mode.assert_not_called()
            self.assertFalse((job / "stock-plan-normal.json").exists())
            state = json.loads((job / "journal.json").read_text())
            state.update(status="REBOOT_UNCERTAIN", reboot_may_have_occurred=True)
            durable_json(job / "journal.json", state)
            with patch.object(task_worker, "return_normal",
                              side_effect=AssertionError("second reboot forbidden")):
                blocked = task_worker._stock_plan_mode_exit(job, Backend(), None)
            self.assertFalse(blocked["reboot_sent"])

    def test_restore_no_write_keeps_ram_without_global_normal_reboot(self):
        class NoWriteBackend(FakeBackend):
            def restore_run(self, _job, _progress):
                return {"kind": "scoped_restore", "status": "NO_WRITE"}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(task_worker, "Backend", NoWriteBackend), \
             patch.object(task_worker, "return_normal",
                          side_effect=AssertionError("plain reboot forbidden")), \
             patch.object(task_worker, "observe_normal_camera_usb", return_value=None):
            output = io.StringIO()
            with redirect_stdout(output):
                task_worker.handle({"action": "restore_run", "job_dir": folder}, NoWriteBackend())
            result = json.loads(output.getvalue().splitlines()[-1])["result"]
            self.assertEqual(result["status"], "NO_WRITE")
            self.assertFalse(result["reboot_sent"])

    def test_partial_restore_keeps_ram_and_verified_exit_is_once_only(self):
        class PartialBackend(FakeBackend):
            def restore_run(self, _job, _progress):
                return {"kind": "scoped_restore", "status": "PARTIAL",
                        "failed_fragments": 1}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(task_worker, "Backend", PartialBackend), \
             patch.object(task_worker, "return_normal",
                          side_effect=AssertionError("partial result must not reboot")), \
             patch.object(task_worker, "observe_normal_camera_usb", return_value=None):
            output = io.StringIO()
            with redirect_stdout(output):
                task_worker.handle({"action": "restore_run", "job_dir": folder}, PartialBackend())
            result = json.loads(output.getvalue().splitlines()[-1])["result"]
            self.assertEqual(result["status"], "PARTIAL")
            self.assertFalse(result["reboot_sent"])
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(task_worker, "Backend", FakeBackend), \
             patch.object(task_worker, "return_normal",
                          return_value={"normal_mode_status": "READY"}) as exit_mode:
            stage = Path(folder) / "scoped-stage"
            stage.mkdir()
            (stage / "manifest.json").write_text("{}")
            output = io.StringIO()
            with redirect_stdout(output):
                task_worker.handle({"action": "restore_run", "job_dir": folder}, FakeBackend())
            self.assertEqual(exit_mode.call_count, 1)
            self.assertTrue(exit_mode.call_args.kwargs["once_only"])
            self.assertEqual(exit_mode.call_args.kwargs["journal_path"],
                             Path(folder) / "post-restore-normal.json")

    def test_prior_failed_update_does_not_block_fresh_stock_run(self):
        class StockBackend(FakeBackend):
            runs = 0
            def stock_run(self, _job, _progress, _digest):
                type(self).runs += 1
                return {"kind": "stock_sd_recovery", "status": "NEEDS_USER_ACTION",
                        "stock_reboot_acknowledged": True}
        StockBackend.runs = 0
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prior, current = root / "prior", root / "current"
            prior.mkdir(); current.mkdir()
            durable_json(prior / "task.json", {"kind": "stock_sd_recovery"})
            durable_json(prior / "journal.json", {
                "status": "REBOOT_ACKNOWLEDGED", "write_may_have_occurred": True,
                "reboot_may_have_occurred": True, "last_verified_utc": "2026-11-14T23:55:29Z"})
            with patch.object(task_worker, "Backend", StockBackend), \
                 patch.object(task_worker, "TASK_ROOT", root), \
                 patch.object(card_connection, "TASK_REGISTRY", root / "registry.json"):
                output = io.StringIO()
                with redirect_stdout(output):
                    task_worker.handle({"action": "stock_run", "job_dir": str(current),
                                        "confirmed_plan_digest": "new-plan"})
                allowed = json.loads(output.getvalue().splitlines()[-1])["result"]
                self.assertEqual(allowed["status"], "NEEDS_USER_ACTION")
                self.assertEqual(StockBackend.runs, 1)
                self.assertFalse((prior / "camera-update-failed.json").exists())

    def test_stock_planner_error_preserved_without_normal_reboot(self):
        with tempfile.TemporaryDirectory() as folder:
            output = io.StringIO()
            request = {"action": "stock_plan", "job_dir": folder}
            with patch.object(task_worker, "APP_ROOT", Path(folder)), \
                 patch.object(task_worker, "MODE_JOURNAL", Path(folder) / "mode.json"), \
                 patch.object(task_worker, "handle", side_effect=RuntimeError("semantic scope mismatch")), \
                 patch.object(task_worker, "return_normal", return_value={
                     "normal_mode_status": "READY", "current_mode": "MSC"}) as exit_mode, \
                 patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), \
                 redirect_stdout(output):
                task_worker.main()
            event = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(event["type"], "error")
            self.assertEqual(event["error"], "semantic scope mismatch")
            self.assertEqual(event["phase"], "plan")
            self.assertEqual(event["error_code"], "RuntimeError")
            self.assertIs(event["write_may_have_occurred"], False)
            self.assertEqual(event["normal_mode_status"], "NOT_EXITED")
            self.assertFalse(event["reboot_sent"])
            exit_mode.assert_not_called()

    def test_only_successful_nonupdate_tasks_auto_exit(self):
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(task_worker, "Backend", FakeBackend), \
             patch.object(task_worker, "return_normal", return_value={
                 "normal_mode_status": "READY", "current_mode": "MSC"}) as exit_mode:
            for action, should_exit in (("connection_enter_ram", True),
                                        ("verify", True), ("restore_run", True),
                                        ("stock_run", False)):
                request = {"action": action, "job_dir": folder}
                output = io.StringIO()
                with redirect_stdout(output):
                    task_worker.handle(request, backend)
                result = json.loads(output.getvalue().splitlines()[-1])["result"]
                self.assertEqual("normal_mode_status" in result, should_exit)
            # A restore result without a verified scoped-stage readback is
            # observation-only; no normal-mode reboot is sent.
            self.assertEqual(exit_mode.call_count, 2)

    def test_later_task_reenters_ram_after_normal_mode(self):
        calls = []
        class Bridge:
            def __init__(self, port): calls.append(("port", port))
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def boot_payload(self, payload, **_): calls.append(("boot", payload))
            def wait_uart(self, *_, **__): return True
        class Transport: pass
        attempts = 0
        def factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FastbootEnumerationUnavailable("ILCE-7M4 RAM Fastboot USB device unavailable")
            return Transport()
        with tempfile.TemporaryDirectory() as folder:
            payload = Path(folder) / "payload.bin"
            payload.write_bytes(b"known RAM payload")
            with patch("recovery_tool.task_backend.list_ports", return_value=["/dev/cu.fake"]), \
                 patch("recovery_tool.task_backend.Bridge", Bridge), \
                 patch("recovery_tool.task_backend.DEFAULT_PAYLOAD", payload):
                backend = Backend(transport_factory=factory)
                result = backend._transport({"inputs": {}, "payload_sha256":
                                             hashlib.sha256(payload.read_bytes()).hexdigest()}, None)
        self.assertIsInstance(result, Transport)
        self.assertEqual(attempts, 2)
        self.assertIn(("boot", b"known RAM payload"), calls)


if __name__ == "__main__": unittest.main()
