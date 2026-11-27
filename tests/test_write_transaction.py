"""Failure injection for durable sector reconciliation and one-shot reboot."""

import json
from pathlib import Path
import tempfile
import unittest
import usb.core
from unittest.mock import patch

from recovery_tool import write_transaction as tx
from recovery_tool.transport import (FastbootError, FastbootEnumerationUnavailable,
                                     USBReadError, DestinationIOError)


SECTOR = 512


class FakeCamera:
    def __init__(self, original, identity):
        self.images = {"nflasha2": original, "nflasha": identity}
        self.reboots = 0
        self.fail_reboot = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def close(self):
        pass

    def region_size(self, name):
        return len(self.images[name])

    def verify_product(self):
        return "ILCE-7M4"

    def read_region(self, name, path, *, offset=0, length=None):
        data = self.images[name]
        path = Path(path)
        if path.exists() or path.with_name(path.name + ".partial").exists():
            raise FileExistsError(f"real Fastboot read refuses existing output: {path}")
        path.write_bytes(data[offset:] if length is None else data[offset:offset + length])

    def _send(self, command):
        if command == "reboot":
            self.reboots += 1
            if self.fail_reboot:
                raise OSError("ACK vanished")

    def _reply(self):
        return b"OKAY", b""


class TransactionAcceptance(unittest.TestCase):
    def _prepare_fragment_mode(self):
        before = b"A" * SECTOR * 5
        after = (b"A" * SECTOR + b"X" * SECTOR + b"A" * SECTOR +
                 b"Y" * SECTOR + b"A" * SECTOR)
        self.original.write_bytes(before)
        self.target.write_bytes(after)
        self.camera.images["nflasha2"] = before[:SECTOR] + b"Z" * SECTOR + before[2*SECTOR:]
        self.fragment_before, self.fragment_after = before, after
        fragments, _ = tx._fragments(self.original, self.target, 5,
                                    [[1, 2], [3, 4]], target_digests=True)
        with patch.object(tx, "_validate_capability_scope"):
            tx.prepare(self.job, kind="scoped_restore",
                       regions=[{"name": "nflasha2", "original": self.original,
                                 "target": self.target}],
                       identity=[{"region": "nflasha", "bytes": SECTOR,
                                  "sha256": tx.digest(self._identity_file())}],
                       input_files=[], metadata={"capability_profile": tx.CAPABILITY_PROFILE,
                                                 "validation_mode": tx.FRAGMENT_READBACK_MODE,
                                                 "offline_plan": {"partitions": [
                                                     {"region": "nflasha2",
                                                      "current_sha256": tx.digest(self.original),
                                                      "target_sha256": tx.digest(self.target),
                                                      "fragments": fragments}]}})

    def test_fragment_mode_accepts_later_live_drift_and_checks_each_fragment(self):
        self._prepare_fragment_mode()
        writes = []
        def flash(_transport, name, _payload, _count, **kwargs):
            self.assertTrue(kwargs.get("capability_restore"))
            start = (1, 3)[len(writes)]
            image = bytearray(self.camera.images[name])
            image[start*SECTOR:(start+1)*SECTOR] = self.fragment_after[start*SECTOR:(start+1)*SECTOR]
            self.camera.images[name] = bytes(image)
            writes.append(start)
        events = []
        with patch.object(tx, "_validate_capability_scope"), patch.object(tx, "_write_response", side_effect=flash):
            result = tx.apply(self.job, transport_factory=lambda: self.camera, progress=events.append)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(writes, [1, 3])
        self.assertEqual([event["stage"] for event in events
                          if event["stage"].startswith("fragment_write_")],
                         ["fragment_write_begin", "fragment_write_verified", "fragment_write_verified"])
        self.assertTrue(all("written and readback verified" in event["message"]
                            for event in events if event["stage"] == "fragment_write_verified"))
        self.assertEqual([(event["completed_fragments"], event["planned_fragments"],
                           event["verified_fragments"], event["failed_fragments"])
                          for event in events if event["stage"] == "fragment_write_verified"],
                         [(1, 2, 1, 0), (2, 2, 2, 0)])
        self.assertEqual([x["status"] for x in result["fragment_results"]], ["verified", "verified"])
        self.assertFalse((self.job / "reconcile-001/nflasha2.readback.bin").exists())
        self.assertEqual(tx.verify_fragment_results(self.job,
                         json.loads((self.job / "manifest.json").read_text()), result)["verified"], 2)

    def test_fragment_mode_two_mismatches_record_partial_and_continue(self):
        self._prepare_fragment_mode()
        calls = []
        def flash(_transport, name, _payload, _count, **_):
            calls.append(len(calls))
            if len(calls) == 3:
                image = bytearray(self.camera.images[name])
                image[3*SECTOR:4*SECTOR] = self.fragment_after[3*SECTOR:4*SECTOR]
                self.camera.images[name] = bytes(image)
        events = []
        with patch.object(tx, "_validate_capability_scope"), patch.object(tx, "_write_response", side_effect=flash):
            result = tx.apply(self.job, transport_factory=lambda: self.camera, progress=events.append)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(calls), 3)
        self.assertEqual([(x["status"], x["attempts"]) for x in result["fragment_results"]],
                         [("failed", 2), ("verified", 1)])
        self.assertEqual(result["fragment_summary"]["failed"], 1)
        self.assertEqual([event["stage"] for event in events
                          if event["stage"] in {"fragment_write_verified", "fragment_write_failed"}],
                         ["fragment_write_failed", "fragment_write_verified"])
        self.assertIn("failed after 2 attempts",
                      next(event["message"] for event in events
                           if event["stage"] == "fragment_write_failed"))
        self.assertEqual([(event["completed_fragments"], event["planned_fragments"],
                           event["verified_fragments"], event["failed_fragments"])
                          for event in events if event["stage"] in
                          {"fragment_write_failed", "fragment_write_verified"}],
                         [(1, 2, 0, 1), (2, 2, 1, 1)])

    def test_fragment_mode_lost_ack_reads_current_fragment_before_resend(self):
        self._prepare_fragment_mode()
        calls = []
        def flash(_transport, name, _payload, _count, **_):
            start = (1, 3)[len(calls)]
            image = bytearray(self.camera.images[name])
            image[start*SECTOR:(start+1)*SECTOR] = self.fragment_after[start*SECTOR:(start+1)*SECTOR]
            self.camera.images[name] = bytes(image)
            calls.append(start)
            if len(calls) == 1:
                raise OSError(19, "lost ACK")
        with patch.object(tx, "_validate_capability_scope"), patch.object(tx, "_write_response", side_effect=flash):
            with self.assertRaises(tx.CameraWriteIOError):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            self.assertEqual(json.loads((self.job / "journal.json").read_text())["inflight"]["attempts"], 1)
            result = tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(calls, [1, 3])

    def test_flash_usb_other_is_typed_with_backend_code_without_a_reconnect_callback(self):
        self._prepare_fragment_mode()
        calls = []
        def flash(*_args, **_kwargs):
            calls.append(1)
            raise usb.core.USBError("Other error", -99)
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", side_effect=flash):
            with self.assertRaises(tx.CameraWriteIOError) as raised:
                tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(raised.exception.errno)
        self.assertEqual(raised.exception.backend_error_code, -99)
        state = json.loads((self.job / "journal.json").read_text())
        self.assertEqual(state["status"], "UNKNOWN_AFTER_WRITE")
        self.assertEqual(state["inflight"]["attempts"], 1)
        self.assertEqual(state["error_detail"], {
            "type": "CameraWriteIOError", "stage": "flash", "errno": None,
            "backend_error_type": "USBError", "backend_error_code": -99})

    def test_fragment_mode_unreadable_inflight_reenters_bounded_without_rewrite(self):
        self._prepare_fragment_mode()
        read = self.camera.read_region
        def fail_read(name, path, *, offset=0, length=None):
            if name == "nflasha2" and "fragment-readbacks" in str(path):
                raise USBReadError("camera read interrupted", OSError(5, "EIO"))
            return read(name, path, offset=offset, length=length)
        self.camera.read_region = fail_read
        resets = []
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", return_value=None) as write:
            with self.assertRaisesRegex(tx.TransactionError, "budget exhausted"):
                tx.apply_with_reconnect(self.job, transport_factory=lambda: self.camera,
                                        reconnect=lambda **kwargs: resets.append(kwargs))
        state = json.loads((self.job / "journal.json").read_text())
        self.assertEqual(state["status"], "UNKNOWN_AFTER_WRITE")
        self.assertEqual(state["inflight"]["attempts"], 1)
        self.assertEqual(len(resets), 5)
        self.assertTrue(all(row["force_bridge"] for row in resets))
        self.assertEqual(write.call_count, 1)
        self.assertFalse(tx._fragment_boundary(self.job,
                         json.loads((self.job / "manifest.json").read_text()), state))

    def test_read_interruption_forces_bridge_even_without_inflight(self):
        self._prepare_fragment_mode()
        self.camera.verify_product = lambda: (_ for _ in ()).throw(
            USBReadError("camera read interrupted", OSError(5, "EIO")))
        resets = []
        with patch.object(tx, "_validate_capability_scope"):
            with self.assertRaisesRegex(tx.TransactionError, "budget exhausted"):
                tx.apply_with_reconnect(self.job, transport_factory=lambda: self.camera,
                                        reconnect=lambda *, force_bridge: resets.append(force_bridge))
        self.assertEqual(resets, [True] * 5)

    def test_reentry_typed_unavailability_uses_budget_then_new_handle(self):
        self._prepare_fragment_mode()
        resets = []
        def reconnect(*, force_bridge):
            resets.append(force_bridge)
            if len(resets) <= 2:
                raise FastbootEnumerationUnavailable("DATA USB unavailable")
        self.camera.verify_product = lambda: (_ for _ in ()).throw(
            FastbootEnumerationUnavailable("DATA USB unavailable"))
        with patch.object(tx, "_validate_capability_scope"):
            with self.assertRaisesRegex(tx.TransactionError, "budget exhausted"):
                tx.apply_with_reconnect(self.job, transport_factory=lambda: self.camera,
                                        reconnect=reconnect)
        self.assertEqual(resets, [False, True, True, False, False])

    def test_timeout_code_is_retryable_only_from_camera_usb_window(self):
        timeout = usb.core.USBTimeoutError("timeout", -7)
        read_error = USBReadError("camera read timeout", timeout)
        self.assertTrue(tx._retryable_fragment_usb(read_error))
        self.assertFalse(tx._retryable_fragment_usb(OSError(60, "local timeout")))
        self.assertFalse(tx._retryable_fragment_usb(
            USBReadError("camera permission", usb.core.USBError("access", -3))))
        self.assertFalse(tx._fragment_transport_unavailable(
            USBReadError("Fastboot unavailable: access denied", OSError(13, "access"))))
        dispose_failed = USBReadError("camera unavailable", OSError(5, "EIO"))
        dispose_failed.usb_cleanup_dispose_failed = True
        self.assertFalse(tx._fragment_transport_unavailable(dispose_failed))

    def test_lost_flash_ack_forces_new_handle_then_reads_target_without_resend(self):
        self._prepare_fragment_mode()
        events = []; writes = []; progress = []
        camera = self.camera
        class Handle:
            def __init__(self, number): self.number = number
            def __enter__(self): events.append(("open", self.number)); return self
            def __exit__(self, *_): events.append(("close", self.number))
            def verify_product(self): return camera.verify_product()
            def region_size(self, name): return camera.region_size(name)
            def read_region(self, name, path, *, offset=0, length=None):
                if "fragment-readbacks" in str(path): events.append(("readback", self.number))
                return camera.read_region(name, path, offset=offset, length=length)
        def factory(): return Handle(1 + sum(item[0] == "open" for item in events))
        def flash(handle, name, _payload, _count, **_):
            writes.append((handle.number, name))
            start = (1, 3)[len(writes)-1]
            image = bytearray(camera.images[name])
            image[start*SECTOR:(start+1)*SECTOR] = self.fragment_after[start*SECTOR:(start+1)*SECTOR]
            camera.images[name] = bytes(image)
            if len(writes) == 1:
                raise usb.core.USBError("Pipe error", -9, 32)
        def reconnect(*, force_bridge): events.append(("bridge", force_bridge))
        with patch.object(tx.sys, "platform", "win32"), patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", side_effect=flash):
            result = tx.apply_with_reconnect(self.job, transport_factory=factory,
                                             reconnect=reconnect, progress=progress.append)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(writes, [(1, "nflasha2"), (2, "nflasha2")])
        self.assertEqual(events[:5], [("open", 1), ("close", 1),
                                      ("bridge", True), ("open", 2), ("readback", 2)])
        self.assertEqual(result["fragment_results"][0]["attempts"], 1)
        self.assertTrue(any("skipping rewrite" in event["message"] for event in progress))

    def test_lost_flash_ack_partial_target_rewrites_once_after_full_read(self):
        self._prepare_fragment_mode()
        events=[]; writes=[]; progress=[]
        read=self.camera.read_region
        def capture_read(name,path,*,offset=0,length=None):
            if "fragment-readbacks" in str(path): events.append("read")
            return read(name,path,offset=offset,length=length)
        self.camera.read_region=capture_read
        def flash(_transport,name,_payload,_count,**_):
            writes.append(name);events.append("write")
            if len(writes)==1:
                raise usb.core.USBError("Other error",-99)
            start=(1,3)[len(writes)-2]
            image=bytearray(self.camera.images[name])
            image[start*SECTOR:(start+1)*SECTOR]=self.fragment_after[start*SECTOR:(start+1)*SECTOR]
            self.camera.images[name]=bytes(image)
        with patch.object(tx,"_validate_capability_scope"), \
             patch.object(tx,"_write_response",side_effect=flash):
            result=tx.apply_with_reconnect(self.job,
                    transport_factory=lambda:self.camera,
                    reconnect=lambda *,force_bridge:events.append("bridge"), progress=progress.append)
        self.assertEqual(result["status"],"VERIFIED")
        self.assertEqual(writes,["nflasha2"]*3)
        self.assertEqual(events[:4],["write","bridge","read","write"])
        self.assertEqual(result["fragment_results"][0]["attempts"],2)
        self.assertTrue(any("rewriting once" in event["message"] for event in progress))

    def test_second_unknown_flash_is_read_then_failed_without_third_write(self):
        self._prepare_fragment_mode()
        writes=[]; resets=[]
        def flash(_transport,name,_payload,_count,**_):
            writes.append(name)
            if len(writes)<=2:
                raise usb.core.USBError("Other error",-99)
            image=bytearray(self.camera.images[name])
            image[3*SECTOR:4*SECTOR]=self.fragment_after[3*SECTOR:4*SECTOR]
            self.camera.images[name]=bytes(image)
        with patch.object(tx,"_validate_capability_scope"), \
             patch.object(tx,"_write_response",side_effect=flash):
            result=tx.apply_with_reconnect(self.job,
                transport_factory=lambda:self.camera,
                reconnect=lambda *,force_bridge:resets.append(force_bridge))
        self.assertEqual(result["status"],"PARTIAL")
        self.assertEqual(writes,["nflasha2"]*3)
        self.assertEqual(resets,[True,True])
        self.assertEqual([(row["status"],row["attempts"]) for row in result["fragment_results"]],
                         [("failed",2),("verified",1)])

    def test_usb_access_error_and_generic_local_error_do_not_reenter(self):
        for number, failure in enumerate((usb.core.USBError("access denied",-3),
                                          OSError(None,"local failure"))):
            with self.subTest(failure=str(failure)):
                if number:
                    self.job=self.root/f"error-{number}"
                self._prepare_fragment_mode()
                resets=[]
                with patch.object(tx,"_validate_capability_scope"), \
                     patch.object(tx,"_write_response",side_effect=failure):
                    with self.assertRaises((tx.CameraWriteIOError,OSError)):
                        tx.apply_with_reconnect(self.job,
                            transport_factory=lambda:self.camera,
                            reconnect=lambda *,force_bridge:resets.append(force_bridge))
                self.assertEqual(resets,[])
                self.assertEqual(json.loads((self.job/"journal.json").read_text())["inflight"]["attempts"],1)

    def test_tampered_pending_intent_refuses_bridge_before_any_new_io(self):
        self._prepare_fragment_mode()
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", side_effect=usb.core.USBError("Other error", -99)):
            with self.assertRaises(tx.CameraWriteIOError):
                tx.apply(self.job, transport_factory=lambda: self.camera)
        path = self.job / "journal.json"
        state = json.loads(path.read_text())
        state["inflight"]["target_sha256"] = "0" * 64
        tx.durable_json(path, state)
        with patch.object(tx, "_validate_capability_scope"):
            with self.assertRaisesRegex(tx.TransactionError, "saved fragment intent differs"):
                tx.apply_with_reconnect(self.job, transport_factory=lambda: self.fail("no open"),
                                        reconnect=lambda **_: self.fail("no reset"))

    def test_new_verified_prefix_resets_exhausted_budget_with_next_pending(self):
        self._prepare_fragment_mode()
        calls = 0
        def flash(_transport, name, _payload, _count, **_):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise usb.core.USBError("Other error", -99)
            image = bytearray(self.camera.images[name])
            image[SECTOR:2 * SECTOR] = self.fragment_after[SECTOR:2 * SECTOR]
            self.camera.images[name] = bytes(image)
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", side_effect=flash):
            with self.assertRaises(tx.CameraWriteIOError):
                tx.apply(self.job, transport_factory=lambda: self.camera)
        path = self.job / "journal.json"
        state = json.loads(path.read_text())
        self.assertEqual((state["confirmed_fragments"], state["inflight"]["position"]), (1, 1))
        state["restore_retry_used"] = 5
        state["restore_retry_verified_fragments"] = 0
        tx.durable_json(path, state)
        resets = []
        with patch.object(tx, "_validate_capability_scope"):
            with self.assertRaisesRegex(tx.TransactionError, "budget exhausted"):
                tx.apply_with_reconnect(self.job, transport_factory=lambda: self.camera,
                    reconnect=lambda **_: (resets.append(1),
                                           (_ for _ in ()).throw(tx.RamPayloadNotReady("not ready"))))
        self.assertEqual(len(resets), 5)
        self.assertEqual(json.loads(path.read_text())["restore_retry_used"], 5)

    def test_fragment_mode_short_readback_is_unknown_not_normal_mismatch(self):
        self._prepare_fragment_mode()
        read = self.camera.read_region
        writes = []
        def short_read(name, path, *, offset=0, length=None):
            if name == "nflasha2" and "fragment-readbacks" in str(path):
                Path(path).write_bytes(b"Q" * (SECTOR // 2))
                return None
            return read(name, path, offset=offset, length=length)
        self.camera.read_region = short_read
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", side_effect=lambda *_args, **_kw: writes.append(1)):
            with self.assertRaisesRegex(tx.TransactionError, "fragment readback incomplete"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(writes, [1])
        state = json.loads((self.job / "journal.json").read_text())
        self.assertEqual(state["status"], "UNKNOWN_AFTER_WRITE")
        self.assertEqual(state["inflight"]["attempts"], 1)

    def test_fragment_mode_empty_content_identity_still_requires_product(self):
        self._prepare_fragment_mode()
        manifest_path = self.job / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["identity"] = []
        tx.durable_json(manifest_path, manifest)
        self.camera.verify_product = lambda: "OTHER"
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response") as flash:
            with self.assertRaisesRegex(tx.TransactionError, "unexpected fragment restore Fastboot product"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            flash.assert_not_called()

    def test_fragment_mode_tampered_saved_readback_refuses_verified_reopen(self):
        self._prepare_fragment_mode()
        calls = []
        def flash(_transport, name, _payload, _count, **_):
            start = (1, 3)[len(calls)]
            image = bytearray(self.camera.images[name])
            image[start*SECTOR:(start+1)*SECTOR] = self.fragment_after[start*SECTOR:(start+1)*SECTOR]
            self.camera.images[name] = bytes(image)
            calls.append(start)
        with patch.object(tx, "_validate_capability_scope"), patch.object(tx, "_write_response", side_effect=flash):
            result = tx.apply(self.job, transport_factory=lambda: self.camera)
            first = self.job / result["fragment_results"][0]["readback_path"]
            first.write_bytes(b"Q" * SECTOR)
            with self.assertRaisesRegex(tx.TransactionError, "saved fragment readback missing or changed"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(calls, [1, 3])

    def test_fragment_mode_crash_after_matching_observation_never_resends(self):
        self._prepare_fragment_mode()
        writes = []
        save = tx.durable_json
        crashed = False
        def flash(_transport, name, _payload, _count, **_):
            start = (1, 3)[len(writes)]
            image = bytearray(self.camera.images[name])
            image[start*SECTOR:(start+1)*SECTOR] = self.fragment_after[start*SECTOR:(start+1)*SECTOR]
            self.camera.images[name] = bytes(image)
            writes.append(start)
        def durable(path, content):
            nonlocal crashed
            save(path, content)
            current = content.get("inflight") if isinstance(content, dict) else None
            if (not crashed and Path(path).name == "journal.json" and current and
                    current.get("last_observed_attempt") == 1):
                crashed = True
                raise RuntimeError("killed after observation")
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", side_effect=flash), \
             patch.object(tx, "durable_json", side_effect=durable):
            with self.assertRaisesRegex(RuntimeError, "killed after observation"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            result = tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(writes, [1, 3])
        self.assertEqual(result["fragment_results"][0]["attempts"], 1)

    def test_fragment_mode_crash_after_second_mismatch_records_failed_without_third_write(self):
        self._prepare_fragment_mode()
        save = tx.durable_json
        writes = []
        crashed = False
        def flash(_transport, name, _payload, _count, **_):
            writes.append(name)
            if len(writes) == 3:
                image = bytearray(self.camera.images[name])
                image[3*SECTOR:4*SECTOR] = self.fragment_after[3*SECTOR:4*SECTOR]
                self.camera.images[name] = bytes(image)
        def durable(path, content):
            nonlocal crashed
            save(path, content)
            current = content.get("inflight") if isinstance(content, dict) else None
            if (not crashed and Path(path).name == "journal.json" and current and
                    current.get("last_observed_attempt") == 2):
                crashed = True
                raise RuntimeError("killed after second mismatch")
        with patch.object(tx, "_validate_capability_scope"), \
             patch.object(tx, "_write_response", side_effect=flash), \
             patch.object(tx, "durable_json", side_effect=durable):
            with self.assertRaisesRegex(RuntimeError, "killed after second mismatch"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            result = tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(writes), 3)
        self.assertEqual(result["fragment_results"][0]["attempts"], 2)

    def test_idle_reentry_budget_persisted_during_host_hash_precedes_device(self):
        stage = self.root / "host-verified"
        stage.mkdir()
        manifest = {"schema": "write-transaction-v1", "kind": "scoped_restore",
                    "metadata": {"capability_profile": tx.CAPABILITY_PROFILE}, "regions": []}
        old = {"schema": "write-transaction-state-v1", "status": "READY", "epoch": 0,
               "write_may_have_occurred": False, "reboot_may_have_occurred": False,
               "confirmed_fragments": 0, "inflight": None, "restore_retry_used": 0}
        tx.durable_json(stage / "manifest.json", manifest)
        tx.durable_json(stage / "journal.json", old)
        events = []
        def host_load():
            events.append("host_hash")
            live = dict(old, restore_retry_used=2)
            tx.durable_json(stage / "journal.json", live)
            return manifest, old
        def apply_loaded(_stage, _manifest, state, **_):
            events.append("device")
            self.assertEqual(state["restore_retry_used"], 2)
            return {"status": "VERIFIED"}
        with patch.object(tx, "_apply_loaded", side_effect=apply_loaded):
            result = tx.apply_with_reconnect(stage, host_load=host_load)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(events, ["host_hash", "device"])

    def test_possible_write_host_hash_cannot_roll_back_durable_retry_budget(self):
        stage = self.root / "host-verified-after-write"
        stage.mkdir()
        manifest = {"schema": "write-transaction-v1", "kind": "scoped_restore",
                    "metadata": {"capability_profile": tx.CAPABILITY_PROFILE}, "regions": []}
        saved = {"schema": "write-transaction-state-v1", "status": "UNKNOWN_AFTER_WRITE",
                 "epoch": 1, "write_may_have_occurred": True,
                 "reboot_may_have_occurred": False, "confirmed_fragments": 0,
                 "inflight": None, "last_reconcile": None, "restore_retry_used": 2}
        tx.durable_json(stage / "manifest.json", manifest)
        tx.durable_json(stage / "journal.json", saved)
        calls = {"hash": 0, "device": 0}
        def host_load():
            calls["hash"] += 1
            stale = dict(saved)
            if calls["hash"] == 2:
                tx.durable_json(stage / "journal.json", dict(saved, restore_retry_used=5))
            return manifest, stale
        def device_attempt(*_args, **_kwargs):
            calls["device"] += 1
            raise USBReadError("camera read interrupted", OSError(5, "EIO"))
        with patch.object(tx, "_apply_loaded", side_effect=device_attempt):
            with self.assertRaises(USBReadError):
                tx.apply_with_reconnect(stage, host_load=host_load)
        self.assertEqual(calls, {"hash": 2, "device": 1})
        self.assertEqual(json.loads((stage / "journal.json").read_text())["restore_retry_used"], 5)




    def test_restore_reconnect_reconciles_before_resending_uncertain_flash(self):
        self.prepare()
        attempts = []
        events = []

        def interrupted_then_finish(_transport, _name, _payload, _count):
            attempts.append(1)
            if len(attempts) == 1:
                # First sector may have reached the camera despite lost ACK.
                self.camera.images["nflasha2"] = (self.before[:SECTOR] + self.after[SECTOR:2*SECTOR]
                                                 + self.before[2*SECTOR:])
                raise OSError(19, "USB disconnected")
            self.assertEqual(self.camera.images["nflasha2"],
                             self.before[:SECTOR] + self.after[SECTOR:2*SECTOR] + self.before[2*SECTOR:])
            self.camera.images["nflasha2"] = self.after

        with patch.object(tx, "_write_response", side_effect=interrupted_then_finish):
            result = tx.apply_with_reconnect(self.job, transport_factory=lambda: self.camera,
                                             progress=events.append)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(len(attempts), 2)
        self.assertEqual([event["retry"] for event in events if event["stage"] == "restore_retry"], [1])
        self.assertGreaterEqual(json.loads((self.job / "journal.json").read_text())["epoch"], 2)

    def test_restore_missing_fastboot_after_uncertain_flash_never_reenters(self):
        self.prepare()
        opens = 0

        def transport_factory():
            nonlocal opens
            opens += 1
            if opens > 1:
                raise FastbootError("RAM Fastboot USB device unavailable")
            return self.camera

        with patch.object(tx, "_write_response", side_effect=OSError(19, "USB disconnected")), \
             patch.object(tx, "_apply_loaded", wraps=tx._apply_loaded) as apply, \
             patch.object(tx, "_identity", wraps=tx._identity):
            reentry = []
            with self.assertRaisesRegex(FastbootError, "unavailable"):
                tx.apply_with_reconnect(self.job, transport_factory=transport_factory,
                                        reconnect=lambda: reentry.append(True))
        self.assertEqual(opens, 2)
        self.assertEqual(reentry, [])
        self.assertEqual(apply.call_count, 2)
        self.assertIsNotNone(json.loads((self.job / "journal.json").read_text()).get("inflight"))

    def test_restore_destination_failure_does_not_retry(self):
        self.prepare()
        opens = 0

        def factory():
            nonlocal opens
            opens += 1
            raise DestinationIOError(5, "local disk failed")

        with self.assertRaises(DestinationIOError):
            tx.apply_with_reconnect(self.job, transport_factory=factory,
                                    reconnect=lambda: self.fail("no camera reentry"))
        self.assertEqual(opens, 1)

    def test_restore_retry_budget_survives_explicit_resume(self):
        self.prepare()
        journal = self.job / "journal.json"
        state = json.loads(journal.read_text())
        state["restore_retry_used"] = 4
        tx.durable_json(journal, state)
        opens = 0

        def factory():
            nonlocal opens
            opens += 1
            return self.camera

        with patch.object(tx, "_write_response", side_effect=OSError(19, "USB disconnected")):
            with self.assertRaises(tx.CameraWriteIOError):
                tx.apply_with_reconnect(self.job, transport_factory=factory)
            self.assertEqual(json.loads(journal.read_text())["restore_retry_used"], 5)
            self.assertEqual(opens, 2)
            with self.assertRaises(tx.CameraWriteIOError):
                tx.apply_with_reconnect(self.job, transport_factory=factory)
        self.assertEqual(opens, 3)  # The resumed invocation gets no sixth reconnect.

    def test_restore_third_value_stops_before_retry_or_write(self):
        self.prepare()
        self.camera.images["nflasha2"] = self.before[:SECTOR] + b"Z" * SECTOR + self.before[2*SECTOR:]
        with patch.object(tx, "_write_response") as write:
            with self.assertRaisesRegex(tx.TransactionError, "unrecognized live sectors"):
                tx.apply_with_reconnect(self.job, transport_factory=lambda: self.camera,
                                        reconnect=lambda: self.fail("no bridge reentry"))
            write.assert_not_called()
        self.assertNotIn("restore_retry_used", json.loads((self.job / "journal.json").read_text()))

    def test_restore_prewrite_fastboot_absent_can_reenter_once(self):
        self.prepare()
        opens = 0
        reentries = []

        def factory():
            nonlocal opens
            opens += 1
            if opens == 1:
                raise FastbootError("RAM Fastboot USB device unavailable")
            return self.camera

        def finish(_transport, _name, _payload, _count):
            self.camera.images["nflasha2"] = self.after

        with patch.object(tx, "_write_response", side_effect=finish):
            result = tx.apply_with_reconnect(self.job, transport_factory=factory,
                                             reconnect=lambda: reentries.append(True))
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(reentries, [True])
        self.assertEqual(json.loads((self.job / "journal.json").read_text())["restore_retry_used"], 1)

    def test_one_apply_verifies_local_pins_once_before_device_reconcile(self):
        self.prepare()
        def finish(_transport, _name, _payload, _count):
            self.camera.images["nflasha2"] = self.after
        with patch.object(tx, "load", wraps=tx.load) as validated, \
             patch.object(tx, "_write_response", side_effect=finish):
            result = tx.apply_with_reconnect(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(validated.call_count, 1)

    def test_interrupted_full_prewrite_resumes_saved_chunks_same_epoch_before_flash(self):
        self.prepare()
        from recovery_tool.resumable_region_read import read_full_region
        actual_read = self.camera.read_region
        offsets = []
        failed = False
        def interrupted(name, path, *, offset=0, length=None):
            nonlocal failed
            if name == "nflasha2" and ".live.bin.segments" in str(path):
                offsets.append(offset)
                if offset == 512 and not failed:
                    failed = True
                    raise USBReadError("offline prewrite USB EIO", OSError(5, "EIO"))
            return actual_read(name, path, offset=offset, length=length)
        def chunked(transport, name, path, size):
            return read_full_region(transport, name, path, size, chunk_bytes=512)
        self.camera.read_region = interrupted
        with patch.object(tx, "read_full_region", side_effect=chunked), \
             patch.object(tx, "_write_response") as flash:
            with self.assertRaises(USBReadError):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            flash.assert_not_called()
            state = json.loads((self.job / "journal.json").read_text())
            self.assertEqual((state["status"], state["epoch"], state["write_may_have_occurred"]),
                             ("NO_WRITE", 0, False))
            self.assertTrue((self.job / "reconcile-001/nflasha2.live.bin.segments/0000000000000000.bin").is_file())
            def finish(_transport, _name, _payload, _count):
                self.camera.images["nflasha2"] = self.after
            flash.side_effect = finish
            self.assertEqual(tx.apply(self.job, transport_factory=lambda: self.camera)["status"],
                             "VERIFIED")
        self.assertEqual(offsets[:4], [0, 512, 512, 1024])
        self.assertFalse((self.job / "reconcile-002").exists())

    def test_restore_prewrite_uart_marker_absent_retries_only_with_zero_writes(self):
        from recovery_tool.bridge import RamPayloadNotReady
        self.prepare()
        calls = {"open": 0, "reentry": 0, "flash": 0}
        events = []
        def factory():
            calls["open"] += 1
            if calls["open"] == 1:
                raise FastbootError("RAM Fastboot USB device unavailable")
            return self.camera
        def reentry():
            calls["reentry"] += 1
            if calls["reentry"] <= 2:
                raise RamPayloadNotReady("RAM payload did not report fastboot ready")
        def flash(_transport, _name, _payload, _count):
            calls["flash"] += 1
            self.camera.images["nflasha2"] = self.after
        with patch.object(tx, "_write_response", side_effect=flash):
            result = tx.apply_with_reconnect(self.job, transport_factory=factory,
                                             reconnect=reentry, progress=events.append)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(calls["reentry"], 3)
        self.assertEqual(calls["flash"], 1)
        self.assertEqual(json.loads((self.job / "journal.json").read_text())["restore_retry_used"], 3)
        self.assertEqual([e["reason"] for e in events if e["stage"] == "restore_reentry_retry"],
                         ["uart_marker_absent", "uart_marker_absent"])

    def test_restore_uart_reentry_budget_is_durable_and_has_no_sixth_reset(self):
        from recovery_tool.bridge import RamPayloadNotReady
        self.prepare()
        resets = []
        def unavailable():
            raise FastbootError("RAM Fastboot USB device unavailable")
        def no_marker():
            resets.append(1)
            raise RamPayloadNotReady("RAM payload did not report fastboot ready")
        with patch.object(tx, "_write_response") as flash:
            with self.assertRaises(RamPayloadNotReady):
                tx.apply_with_reconnect(self.job, transport_factory=unavailable,
                                        reconnect=no_marker)
            self.assertEqual(len(resets), 5)
            self.assertEqual(json.loads((self.job / "journal.json").read_text())["restore_retry_used"], 5)
            with self.assertRaises(FastbootError):
                tx.apply_with_reconnect(self.job, transport_factory=unavailable,
                                        reconnect=no_marker)
            flash.assert_not_called()
        self.assertEqual(len(resets), 5)

    def test_restore_after_confirmed_write_never_auto_resets_even_without_inflight(self):
        self.prepare()
        state = json.loads((self.job / "journal.json").read_text())
        state.update(status="UNKNOWN_AFTER_WRITE", write_may_have_occurred=True,
                     confirmed_fragments=1)
        state.pop("inflight", None)
        tx.durable_json(self.job / "journal.json", state)
        resets = []
        with self.assertRaises(FastbootError):
            tx.apply_with_reconnect(
                self.job,
                transport_factory=lambda: (_ for _ in ()).throw(
                    FastbootError("RAM Fastboot USB device unavailable")),
                reconnect=lambda: resets.append(1))
        self.assertEqual(resets, [])

    def test_verified_preexisting_stock_trigger_reboots_once_without_flash(self):
        self.target.write_bytes(self.before)
        tx.prepare(self.job, kind="stock_sd_recovery",
                   regions=[{"name": "nflasha2", "original": self.original,
                             "target": self.target}],
                   identity=[{"region": "nflasha", "bytes": SECTOR,
                              "sha256": tx.digest(self._identity_file())}],
                   input_files=[], reboot=True,
                   metadata={"preexisting_trigger_verified": True})
        with patch.object(tx, "_write_response") as write:
            state = tx.apply(self.job, transport_factory=lambda: self.camera)
            write.assert_not_called()
        self.assertEqual(state["status"], "REBOOT_ACKNOWLEDGED")
        self.assertEqual(self.camera.reboots, 1)
        with self.assertRaisesRegex(tx.TransactionError, "reboot may already"):
            tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(self.camera.reboots, 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before = b"A"*SECTOR + b"B"*SECTOR + b"C"*SECTOR + b"D"*SECTOR
        self.after = b"A"*SECTOR + b"X"*SECTOR + b"Y"*SECTOR + b"D"*SECTOR
        self.identity = b"I"*SECTOR
        self.original = self.root / "before.bin"
        self.target = self.root / "target.bin"
        self.original.write_bytes(self.before)
        self.target.write_bytes(self.after)
        self.job = self.root / "job"
        self.camera = FakeCamera(self.before, self.identity)

    def prepare(self, reboot=False):
        tx.prepare(self.job, kind="test", regions=[{"name": "nflasha2",
                   "original": self.original, "target": self.target}],
                   identity=[{"region": "nflasha", "bytes": SECTOR,
                              "sha256": tx.digest(self._identity_file())}],
                   input_files=[], reboot=reboot)

    def _identity_file(self):
        path = self.root / "identity.bin"
        path.write_bytes(self.identity)
        return path


    def test_partial_write_lost_ack_then_sector_reconcile_and_continue(self):
        self.prepare()

        def partial(_transport, _name, _payload, _count):
            self.camera.images["nflasha2"] = (self.before[:SECTOR] + self.after[SECTOR:2*SECTOR]
                                              + self.before[2*SECTOR:])
            raise OSError("flash response lost")

        with patch.object(tx, "_write_response", side_effect=partial) as write:
            with self.assertRaisesRegex(OSError, "response lost"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            self.assertEqual(write.call_count, 1)
        state = json.loads((self.job / "journal.json").read_text())
        self.assertEqual(state["status"], "UNKNOWN_AFTER_WRITE")
        self.assertEqual(state["inflight"]["fragment"], 0)
        plan = tx.reconcile(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(plan["regions"][0]["pending_sectors"], 1)
        self.assertEqual(plan["regions"][0]["already_target_sectors"], 1)

        def finish(_transport, _name, _payload, _count):
            self.camera.images["nflasha2"] = self.after

        with patch.object(tx, "_write_response", side_effect=finish) as write:
            result = tx.apply(self.job, transport_factory=lambda: self.camera)
            self.assertEqual(write.call_count, 1)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(self.camera.images["nflasha2"], self.after)

    def test_unknown_third_value_stops_before_write(self):
        self.prepare()
        self.camera.images["nflasha2"] = self.before[:SECTOR] + b"Z"*SECTOR + self.before[2*SECTOR:]
        with patch.object(tx, "_write_response") as write:
            with self.assertRaisesRegex(tx.TransactionError, "unrecognized live sectors"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            write.assert_not_called()
        state = json.loads((self.job / "journal.json").read_text())
        self.assertEqual(state["status"], "NO_WRITE")

    def test_wrong_device_identity_stops_before_write(self):
        self.prepare()
        self.camera.images["nflasha"] = b"W" * SECTOR
        with patch.object(tx, "_write_response") as write:
            with self.assertRaisesRegex(tx.TransactionError, "identity pin differs"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
            write.assert_not_called()
        self.assertEqual(json.loads((self.job / "journal.json").read_text())["status"], "NO_WRITE")

    def test_full_readback_mismatch_is_unknown(self):
        self.prepare()
        with patch.object(tx, "_write_response", return_value=None):
            with self.assertRaisesRegex(tx.TransactionError, "full readback differs"):
                tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(json.loads((self.job / "journal.json").read_text())["status"],
                         "UNKNOWN_AFTER_WRITE")

    def test_already_target_is_skipped(self):
        self.prepare()
        self.camera.images["nflasha2"] = self.after
        with patch.object(tx, "_write_response") as write:
            result = tx.apply(self.job, transport_factory=lambda: self.camera)
            write.assert_not_called()
        self.assertEqual(result["status"], "VERIFIED")

    def test_reboot_ack_ambiguity_never_resends(self):
        self.prepare(reboot=True)
        self.camera.images["nflasha2"] = self.after
        state_path = self.job / "journal.json"
        state = json.loads(state_path.read_text())
        state["write_may_have_occurred"] = True
        tx.durable_json(state_path, state)
        self.camera.fail_reboot = True
        with self.assertRaisesRegex(OSError, "ACK vanished"):
            tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(self.camera.reboots, 1)
        state = json.loads((self.job / "journal.json").read_text())
        self.assertEqual(state["status"], "REBOOT_UNCERTAIN")
        self.assertEqual(tx.reconcile(self.job, transport_factory=lambda: self.camera)["status"],
                         "NEEDS_USER_ACTION")
        with self.assertRaisesRegex(tx.TransactionError, "reboot may already"):
            tx.apply(self.job, transport_factory=lambda: self.camera)
        self.assertEqual(self.camera.reboots, 1)

    def test_pinned_target_tamper_stops(self):
        self.prepare()
        self.target.write_bytes(b"Z"*len(self.after))
        with self.assertRaisesRegex(tx.TransactionError, "pinned partition image changed"):
            tx.reconcile(self.job, transport_factory=lambda: self.camera)


if __name__ == "__main__":
    unittest.main()
