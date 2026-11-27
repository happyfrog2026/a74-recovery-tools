import threading
import time
import unittest
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from recovery_tool.restore_session import IdleUSBSession
from recovery_tool.task_backend import Backend
from recovery_tool.snapshot import RESTORE_CURRENT_PROFILE
from recovery_tool.transport import USBReadError
from recovery_tool import write_transaction as tx


class IdleUSBSessionTests(unittest.TestCase):
    def test_retry_reset_requires_verified_prefix_and_valid_pending_intent(self):
        for case, confirmed, inflight, pending_valid, should_reenter in (
            ("verified", 1, None, False, True),
            ("failed_only", 0, None, False, False),
            ("valid_pending", 1, {"position": 1, "region": "nflasha5"}, True, True),
            ("bad_pending", 1, {"region": "nflasha5"}, False, False),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                job = Path(folder); current = job / "current-snapshot"
                segment = current / "segments/nflasha.first/0000000000.bin"
                segment.parent.mkdir(parents=True)
                data = b"A" * 512; segment.write_bytes(data)
                (current / "nflasha.raw.bin").write_bytes(data)
                (current / "verified.json").write_text(json.dumps({
                    "nflasha": {"bytes": 512}}))
                (current / "capture-receipt.json").write_text(json.dumps({
                    "status": "VERIFIED", "capture_profile": RESTORE_CURRENT_PROFILE,
                    "capture_regions": {"nflasha": 512},
                    "not_captured_regions": ["cp-nor"],
                    "segments": {"nflasha": {"first": [{
                        "file": "segments/nflasha.first/0000000000.bin",
                        "offset": 0, "bytes": 512,
                        "sha256": hashlib.sha256(data).hexdigest()}]}}}))
                stage = job / "scoped-stage"; stage.mkdir()
                pins = [{"path": str(current / name),
                         "sha256": tx.digest(current / name)} for name in
                        ("capture-receipt.json", "verified.json")]
                manifest = stage / "manifest.json"
                manifest.write_text(json.dumps({"input_files": pins}))
                (stage / "task.json").write_text(json.dumps({"kind": "scoped_restore",
                                                         "plan_digest": tx.digest(manifest)}))
                state = {"schema": "write-transaction-state-v1",
                         "status": "UNKNOWN_AFTER_WRITE" if inflight else "FRAGMENT_VERIFIED",
                         "write_may_have_occurred": confirmed > 0 or inflight is not None,
                         "reboot_may_have_occurred": False,
                         "confirmed_fragments": confirmed,
                         "restore_retry_used": 5, "inflight": inflight}
                (stage / "journal.json").write_text(json.dumps(state))
                (job / "restore-session-policy.json").write_text(json.dumps({
                    "schema": "restore-ram-session-v1", "retries_used": 5,
                    "verified_fragment_count": 0}))
                class Camera:
                    def __enter__(self): return self
                    def __exit__(self, *_): pass
                backend = Backend(transport_factory=lambda: self.fail("direct USB forbidden"))
                session = backend._restore_idle_session(job, {"inputs": {}}, stage, None)
                entries = []
                with patch("recovery_tool.task_backend.tx._verified_region_boundary", return_value=True), \
                     patch("recovery_tool.task_backend.tx._fragment_inflight_reentry_boundary",
                           return_value=pending_valid), \
                     patch.object(backend, "_transport", side_effect=lambda *_a, **_kw: (entries.append(1), Camera())[1]):
                    if should_reenter:
                        session.reenter()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "budget exhausted|readback evidence"):
                            session.reenter()
                self.assertEqual(len(entries), 1 if should_reenter else 0)
                saved = json.loads((job / "restore-session-policy.json").read_text())
                self.assertEqual(saved["retries_used"], 1 if should_reenter else 5)

    def test_legacy_full_capture_is_admitted_without_reading_cp_nor(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            current = job / "current-snapshot"
            segment = current / "segments/nflasha.first/0000000000.bin"
            segment.parent.mkdir(parents=True)
            data = b"A" * 512
            segment.write_bytes(data)
            (current / "nflasha.raw.bin").write_bytes(data)
            (current / "verified.json").write_text(json.dumps({
                name: {"bytes": 512} for name in
                ("nflashaB0", "nflashaB1", "nflasha", "cp-nor")}))
            (current / "capture-receipt.json").write_text(json.dumps({
                "status": "VERIFIED", "segments": {"nflasha": {"first": [{
                    "file": "segments/nflasha.first/0000000000.bin", "offset": 0,
                    "bytes": 512, "sha256": hashlib.sha256(data).hexdigest()}]}}}))
            class Camera:
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def verify_product(self): return "ILCE-7M4"
                def read_region(self, region, path, *, offset, length):
                    self.asserted_region = region
                    Path(path).write_bytes(data)
            backend = Backend(transport_factory=Camera)
            session = backend._restore_idle_session(job, {"inputs": {}}, job / "scoped-stage", None)
            self.assertEqual(session.run_host(lambda cancel, progress: time.sleep(0.05)), None)
            self.assertFalse((job / "session-probes/cp-nor.bin").exists())

    def test_backend_reenters_during_host_work_and_persists_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            current = job / "current-snapshot"
            segment = current / "segments/nflasha.first/0000000000.bin"
            segment.parent.mkdir(parents=True)
            data = b"A" * 512
            segment.write_bytes(data)
            (current / "nflasha.raw.bin").write_bytes(data)
            (current / "verified.json").write_text(json.dumps({
                name: {"bytes": 512} for name in ("nflashaB0", "nflashaB1", "nflasha")}))
            (current / "capture-receipt.json").write_text(json.dumps({
                "status": "VERIFIED", "capture_profile": RESTORE_CURRENT_PROFILE,
                "not_captured_regions": ["cp-nor"],
                "segments": {"nflasha": {"first": [{
                    "file": "segments/nflasha.first/0000000000.bin", "offset": 0,
                    "bytes": 512, "sha256": hashlib.sha256(data).hexdigest()}]}}}))
            ready = False
            reentries = []
            class Camera:
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def verify_product(self): return "ILCE-7M4"
                def read_region(self, _region, path, *, offset, length):
                    Path(path).write_bytes(data)
            def direct():
                if not ready:
                    from recovery_tool.transport import FastbootEnumerationUnavailable
                    raise FastbootEnumerationUnavailable("ILCE-7M4 RAM Fastboot USB device unavailable")
                return Camera()
            def bridge(*_args, **_kwargs):
                nonlocal ready
                ready = True
                reentries.append(1)
                return Camera()
            backend = Backend(transport_factory=direct)
            session = backend._restore_idle_session(job, {"inputs": {}}, job / "scoped-stage", None)
            with patch.object(backend, "_transport", side_effect=bridge), \
                 patch("recovery_tool.card_connection._blocking_tasks", return_value=[]):
                self.assertEqual(session.run_host(lambda cancel, progress: "verified" if reentries else
                                                  (time.sleep(0.6), "verified")[1]), "verified")
            self.assertEqual(len(reentries), 1)
            policy = json.loads((job / "restore-session-policy.json").read_text())
            self.assertEqual(policy["retries_used"], 1)

    def test_nontransient_usb_access_error_never_resets_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            current = job / "current-snapshot"
            segment = current / "segments/nflasha.first/0000000000.bin"
            segment.parent.mkdir(parents=True)
            data = b"A" * 512
            segment.write_bytes(data)
            (current / "nflasha.raw.bin").write_bytes(data)
            (current / "verified.json").write_text(json.dumps({
                name: {"bytes": 512} for name in ("nflashaB0", "nflashaB1", "nflasha")}))
            (current / "capture-receipt.json").write_text(json.dumps({
                "status": "VERIFIED", "capture_profile": RESTORE_CURRENT_PROFILE,
                "not_captured_regions": ["cp-nor"],
                "segments": {"nflasha": {"first": [{
                    "file": "segments/nflasha.first/0000000000.bin", "offset": 0,
                    "bytes": 512, "sha256": hashlib.sha256(data).hexdigest()}]}}}))
            def denied():
                raise USBReadError("camera access denied", OSError(13, "access denied"))
            backend = Backend(transport_factory=denied)
            session = backend._restore_idle_session(job, {"inputs": {}}, job / "scoped-stage", None)
            with patch.object(backend, "_transport", side_effect=AssertionError("reset forbidden")):
                with self.assertRaisesRegex(USBReadError, "access denied"):
                    session.run_host(lambda cancel, progress: time.sleep(0.05))

    def test_bad_saved_capture_anchor_rejected_before_any_usb(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            current = job / "current-snapshot"
            segment = current / "segments/nflasha.first/0000000000.bin"
            segment.parent.mkdir(parents=True)
            segment.write_bytes(b"B" * 512)
            (current / "nflasha.raw.bin").write_bytes(b"A" * 512)
            (current / "verified.json").write_text(json.dumps({
                name: {"bytes": 512} for name in ("nflashaB0", "nflashaB1", "nflasha")}))
            (current / "capture-receipt.json").write_text(json.dumps({
                "status": "VERIFIED", "capture_profile": RESTORE_CURRENT_PROFILE,
                "not_captured_regions": ["cp-nor"],
                "segments": {"nflasha": {"first": [{
                    "file": "segments/nflasha.first/0000000000.bin", "offset": 0,
                    "bytes": 512, "sha256": hashlib.sha256(b"A" * 512).hexdigest()}]}}}))
            backend = Backend(transport_factory=lambda: self.fail("USB must remain untouched"))
            with self.assertRaisesRegex(ValueError, "first segment changed"):
                backend._restore_idle_session(job, {"inputs": {}}, job / "scoped-stage", None)

    def test_lost_ram_reenters_while_local_hash_is_still_running(self):
        host_running = threading.Event()
        release_host = threading.Event()
        events = []
        ready = False
        def work(cancelled, progress):
            host_running.set()
            progress({"stage": "restore_local_verify", "bytes_done": 1})
            while not release_host.wait(0.005):
                if cancelled.is_set():
                    raise RuntimeError("cancelled")
            events.append("host_done")
            return "verified"
        def probe():
            self.assertEqual(threading.current_thread(), threading.main_thread())
            self.assertTrue(host_running.is_set())
            return ready
        def reenter():
            nonlocal ready
            events.append("reentry")
            self.assertFalse(release_host.is_set())
            ready = True
            release_host.set()
        session = IdleUSBSession(probe=probe, reenter=reenter,
                                 can_reenter=lambda: True,
                                 progress=lambda event: events.append(event["stage"]),
                                 interval_seconds=0.01)
        self.assertEqual(session.run_host(work), "verified")
        self.assertLess(events.index("reentry"), events.index("host_done"))

    def test_unsafe_boundary_cancels_host_without_reentry(self):
        ended = threading.Event()
        called = []
        def work(cancelled, progress):
            try:
                while not cancelled.wait(0.005):
                    pass
                raise RuntimeError("cancelled")
            finally:
                ended.set()
        session = IdleUSBSession(probe=lambda: False,
                                 reenter=lambda: called.append("reset"),
                                 can_reenter=lambda: False,
                                 interval_seconds=0.01)
        with self.assertRaisesRegex(RuntimeError, "safe reentry boundary"):
            session.run_host(work)
        self.assertTrue(ended.is_set())
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
