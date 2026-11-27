"""Fake-only camera mode transition checks; never opens USB hardware."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import card_connection
from recovery_tool.write_transaction import durable_json


class Clock:
    def __init__(self): self.value = 0.0
    def monotonic(self): return self.value
    def sleep(self, seconds): self.value += seconds


class FakeFastboot:
    def __init__(self, *, lose_ack=False):
        self.commands = []
        self.lose_ack = lose_ack
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def verify_product(self): return "ILCE-7M4"
    def _send(self, command): self.commands.append(command)
    def _reply(self):
        if self.lose_ack: raise RuntimeError("simulated lost ACK")
        return b"OKAY", b""


class CardConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.journal = self.root / "mode.json"
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        registry_patch = patch.object(card_connection, "TASK_REGISTRY", self.root / "registry.json")
        registry_patch.start()
        self.addCleanup(registry_patch.stop)

    def call(self, *, normal_probe, volume_probe=lambda: [], transport_factory=None, **kwargs):
        return card_connection.prepare_card_connection(
            route="camera", journal_path=self.journal, task_root=self.tasks,
            normal_probe=normal_probe, volume_probe=volume_probe,
            transport_factory=transport_factory or (lambda: self.fail("Fastboot opened")),
            sleep=self.clock.sleep, monotonic=self.clock.monotonic, timeout=0.7, **kwargs)

    def test_existing_sony_msc_candidate_never_reboots_or_claims_volume_association(self):
        result = self.call(normal_probe=lambda: {"status": "MSC"},
                           volume_probe=lambda: [{"path": "/Volumes/CAMERA", "device_identifier": "disk1"}])
        self.assertEqual(result["status"], "READY")
        self.assertFalse(result["camera_volume_verified"])
        self.assertFalse(result["reboot_sent"])

    def test_fastboot_exit_once_then_sony_msc_mount(self):
        fastboot = FakeFastboot()
        calls = 0
        def normal():
            nonlocal calls
            calls += 1
            return {"status": "MSC"} if calls > 2 else None
        result = self.call(normal_probe=normal,
                           volume_probe=lambda: [{"path": "/Volumes/CAMERA", "device_identifier": "disk1"}],
                           transport_factory=lambda: fastboot)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(fastboot.commands, ["reboot"])
        self.assertEqual(json.loads(self.journal.read_text())["status"], "READY")

    def test_r5_fastboot_exit_records_selected_and_actual_model(self):
        fastboot = FakeFastboot()
        fastboot.verify_product = lambda: "ILCE-7RM5"
        calls = 0
        def normal():
            nonlocal calls
            calls += 1
            return {"status": "MSC"} if calls > 2 else None
        result = self.call(normal_probe=normal,
                           volume_probe=lambda: [{"path": "/Volumes/CAMERA", "device_identifier": "disk1"}],
                           transport_factory=lambda: fastboot, camera_model="ILCE-7RM5")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(fastboot.commands, ["reboot"])
        receipt = json.loads(self.journal.read_text())
        self.assertEqual(receipt["camera_model"], "ILCE-7RM5")
        self.assertEqual(receipt["product"], "ILCE-7RM5")

    def test_lost_ack_never_retries_reboot(self):
        fastboot = FakeFastboot(lose_ack=True)
        first = self.call(normal_probe=lambda: None, transport_factory=lambda: fastboot)
        self.assertEqual(first["status"], "REBOOT_UNCERTAIN")
        second = self.call(normal_probe=lambda: None, transport_factory=lambda: fastboot)
        self.assertEqual(second["status"], "REBOOT_UNCERTAIN")
        self.assertEqual(fastboot.commands, ["reboot"])
        self.assertTrue(json.loads(self.journal.read_text())["reboot_may_have_occurred"])

    def test_task_local_once_only_exit_never_reboots_again_after_ready(self):
        fastboot = FakeFastboot()
        calls = 0
        def first_normal():
            nonlocal calls
            calls += 1
            return {"status": "MSC"} if calls >= 2 else None
        first = card_connection.return_normal(
            journal_path=self.root / "post-backup-normal.json", task_root=self.tasks,
            ignore_historical_tasks=True, once_only=True,
            transport_factory=lambda: fastboot, normal_probe=first_normal,
            sleep=self.clock.sleep, monotonic=self.clock.monotonic, timeout=0.7)
        self.assertEqual(first["normal_mode_status"], "READY")
        self.assertEqual(fastboot.commands, ["reboot"])
        second = card_connection.return_normal(
            journal_path=self.root / "post-backup-normal.json", task_root=self.tasks,
            ignore_historical_tasks=True, once_only=True,
            transport_factory=lambda: fastboot, normal_probe=lambda: None,
            sleep=self.clock.sleep, monotonic=self.clock.monotonic, timeout=0.1)
        self.assertEqual(second["normal_mode_status"], "REBOOT_UNCERTAIN")
        self.assertFalse(second["reboot_sent"])
        self.assertEqual(fastboot.commands, ["reboot"])

    def test_unresolved_stock_journal_blocks_mode_change(self):
        job = self.tasks / "stock"
        job.mkdir()
        durable_json(job / "task.json", {"kind": "stock_sd_recovery"})
        durable_json(job / "journal.json", {"status": "REBOOT_ACKNOWLEDGED",
                                             "write_may_have_occurred": True,
                                             "reboot_may_have_occurred": True})
        result = self.call(normal_probe=lambda: None)
        self.assertEqual(result["status"], "NO_WRITE")
        self.assertEqual(result["blocking_tasks"][0]["job_dir"], str(job))
        confirmed = card_connection.confirm_stock_update_complete(job, confirmed=True)
        self.assertEqual(confirmed["status"], "VERIFIED")
        self.assertEqual(card_connection._blocking_tasks(self.tasks), [])

    def test_verified_fresh_restore_exit_ignores_historical_journal_only(self):
        old = self.tasks / "old-restore"
        old.mkdir()
        durable_json(old / "task.json", {"kind": "scoped_restore"})
        stage = old / "scoped-stage"
        stage.mkdir()
        durable_json(stage / "journal.json", {"status": "UNKNOWN_AFTER_WRITE",
                                               "write_may_have_occurred": True,
                                               "reboot_may_have_occurred": False})
        fastboot = FakeFastboot()
        result = card_connection.return_normal(
            journal_path=self.root / "new-restore-normal.json", task_root=self.tasks,
            ignore_historical_tasks=True, transport_factory=lambda: fastboot,
            normal_probe=lambda: None, sleep=self.clock.sleep,
            monotonic=self.clock.monotonic, timeout=0.1)
        self.assertEqual(fastboot.commands, ["reboot"])
        self.assertTrue(result["reboot_sent"])
        self.assertEqual(card_connection._blocking_tasks(self.tasks)[0]["job_dir"], str(old))

    def test_uncertain_stock_reboot_cannot_be_confirmed_complete(self):
        job = self.tasks / "uncertain-stock"
        job.mkdir()
        durable_json(job / "task.json", {"kind": "stock_sd_recovery"})
        durable_json(job / "journal.json", {"status": "REBOOT_UNCERTAIN",
                                             "write_may_have_occurred": True,
                                             "reboot_may_have_occurred": True})
        with self.assertRaisesRegex(ValueError, "not been acknowledged"):
            card_connection.confirm_stock_update_complete(job, confirmed=True)
        self.assertFalse((job / "camera-update-complete.json").exists())

    def test_explicit_failed_no_card_receipt_resolves_only_prior_acknowledged_reboot(self):
        job = self.tasks / "stock-no-card"
        job.mkdir()
        durable_json(job / "task.json", {"kind": "stock_sd_recovery"})
        durable_json(job / "journal.json", {"status": "REBOOT_ACKNOWLEDGED",
                                           "write_may_have_occurred": True,
                                           "reboot_may_have_occurred": True,
                                           "last_verified_utc": "2026-11-14T23:55:29Z"})
        with self.assertRaisesRegex(ValueError, "explicit"):
            card_connection.confirm_stock_update_failed(job, confirmed=False, reason="no_sd_card")
        self.assertEqual(len(card_connection._blocking_tasks(self.tasks)), 1)
        result = card_connection.confirm_stock_update_failed(job, confirmed=True, reason="no_sd_card")
        self.assertEqual(result["status"], "FAILURE_RECORDED")
        self.assertEqual(card_connection._blocking_tasks(self.tasks), [])
        self.assertFalse((job / "camera-update-complete.json").exists())
        with self.assertRaisesRegex(ValueError, "already confirmed failed"):
            card_connection.confirm_stock_update_complete(job, confirmed=True)
        state = json.loads((job / "journal.json").read_text())
        state["status"] = "REBOOT_UNCERTAIN"
        durable_json(job / "journal.json", state)
        self.assertEqual(len(card_connection._blocking_tasks(self.tasks)), 1)

    def test_observed_sony_blink_failure_is_distinct_from_no_card(self):
        job = self.tasks / "stock-blink-error"
        job.mkdir()
        durable_json(job / "task.json", {"kind": "stock_sd_recovery"})
        durable_json(job / "journal.json", {"status": "REBOOT_ACKNOWLEDGED",
                                           "write_may_have_occurred": True,
                                           "reboot_may_have_occurred": True,
                                           "last_verified_utc": "2026-11-15T01:01:05Z"})
        with self.assertRaisesRegex(ValueError, "explicit"):
            card_connection.confirm_stock_update_failed(
                job, confirmed=False, reason="blink_5_long_6_short")
        result = card_connection.confirm_stock_update_failed(
            job, confirmed=True, reason="blink_5_long_6_short")
        self.assertEqual(result["status"], "FAILURE_RECORDED")
        self.assertIn("5-long/6-short", result["summary"])
        self.assertEqual(json.loads((job / "camera-update-failed.json").read_text())["reason"],
                         "blink_5_long_6_short")
        self.assertEqual(card_connection._blocking_tasks(self.tasks), [])
        saved = (job / "camera-update-failed.json").read_bytes()
        card_connection.confirm_stock_update_failed(job, confirmed=True, reason="blink_5_long_6_short")
        self.assertEqual((job / "camera-update-failed.json").read_bytes(), saved)
        with self.assertRaisesRegex(ValueError, "differs"):
            card_connection.confirm_stock_update_failed(job, confirmed=True, reason="no_sd_card")
        state = json.loads((job / "journal.json").read_text())
        state["last_verified_utc"] = "later"
        durable_json(job / "journal.json", state)
        with self.assertRaisesRegex(ValueError, "differs"):
            card_connection.confirm_stock_update_failed(job, confirmed=True, reason="blink_5_long_6_short")
        self.assertEqual(len(card_connection._blocking_tasks(self.tasks)), 1)

    def test_verified_scoped_restore_does_not_block_normal_exit(self):
        job = self.tasks / "restore"
        job.mkdir()
        durable_json(job / "task.json", {"kind": "scoped_restore"})
        durable_json(job / "journal.json", {"status": "VERIFIED",
                                             "write_may_have_occurred": True,
                                             "reboot_may_have_occurred": False})
        fastboot = FakeFastboot()
        result = card_connection.return_normal(
            journal_path=self.journal, task_root=self.tasks,
            normal_probe=lambda: None, transport_factory=lambda: fastboot,
            sleep=self.clock.sleep, monotonic=self.clock.monotonic, timeout=0.7)
        self.assertEqual(result["normal_mode_status"], "REBOOT_UNCERTAIN")
        self.assertEqual(fastboot.commands, ["reboot"])

    def test_registry_tracks_restore_task_outside_default_root(self):
        outside = self.root / "external-backup" / "restore-job"
        outside.mkdir(parents=True)
        (outside / "scoped-stage").mkdir()
        durable_json(outside / "task.json", {"kind": "scoped_restore"})
        durable_json(outside / "scoped-stage/journal.json",
                     {"status": "UNKNOWN_AFTER_WRITE", "write_may_have_occurred": True,
                      "reboot_may_have_occurred": False})
        card_connection.register_task(outside)
        blocked = card_connection._blocking_tasks(self.tasks)
        self.assertEqual(blocked[0]["job_dir"], str(outside.resolve()))


if __name__ == "__main__": unittest.main()
