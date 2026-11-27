"""Current-task failure evidence tests; no camera or app access."""

import json
from pathlib import Path
import tempfile
import unittest
import usb.core

from recovery_tool.task_backend import _restore_failure_details
from recovery_tool.write_transaction import CameraWriteIOError


class RestoreFailureEvidenceTests(unittest.TestCase):
    def test_usb_other_from_flash_window_keeps_type_and_code_without_inventing_errno(self):
        with tempfile.TemporaryDirectory() as folder:
            detail = _restore_failure_details(
                CameraWriteIOError(usb.core.USBError("Other error", -99)),
                "transaction", Path(folder))
            self.assertEqual((detail["phase"], detail["error_code"]),
                             ("flash", "camera_usb_flash"))
            self.assertEqual(detail["exception_chain"][0]["backend_error_code"], -99)
            self.assertIsNone(detail["exception_chain"][0]["errno"])

    def test_idle_boundary_message_preserves_current_flash_eio_and_fragment(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            state = {
                "schema": "write-transaction-state-v1",
                "status": "UNKNOWN_AFTER_WRITE",
                "write_may_have_occurred": True,
                "reboot_may_have_occurred": False,
                "confirmed_fragments": 3118,
                "inflight": {"position": 3118, "region": "nflasha5", "index": 2605,
                             "start_sector": 234256, "sectors": 898, "attempts": 1,
                             "attempt_readbacks": []},
                "fragment_results": [],
                "error": "camera USB flash interrupted: [Errno 5] Input/Output Error",
            }
            (stage / "journal.json").write_text(json.dumps(state))
            detail = _restore_failure_details(
                RuntimeError("RAM session lost outside a safe reentry boundary"),
                "transaction", stage)
            self.assertEqual(detail["phase"], "flash")
            self.assertEqual(detail["error_code"], "camera_usb_flash")
            self.assertEqual(detail["failure_cause"], state["error"])
            self.assertEqual(detail["secondary_failure_cause"],
                             "RAM session lost outside a safe reentry boundary")
            self.assertEqual(detail["verified_fragments"], 3118)
            self.assertEqual(detail["pending_fragment"],
                             {"region": "nflasha5", "index": 2605,
                              "start_sector": 234256, "sectors": 898, "attempts": 1})

    def test_malformed_or_unrelated_journal_does_not_mask_current_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            (stage / "manifest.json").write_text("{}")
            (stage / "journal.json").write_text("{broken")
            detail = _restore_failure_details(
                RuntimeError("RAM session lost outside a safe reentry boundary"),
                "transaction", stage)
            self.assertEqual(detail["phase"], "transaction")
            self.assertIsNone(detail["write_may_have_occurred"])
            self.assertIsNone(detail["pending_fragment"])
            (stage / "journal.json").write_text(json.dumps({
                "schema": "write-transaction-state-v1", "status": "UNKNOWN_AFTER_WRITE",
                "write_may_have_occurred": True, "reboot_may_have_occurred": False,
                "confirmed_fragments": 1, "inflight": {"region": "nflasha5"},
                "error": "camera USB flash interrupted: EIO"}))
            detail = _restore_failure_details(
                RuntimeError("RAM session lost outside a safe reentry boundary"),
                "transaction", stage)
            self.assertEqual(detail["phase"], "transaction")
            self.assertIsNone(detail["pending_fragment"])
            (stage / "journal.json").write_text(json.dumps({
                "schema": "write-transaction-state-v1", "status": "UNKNOWN_AFTER_WRITE",
                "write_may_have_occurred": False, "reboot_may_have_occurred": False,
                "confirmed_fragments": 1,
                "inflight": {"region": "nflasha5", "index": 0,
                             "start_sector": 1, "sectors": 1, "attempts": 1},
                "error": "camera USB flash interrupted: EIO"}))
            detail = _restore_failure_details(
                RuntimeError("RAM session lost outside a safe reentry boundary"),
                "transaction", stage)
            self.assertEqual(detail["phase"], "transaction")
            self.assertEqual(detail["failure_cause"],
                             "RAM session lost outside a safe reentry boundary")
