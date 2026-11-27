"""Offline acceptance for the public, read-only snapshot entry point."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import snapshot
from recovery_tool import card_connection
from recovery_tool.write_transaction import durable_json
from recovery_tool.transport import (USBReadError, USBCloseError, DestinationIOError,
                                     FastbootError, FastbootEnumerationUnavailable, PRODUCT)


class FakeTransport:
    calls = []
    events = []
    disagree = False
    fail_once = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def region_size(self, region):
        return snapshot.REGIONS[region]

    def read_region(self, region, output, *, offset=0, length=None):
        self.calls.append((region, Path(output).name))
        self.events.append((region, Path(output).parent.name, offset))
        if self.fail_once and region == "nflasha" and offset == 512 and \
                "nflasha.first" in str(Path(output).parent):
            type(self).fail_once = False
            Path(str(output) + ".partial").write_bytes(b"interrupted")
            raise OSError("USB disconnected")
        data = region.encode().ljust(snapshot.REGIONS[region], b"\0")
        if self.disagree and region == "nflasha" and "nflasha.second" in str(Path(output).parent):
            data = b"X" + data[1:]
        data = data[offset:offset + length] if length is not None else data[offset:]
        Path(output).write_bytes(data)
        return len(data)


class SnapshotAcceptance(unittest.TestCase):
    def test_restore_capture_idle_hook_runs_only_after_usb_read_closes(self):
        state = {"active": False, "hooks": 0}
        class Tracked(FakeTransport):
            def __enter__(self):
                self.asserted = True
                state["active"] = True
                return self
            def __exit__(self, *_):
                state["active"] = False
            def verify_product(self): return PRODUCT
        def idle_hook(_folder, _receipt, _receipt_path, _retry_state):
            self.assertFalse(state["active"])
            state["hooks"] += 1
        with patch.object(snapshot, "RESTORE_CURRENT_REGIONS",
                          {name: value for name, value in self.regions.items() if name != "cp-nor"}):
            receipt = snapshot.capture(self.folder / "restore", transport_factory=Tracked,
                                       profile="restore_current", idle_hook=idle_hook)
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertGreater(state["hooks"], 0)

    def _retry_transport(self, *, failures=1, identity_changed=False, destination_error=False,
                         usb_error=None):
        class RetryTransport(FakeTransport):
            remaining = failures
            def verify_product(self): return PRODUCT
            def read_region(self, region, output, *, offset=0, length=None):
                path = Path(output)
                if identity_changed and "identity-check-" in str(path) and region == "nflashaB0":
                    path.write_bytes(b"X" * self.region_size(region))
                    return self.region_size(region)
                if region == "nflasha" and offset == 512 and "nflasha.second" in str(path.parent):
                    if destination_error:
                        raise DestinationIOError(5, "simulated destination write EIO")
                    if type(self).remaining > 0:
                        type(self).remaining -= 1
                        Path(str(path) + ".partial").write_bytes(b"interrupted")
                        raise USBReadError("simulated camera USB EIO", usb_error or OSError(5, "I/O error"),
                                           stage="usb_bulk_read")
                return super().read_region(region, output, offset=offset, length=length)
        return RetryTransport

    def test_transient_usb_failure_auto_resumes_with_identity_and_checkpoint(self):
        transport = self._retry_transport()
        events = []
        receipt = snapshot.capture(self.folder, transport_factory=transport,
                                   auto_retry_usb=True, progress=events.append, sleep=lambda _: None)
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertNotIn("error", receipt)
        self.assertEqual(len([x for x in events if x["stage"] == "backup_retry"]), 1)
        self.assertEqual(sum(region == "nflasha" and parent == "nflasha.first" and offset == 0
                             for region, parent, offset in FakeTransport.events), 1)
        self.assertEqual(len(receipt["interrupted_files"]), 1)

    def test_usb_backend_error_without_errno_retries_with_remaining_budget(self):
        class USBError(OSError):
            def __init__(self):
                super().__init__(None, "Other error")
                self.backend_error_code = -99
        transport = self._retry_transport(usb_error=USBError())
        receipt = snapshot.capture(self.folder, transport_factory=transport,
                                   auto_retry_usb=True, sleep=lambda _: None)
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertEqual(len(receipt["retry_events"]), 1)
        self.assertEqual(receipt["retry_events"][0]["max_retries"], 5)

    def test_windows_bulk_pipe_reopens_or_reenters_based_on_pnp(self):
        class USBError(OSError):
            backend_error_code = -9

        for state in ("present", "departed", "departed_attach_only", "unknown", "wrong_camera", "drift_after_reentry"):
            with self.subTest(state=state):
                folder = self.folder / state
                transport = self._retry_transport(
                    usb_error=USBError(32, "Pipe error"),
                    identity_changed=state in ("wrong_camera", "drift_after_reentry"))
                events = []
                reentries = []
                def reenter():
                    self.assertIn(state, ("departed", "drift_after_reentry"))
                    reentries.append(True)
                    return transport()
                pnp = {"wrong_camera": "present", "departed_attach_only": "departed",
                       "drift_after_reentry": "departed"}.get(state, state)
                options = dict(transport_factory=transport, auto_retry_usb=True,
                               reentry_factory=None if state == "departed_attach_only" else reenter,
                               backup_fastboot_state=lambda: pnp,
                               progress=events.append, sleep=lambda _: None)
                if state != "departed_attach_only":
                    receipt = snapshot.capture(folder, **options)
                    self.assertEqual(receipt["status"], "VERIFIED")
                    self.assertEqual(len(receipt["retry_events"]), 1)
                    self.assertEqual(receipt["retry_events"][0]["reentry_requested"], state in ("departed", "drift_after_reentry"))
                    self.assertEqual(len(reentries), int(state in ("departed", "drift_after_reentry")))
                    self.assertEqual(len(receipt["interrupted_files"]), 1)
                    self.assertIn("backup_fastboot_direct", [event["stage"] for event in events])
                    self.assertNotIn("backup_anchor_verified", [event["stage"] for event in events])
                else:
                    with self.assertRaises(snapshot.CaptureFailure):
                        snapshot.capture(folder, **options)
                    receipt = json.loads((folder / "capture-receipt.json").read_text())
                    self.assertEqual(receipt["failure"]["usb_backend_error_code"], -9)
                    self.assertEqual(receipt["failure"]["fastboot_pnp_state"], pnp)
                    self.assertFalse(reentries)
                    self.assertFalse(receipt.get("retry_events"))

    def test_windows_bulk_pipe_direct_reopen_is_bounded(self):
        class USBError(OSError):
            backend_error_code = -9

        transport = self._retry_transport(failures=snapshot.MAX_USB_RETRIES + 1,
                                          usb_error=USBError(32, "Pipe error"))
        with self.assertRaises(snapshot.CaptureFailure):
            snapshot.capture(self.folder, transport_factory=transport, auto_retry_usb=True,
                             reentry_factory=lambda: self.fail("Windows retry attempted bridge reentry"),
                             backup_fastboot_state=lambda: "present", sleep=lambda _: None)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(len(receipt["retry_events"]), snapshot.WINDOWS_BACKUP_USB_RETRIES)
        self.assertFalse(any(row["reentry_requested"] for row in receipt["retry_events"]))

    def test_windows_restore_capture_retries_pipe_from_the_read_boundary(self):
        class USBError(OSError):
            backend_error_code = -9

        transport = self._retry_transport(usb_error=USBError(32, "Pipe error"))
        with patch.object(snapshot, "RESTORE_CURRENT_REGIONS", self.regions):
            receipt = snapshot.capture(self.folder, transport_factory=transport,
                                       profile="restore_current", auto_retry_usb=True,
                                       reentry_factory=transport,
                                       backup_fastboot_state=lambda: "departed",
                                       sleep=lambda _: None)
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertTrue(receipt["retry_events"][0]["reentry_requested"])

    def test_windows_next_region_reopens_or_reenters_with_checkpoint(self):
        for case in ("verified", "departed", "unknown", "no_checkpoint", "wrong_camera"):
            with self.subTest(case=case):
                folder = self.folder / case
                manifest = folder / "verified.json"
                probes = []
                live_anchors = []

                class BoundaryTransport(FakeTransport):
                    def verify_product(self):
                        return PRODUCT

                    def read_region(self, region, output, *, offset=0, length=None):
                        if "identity-check-" in Path(output).parent.name:
                            live_anchors.append(region)
                            if case == "wrong_camera":
                                Path(output).write_bytes(b"x" * length)
                                return length
                        return super().read_region(region, output, offset=offset, length=length)

                first_loss = True

                def direct():
                    nonlocal first_loss
                    previous_verified = manifest.exists() and "nflasha" in json.loads(manifest.read_text())
                    if first_loss and (case == "no_checkpoint" or previous_verified):
                        first_loss = False
                        raise FastbootEnumerationUnavailable("RAM Fastboot USB device unavailable")
                    return BoundaryTransport()

                def probe():
                    probes.append(True)
                    return case if case in ("unknown", "departed") else "present"

                def reenter():
                    self.assertEqual(case, "departed")
                    return BoundaryTransport()
                opts = dict(transport_factory=direct, auto_retry_usb=True,
                            reentry_factory=reenter,
                            backup_fastboot_state=probe,
                            sleep=lambda _: None)
                receipt = snapshot.capture(folder, **opts)
                self.assertEqual(receipt["status"], "VERIFIED")
                self.assertEqual(receipt["retry_consecutive_used"], 0)
                self.assertEqual(receipt["region_coverage"]["cp-nor"]["status"], "VERIFIED")
                self.assertEqual(len(receipt["retry_events"]), 1)
                event = receipt["retry_events"][0]
                self.assertEqual((event["region"], event["pass"]),
                                 ("nflashaB0" if case == "no_checkpoint" else "cp-nor", "region-size"))
                self.assertEqual(event["reentry_requested"], case == "departed")
                self.assertFalse(live_anchors)
                self.assertEqual(probes, [True])

    def test_windows_backup_retries_failed_entry_then_resets_after_progress(self):
        from recovery_tool.bridge import RamPayloadNotReady

        state = {"departed": False, "entries": 0}
        interrupted = set()

        class Reader(FakeTransport):
            def verify_product(self):
                return PRODUCT

            def read_region(self, region, output, *, offset=0, length=None):
                path = Path(output)
                if "identity-check-" in str(path):
                    raise AssertionError("previously verified segments must not be read again")
                if path.parent.name in {"nflasha.second", "cp-nor.second"} and region not in interrupted:
                    interrupted.add(region)
                    state["departed"] = True
                    raise USBReadError("simulated disconnect", OSError(5, "EIO"), stage="usb_bulk_read")
                return super().read_region(region, output, offset=offset, length=length)

        def reenter():
            state["entries"] += 1
            if state["entries"] == 1:
                raise RamPayloadNotReady("UART ready marker absent")
            state["departed"] = False
            return Reader()

        receipt = snapshot.capture(
            self.folder, transport_factory=Reader, reentry_factory=reenter,
            backup_fastboot_state=lambda: "departed" if state["departed"] else "present",
            auto_retry_usb=True, sleep=lambda _: None)
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertEqual([event["retry"] for event in receipt["retry_events"]], [1, 2, 1])
        self.assertEqual([event["reentry_requested"] for event in receipt["retry_events"]],
                         [True, True, True])
        self.assertEqual(receipt["retry_consecutive_used"], 0)

    def test_isolated_backup_recapture_preserves_original_and_verifies_fresh_data(self):
        from recovery_tool.task_backend import Backend

        prior = self.folder / "original"
        class Reader(FakeTransport):
            def verify_product(self):
                return PRODUCT

        receipt = snapshot.capture(prior, transport_factory=Reader)
        manifest_path = prior / "verified.json"
        receipt_path = prior / "capture-receipt.json"
        old_anchor = next(item for item in snapshot.resume_anchors(prior, receipt)
                          if item["region"] == "nflasha")
        receipt["status"] = "INCOMPLETE"
        receipt["failure"] = {"anchor_mismatch": {
            "region": "nflasha", "offset": old_anchor["offset"], "bytes": old_anchor["bytes"],
            "saved_sha256": old_anchor["sha256"], "live_sha256": "f" * 64,
        }}
        snapshot.save(receipt_path, receipt)
        original = {path: path.read_bytes() for path in prior.rglob("*") if path.is_file()}
        output = self.folder / "unconfirmed"
        blocked = Backend(transport_factory=lambda: self.fail("opened without confirmation"))
        result = blocked.backup_recapture(prior, output, same_camera_confirmed=False)
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertFalse(output.exists())
        with self.assertRaisesRegex(RuntimeError, "new directory"):
            snapshot.capture(prior, transport_factory=lambda: self.fail("resumed stale snapshot"))

        for case in ("region-good", "region-disagrees", "region-disconnected"):
            with self.subTest(case=case):
                measured = []
                class RegionReader(Reader):
                    def region_size(self, region):
                        self.assert_region(region)
                        return super().region_size(region)

                    def assert_region(self, region):
                        if region != "nflasha":
                            raise AssertionError(f"unexpected region access: {region}")

                    def read_region(self, region, output, *, offset=0, length=None):
                        self.assert_region(region)
                        is_repeat = "nflasha.second" in Path(output).parent.name
                        measured.append((offset, is_repeat))
                        if case == "region-disconnected" and offset == 512:
                            raise FastbootEnumerationUnavailable("lost active Fastboot")
                        count = super().read_region(region, output, offset=offset, length=length)
                        if case == "region-disagrees" and is_repeat:
                            Path(output).write_bytes(b"D" * count)
                        return count

                output = self.folder / case
                with patch.object(Backend, "_transport", side_effect=AssertionError("bridge prohibited")):
                    result = Backend(transport_factory=RegionReader).backup_recapture(
                        prior, output, same_camera_confirmed=True, region="nflasha")
                self.assertFalse((output / "verified.json").exists())
                self.assertFalse((output / "decrypted").exists())
                from recovery_tool.task_backend import _restore_snapshot_folder
                with self.assertRaisesRegex(ValueError, "verified snapshot folder"):
                    _restore_snapshot_folder(output)
                provenance = json.loads((output / "recapture.json").read_text())
                self.assertEqual(provenance["missing_regions"], ["cp-nor", "nflashaB0", "nflashaB1"])
                self.assertFalse(provenance["complete_backup"])
                if case == "region-good":
                    self.assertEqual(result["status"], "PARTIAL")
                    self.assertEqual(measured, [(0, False), (0, True), (512, False), (512, True)])
                    manifest = json.loads((output / "verified-region.json").read_text())
                    self.assertEqual(set(manifest), {"nflasha"})
                    self.assertEqual(snapshot.sha256(output / "nflasha.raw.bin"), manifest["nflasha"]["sha256"])
                    self.assertEqual(snapshot.sha256(output / "nflasha.repeat.bin"), manifest["nflasha"]["sha256"])
                    self.assertEqual(set(provenance["verified_regions"]["nflasha"]), {"first", "second"})
                    self.assertIn("capture_started_utc", provenance)
                else:
                    self.assertEqual(result["status"], "INCOMPLETE")
                    self.assertFalse((output / "verified-region.json").exists())
                for path, contents in original.items():
                    self.assertEqual(path.read_bytes(), contents)

        for case in ("verified", "discordant", "disconnect", "interrupted"):
            with self.subTest(case=case):
                class FreshReader(Reader):
                    def read_region(self, region, output, *, offset=0, length=None):
                        if region == "nflasha" and case == "disconnect":
                            raise FastbootEnumerationUnavailable("lost active Fastboot")
                        if region == "nflasha" and case == "interrupted":
                            raise KeyboardInterrupt
                        count = super().read_region(region, output, offset=offset, length=length)
                        if region == "nflasha":
                            data = Path(output).read_bytes()
                            is_repeat = "nflasha.second" in Path(output).parent.name
                            replacement = b"D" if case == "discordant" and is_repeat else b"F"
                            Path(output).write_bytes(replacement + data[1:])
                        return count

                output = self.folder / case
                backend = Backend(transport_factory=FreshReader)
                with patch.object(Backend, "_transport", side_effect=AssertionError("bridge prohibited")):
                    if case == "interrupted":
                        with self.assertRaises(KeyboardInterrupt):
                            backend.backup_recapture(prior, output, same_camera_confirmed=True)
                    else:
                        result = backend.backup_recapture(prior, output, same_camera_confirmed=True)
                        self.assertEqual(result["status"], "VERIFIED" if case == "verified" else "INCOMPLETE")
                provenance = json.loads((output / "recapture.json").read_text())
                captured = json.loads((output / "capture-receipt.json").read_text())
                self.assertEqual(provenance["reused_regions"], [])
                self.assertFalse(provenance["atomic_camera_snapshot"])
                self.assertEqual(captured["status"], "VERIFIED" if case == "verified" else "INCOMPLETE")
                if case == "verified":
                    self.assertEqual(provenance["status"], "VERIFIED")
                    self.assertEqual(result["raw_regions"], 4)
                    self.assertNotEqual((output / "nflasha.raw.bin").read_bytes(),
                                        (prior / "nflasha.raw.bin").read_bytes())
                else:
                    self.assertNotEqual(provenance["status"], "VERIFIED")
                    self.assertTrue((output / "nflashaB0.raw.bin").is_file())
                for path, contents in original.items():
                    self.assertEqual(path.read_bytes(), contents)

    def test_nontransient_usb_access_error_stops_without_retry(self):
        transport = self._retry_transport(usb_error=OSError(13, "Access denied"))
        with self.assertRaises(snapshot.CaptureFailure) as caught:
            snapshot.capture(self.folder, transport_factory=transport,
                             auto_retry_usb=True, sleep=lambda _: None)
        self.assertEqual(caught.exception.details["origin"], "camera_usb_read")
        self.assertFalse(json.loads((self.folder / "capture-receipt.json").read_text())
                         .get("retry_events"))

    def test_complete_segment_with_failed_usb_dispose_does_not_open_next_read(self):
        opens = []
        class FailedDispose(FakeTransport):
            def __enter__(self):
                self.read_called = False
                opens.append(1)
                return self
            def read_region(self, *args, **kwargs):
                self.read_called = True
                return super().read_region(*args, **kwargs)
            def __exit__(self, *_):
                if self.read_called:
                    error = USBCloseError("camera USB cleanup failed", OSError(5, "EIO"))
                    error.usb_cleanup_dispose_failed = True
                    raise error
        with self.assertRaises(snapshot.CaptureFailure):
            snapshot.capture(self.folder, transport_factory=FailedDispose,
                             auto_retry_usb=True, sleep=lambda _: None)
        self.assertEqual(len(opens), 2)  # extent discovery, then first segment only
        self.assertFalse(json.loads((self.folder / "capture-receipt.json").read_text())
                         .get("retry_events"))

    def test_identity_mismatch_stops_automatic_retry(self):
        transport = self._retry_transport(identity_changed=True)
        with self.assertRaisesRegex(snapshot.CaptureFailure, "backup checkpoint content changed"):
            snapshot.capture(self.folder, transport_factory=transport,
                             auto_retry_usb=True, sleep=lambda _: None)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(receipt["failure"]["origin"], "identity_or_protocol")
        self.assertEqual(receipt["status"], "INCOMPLETE")

    def test_destination_error_never_retries_camera(self):
        transport = self._retry_transport(destination_error=True)
        events = []
        with self.assertRaisesRegex(snapshot.CaptureFailure, "destination write"):
            snapshot.capture(self.folder, transport_factory=transport,
                             auto_retry_usb=True, progress=events.append, sleep=lambda _: None)
        self.assertFalse(events)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(receipt["failure"]["origin"], "destination_io")

    def test_transient_usb_retry_is_bounded(self):
        transport = self._retry_transport(failures=7)
        events = []
        with self.assertRaisesRegex(snapshot.CaptureFailure, "camera USB EIO"):
            snapshot.capture(self.folder, transport_factory=transport,
                             auto_retry_usb=True, progress=events.append, sleep=lambda _: None)
        self.assertEqual(len(events), 5)
        self.assertEqual(json.loads((self.folder / "capture-receipt.json").read_text())
                         ["retry_consecutive_used"], 5)
        self.assertEqual(json.loads((self.folder / "capture-receipt.json").read_text())
                         ["failure"]["origin"], "camera_usb_read")
        # The five-event budget is durable across an explicit user resume.
        with self.assertRaises(snapshot.CaptureFailure):
            snapshot.capture(self.folder, transport_factory=transport,
                             auto_retry_usb=True, progress=events.append, sleep=lambda _: None)
        self.assertEqual(len(events), 5)

    def test_new_matching_segment_pair_resets_consecutive_but_not_total(self):
        class ProgressTransport(FakeTransport):
            first_failure = True
            later_failures = 6
            def verify_product(self): return PRODUCT
            def read_region(self, region, output, *, offset=0, length=None):
                path = Path(output)
                if region == "nflasha" and offset == 512 and "nflasha.second" in str(path.parent) and \
                        type(self).first_failure:
                    type(self).first_failure = False
                    raise USBReadError("USB interrupted before first new pair", OSError(5, "EIO"))
                if region == "cp-nor" and "cp-nor.second" in str(path.parent) and \
                        type(self).later_failures:
                    type(self).later_failures -= 1
                    raise USBReadError("USB interrupted without new pair", OSError(5, "EIO"))
                return super().read_region(region, output, offset=offset, length=length)
        events = []
        with self.assertRaises(snapshot.CaptureFailure):
            snapshot.capture(self.folder, transport_factory=ProgressTransport,
                             auto_retry_usb=True, progress=events.append, sleep=lambda _: None)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(len(receipt["retry_events"]), 6)
        self.assertEqual(receipt["retry_consecutive_used"], 5)
        self.assertEqual(receipt["retry_reset_events"][-1]["region"], "nflasha")
        self.assertEqual(receipt["retry_reset_events"][-1]["offset"], 512)
        self.assertEqual([x["total_retries"] for x in events if x["stage"] == "backup_retry"],
                         [1, 2, 3, 4, 5, 6])
        self.assertEqual(len([x for x in events if x["stage"] == "backup_retry_reset"]), 1)
        # Re-reading old saved pairs on a separate invocation must not reset 5/5.
        ProgressTransport.later_failures = 1
        with self.assertRaises(snapshot.CaptureFailure):
            snapshot.capture(self.folder, transport_factory=ProgressTransport,
                             auto_retry_usb=True, sleep=lambda _: None)
        resumed = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(resumed["retry_consecutive_used"], 5)
        self.assertEqual(len(resumed["retry_events"]), 6)
        self.assertEqual(len(resumed["retry_reset_events"]), 1)

    def test_mismatched_new_pair_never_resets_retry_counter(self):
        class MismatchTransport(FakeTransport):
            failed = False
            def verify_product(self): return PRODUCT
            def read_region(self, region, output, *, offset=0, length=None):
                path = Path(output)
                if region == "nflasha" and offset == 512 and "nflasha.second" in str(path.parent):
                    if not type(self).failed:
                        type(self).failed = True
                        raise USBReadError("USB interrupted", OSError(5, "EIO"))
                    path.write_bytes(b"X" * length)
                    return length
                return super().read_region(region, output, offset=offset, length=length)
        with self.assertRaisesRegex(RuntimeError, "two independent segment reads differ"):
            snapshot.capture(self.folder, transport_factory=MismatchTransport,
                             auto_retry_usb=True, sleep=lambda _: None)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(receipt["retry_consecutive_used"], 1)
        self.assertFalse(receipt.get("retry_reset_events"))

    def test_missing_endpoint_reprobes_before_using_guarded_reentry(self):
        class USBError(OSError):
            backend_error_code = -5
        error = USBError(2, "Entity not found")
        transport = self._retry_transport(usb_error=error)
        self.assertFalse(snapshot.is_transient_camera_usb(
            USBReadError("descriptor lookup failed", error, stage="usb_descriptor")))
        state = {"unavailable_sent": False, "reentries": 0}
        def direct():
            if transport.remaining == 0 and not state["unavailable_sent"]:
                state["unavailable_sent"] = True
                raise FastbootEnumerationUnavailable("RAM Fastboot USB device unavailable")
            return transport()
        def reenter():
            state["reentries"] += 1
            return transport()
        receipt = snapshot.capture(self.folder, transport_factory=direct,
                                   reentry_factory=reenter, auto_retry_usb=True,
                                   sleep=lambda _: None)
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertEqual(state["reentries"], 1)
        self.assertEqual(len(receipt["retry_events"]), 2)
        self.assertTrue(receipt["retry_events"][-1]["reentry_requested"])

    def test_reentry_guard_failure_stops_without_reset(self):
        transport = self._retry_transport()
        state = {"unavailable_sent": False, "guard_calls": 0}
        def direct():
            if transport.remaining == 0 and not state["unavailable_sent"]:
                state["unavailable_sent"] = True
                raise FastbootEnumerationUnavailable("RAM Fastboot USB device unavailable")
            return transport()
        def blocked():
            state["guard_calls"] += 1
            raise RuntimeError("unresolved stock update")
        with self.assertRaisesRegex(snapshot.CaptureFailure, "unresolved stock update"):
            snapshot.capture(self.folder, transport_factory=direct,
                             reentry_factory=blocked, auto_retry_usb=True,
                             sleep=lambda _: None)
        self.assertEqual(state["guard_calls"], 1)
        self.assertEqual(json.loads((self.folder / "capture-receipt.json").read_text())
                         ["failure"]["origin"], "identity_or_protocol")

    def test_prior_blink_failure_receipt_allows_separate_checkpoint_resume(self):
        prior = self.folder / "tasks/prior-stock"
        prior.mkdir(parents=True)
        durable_json(prior / "task.json", {"kind": "stock_sd_recovery"})
        durable_json(prior / "journal.json", {
            "status": "REBOOT_ACKNOWLEDGED", "write_may_have_occurred": True,
            "reboot_may_have_occurred": True, "last_verified_utc": "2026-11-15T01:01:05Z"})
        state = {"disconnected": False, "failed_once": False, "reentries": 0}
        class Camera(FakeTransport):
            def verify_product(self): return PRODUCT
            def read_region(self, region, output, *, offset=0, length=None):
                if region == "cp-nor" and "cp-nor.second" in str(Path(output).parent) and \
                        not state["failed_once"]:
                    state["failed_once"] = state["disconnected"] = True
                    Path(str(output) + ".partial").write_bytes(b"interrupted")
                    raise USBReadError("camera disconnected", OSError(19, "No such device"))
                return super().read_region(region, output, offset=offset, length=length)
        def direct():
            if state["disconnected"]:
                raise FastbootEnumerationUnavailable("RAM Fastboot USB device unavailable")
            return Camera()
        def guarded_reentry():
            if card_connection._blocking_tasks(prior.parent):
                raise RuntimeError("automatic RAM reentry blocked by unresolved camera operation")
            state["reentries"] += 1
            state["disconnected"] = False
            return Camera()
        with patch.object(card_connection, "TASK_REGISTRY", self.folder / "registry.json"):
            with self.assertRaisesRegex(snapshot.CaptureFailure, "blocked"):
                snapshot.capture(self.folder / "backup", transport_factory=direct,
                                 reentry_factory=guarded_reentry, auto_retry_usb=True,
                                 sleep=lambda _: None)
            receipt_path = self.folder / "backup/capture-receipt.json"
            self.assertEqual(len(json.loads(receipt_path.read_text())["retry_events"]), 2)
            self.assertEqual(state["reentries"], 0)
            card_connection.confirm_stock_update_failed(
                prior, confirmed=True, reason="blink_5_long_6_short")
            receipt = snapshot.capture(self.folder / "backup", transport_factory=direct,
                                       reentry_factory=guarded_reentry, auto_retry_usb=True,
                                       sleep=lambda _: None)
            self.assertEqual(receipt["status"], "VERIFIED")
            self.assertEqual(len(receipt["retry_events"]), 3)
            self.assertEqual(state["reentries"], 1)

    def setUp(self):
        FakeTransport.calls = []
        FakeTransport.events = []
        FakeTransport.disagree = False
        FakeTransport.fail_once = False
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.regions = {"nflashaB0": 512, "nflashaB1": 512,
                        "nflasha": 1024, "cp-nor": 512}
        self.patches = (patch.object(snapshot, "REGIONS", self.regions),
                        patch.object(snapshot, "DEFAULT_CHUNK_BYTES", 512),
                        patch.object(snapshot, "FastbootTransport", FakeTransport))
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_missing_boot_and_short_user_can_retry_with_available_double_read_anchor(self):
        calls = []
        class Partial(FakeTransport):
            failed = False
            def verify_product(self): return PRODUCT
            def region_size(self, region):
                if region.startswith("nflashaB"):
                    raise FastbootError("invalid partition size ''")
                return 1024 if region == "nflasha" else 512
            def read_region(self, region, output, *, offset=0, length=None):
                calls.append((region, length))
                if region == "cp-nor" and not type(self).failed:
                    type(self).failed = True
                    raise USBReadError("read interrupted", OSError(5, "EIO"))
                Path(output).write_bytes(bytes(length))
        with patch.object(snapshot, "DEFAULT_CHUNK_BYTES", 4096):
            result = snapshot.capture(self.folder, transport_factory=Partial,
                                      auto_retry_usb=True, sleep=lambda _: None)
            anchors = snapshot.resume_anchors(self.folder, result)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(set(result["capture_regions"]), {"nflasha", "cp-nor"})
        self.assertEqual(len(result["retry_events"]), 1)
        self.assertIn(("nflasha", 1024), calls)
        self.assertEqual({a["region"] for a in anchors}, {"nflasha", "cp-nor"})
        (self.folder / "segments/nflasha.second/0000000000.bin").write_bytes(b"x" * 1024)
        with self.assertRaisesRegex(RuntimeError, "anchor changed"):
            snapshot.resume_anchors(self.folder, result)

    def test_missing_boot_first_user_region_retries_from_segment_without_verified_manifest(self):
        class UserFirst(FakeTransport):
            failed = False
            def verify_product(self): return PRODUCT
            def region_size(self, region):
                if region.startswith("nflashaB"):
                    raise FastbootError("invalid partition size ''")
                return 1024 if region == "nflasha" else 512
            def read_region(self, region, output, *, offset=0, length=None):
                if region == "nflasha" and offset == 512 and not type(self).failed:
                    type(self).failed = True
                    self_test.assertFalse((self_test.folder / "verified.json").exists())
                    raise USBReadError("second chunk interrupted", OSError(5, "EIO"))
                Path(output).write_bytes(bytes(length))
        self_test = self
        result = snapshot.capture(self.folder, transport_factory=UserFirst,
                                  auto_retry_usb=True, sleep=lambda _: None)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(len(result["retry_events"]), 1)
        self.assertEqual(result["retry_events"][0]["offset"], 512)
        self.assertEqual(set(result["capture_regions"]), {"nflasha", "cp-nor"})

    def test_all_regions_unavailable_cannot_certify_empty_backup(self):
        class Unavailable(FakeTransport):
            def region_size(self, region):
                raise FastbootError("invalid partition size ''")
        with self.assertRaisesRegex(RuntimeError, "no readable regions"):
            snapshot.capture(self.folder, transport_factory=Unavailable)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(receipt["status"], "INCOMPLETE")
        self.assertEqual(len(receipt["region_coverage"]), 4)
        self.assertFalse((self.folder / "verified.json").exists())

    def test_no_saved_anchor_explicitly_refuses_continuity(self):
        with self.assertRaisesRegex(RuntimeError, "no saved double-read anchor"):
            snapshot.resume_anchors(self.folder, {"capture_regions": {"nflasha": 512}})

    def test_reader_sizes_replace_fixed_baseline_and_unavailable_region_is_explicit(self):
        class Dynamic(FakeTransport):
            def region_size(self, region):
                if region == "nflashaB1":
                    raise FastbootError("invalid partition size ''")
                return 1536 if region == "nflasha" else 512
            def read_region(self, region, output, *, offset=0, length=None):
                Path(output).write_bytes(bytes(length))
        result = snapshot.capture(self.folder, transport_factory=Dynamic)
        self.assertEqual(result["capture_regions"]["nflasha"], 1536)
        self.assertNotIn("nflashaB1", result["capture_regions"])
        self.assertEqual(result["region_coverage"]["nflashaB1"]["status"], "UNAVAILABLE")
        self.assertFalse(result["physical_device_complete"])
        self.assertEqual(snapshot.captured_regions(result), result["capture_regions"])
        self.assertEqual((self.folder / "nflasha.raw.bin").stat().st_size, 1536)

    def test_windows_resume_reads_only_c2_and_remaining_pairs(self):
        from recovery_tool.task_backend import Backend

        self.regions["nflasha"] = 2048
        for scenario in ("matches", "changes", "unstable"):
            with self.subTest(scenario=scenario):
                job = self.folder / scenario
                folder = job / "snapshot"
                calls = []

                class Reader(FakeTransport):
                    def verify_product(self):
                        return PRODUCT

                    def read_region(self, region, output, *, offset=0, length=None):
                        path = Path(output)
                        if "identity-check-" in path.parent.name:
                            raise AssertionError("verified pairs must not be read again")
                        calls.append((region, offset, path.parent.name.rsplit(".", 1)[-1]))
                        count = super().read_region(region, output, offset=offset, length=length)
                        if region == "nflasha" and offset == 1024 and scenario != "matches":
                            byte = b"Z" if scenario == "changes" or ".first" in path.parent.name else b"R"
                            path.write_bytes(byte * count)
                        return count

                class Interrupted(FakeTransport):
                    def read_region(self, region, output, *, offset=0, length=None):
                        if (region, offset, Path(output).parent.name) == ("nflasha", 1024, "nflasha.second"):
                            raise FastbootEnumerationUnavailable("lost Fastboot at C2")
                        return super().read_region(region, output, offset=offset, length=length)

                backend = Backend(transport_factory=Reader, backup_fastboot_state=lambda: "present")
                backend.prepare("full_backup", {}, job)
                with self.assertRaises(FastbootEnumerationUnavailable):
                    snapshot.capture(folder, transport_factory=Interrupted)
                paths = [folder / f"segments/nflasha.{attempt}/{offset:010x}.bin"
                         for offset in (0, 512) for attempt in ("first", "second")]
                old = [path.read_bytes() for path in paths]
                self.assertEqual(backend.backup_resume_status(job)["status"], "READY")
                result = backend.apply(job)
                self.assertEqual(calls[0], ("nflasha", 1024, "second"))
                self.assertEqual([path.read_bytes() for path in paths], old)
                self.assertTrue(all(region == "cp-nor" or region == "nflasha" and offset >= 1024
                                    for region, offset, _ in calls))
                receipt = json.loads((folder / "capture-receipt.json").read_text())
                disputed = receipt.get("disputed_segments", [])
                if scenario == "unstable":
                    self.assertEqual(result["status"], "INCOMPLETE")
                    self.assertEqual(receipt["retry_consecutive_used"], 3)
                    self.assertEqual(len(disputed), 3)
                    self.assertTrue(all(offset == 1024 for _, offset, _ in calls))
                else:
                    self.assertEqual(result["status"], "VERIFIED")
                    self.assertEqual(len(disputed), int(scenario == "changes"))
                    expected = [("nflasha", 1024, "second")]
                    if scenario == "changes":
                        expected.extend([("nflasha", 1024, "first"), ("nflasha", 1024, "second")])
                        self.assertEqual((folder / "nflasha.raw.bin").read_bytes()[1024:1536], b"Z" * 512)
                        self.assertEqual(receipt["retry_consecutive_used"], 0)
                    expected.extend([("nflasha", 1536, "first"), ("nflasha", 1536, "second"),
                                     ("cp-nor", 0, "first"), ("cp-nor", 0, "second")])
                    self.assertEqual(calls, expected)
                    self.assertEqual(backend.verify(job)["status"], "VERIFIED")

    def test_double_read_receipt_and_resume_without_new_capture(self):
        receipt = snapshot.capture(self.folder)
        verified = json.loads((self.folder / "verified.json").read_text())
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertEqual(set(verified), set(self.regions))
        self.assertEqual(len(FakeTransport.calls), 10)
        self.assertEqual([event[1:] for event in FakeTransport.events
                          if event[0] == "nflasha"],
                         [("nflasha.first", 0), ("nflasha.second", 0),
                          ("nflasha.first", 512), ("nflasha.second", 512)])
        for region in self.regions:
            self.assertEqual(receipt["reads"][region]["first"]["sha256"],
                             receipt["reads"][region]["second"]["sha256"])
            self.assertEqual((self.folder / f"{region}.raw.bin").stat().st_size,
                             self.regions[region])
        snapshot.capture(self.folder)
        self.assertEqual(len(FakeTransport.calls), 10)

    def test_mismatched_second_read_preserves_files_and_blocks_verification(self):
        FakeTransport.disagree = True
        with self.assertRaisesRegex(RuntimeError, "two independent segment reads differ"):
            snapshot.capture(self.folder)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(receipt["status"], "INCOMPLETE")
        self.assertNotIn("nflasha", json.loads((self.folder / "verified.json").read_text()))
        self.assertTrue((self.folder / "segments/nflasha.first/0000000000.bin").exists())
        self.assertTrue((self.folder / "segments/nflasha.second/0000000000.bin").exists())

    def test_interrupted_segment_is_preserved_and_resume_skips_complete_segment(self):
        FakeTransport.fail_once = True
        with self.assertRaisesRegex(OSError, "USB disconnected"):
            snapshot.capture(self.folder)
        self.assertEqual(json.loads((self.folder / "capture-receipt.json").read_text())["status"],
                         "INCOMPLETE")
        first_chunk = self.folder / "segments/nflasha.first/0000000000.bin"
        self.assertTrue(first_chunk.exists())
        snapshot.capture(self.folder)
        receipt = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertEqual(sum(region == "nflasha" and name == "0000000000.bin"
                             for region, name in FakeTransport.calls), 2)
        self.assertTrue(receipt["interrupted_files"])

    def test_same_task_resume_marks_capture_in_progress_and_keeps_prior_failure(self):
        FakeTransport.fail_once = True
        with self.assertRaisesRegex(OSError, "USB disconnected"):
            snapshot.capture(self.folder)
        prior = json.loads((self.folder / "capture-receipt.json").read_text())
        self.assertEqual(prior["status"], "INCOMPLETE")
        observed = []
        class ResumeTransport(FakeTransport):
            def read_region(self, region, output, *, offset=0, length=None):
                observed.append(json.loads((self_folder / "capture-receipt.json").read_text()))
                return super().read_region(region, output, offset=offset, length=length)
        self_folder = self.folder
        resumed = snapshot.capture(self.folder, transport_factory=ResumeTransport)
        self.assertTrue(observed)
        self.assertEqual(observed[0]["status"], "IN_PROGRESS")
        self.assertNotIn("error", observed[0])
        self.assertEqual(observed[0]["previous_failures"][0]["error"], prior["error"])
        self.assertEqual(resumed["status"], "VERIFIED")
        self.assertEqual(resumed["started_utc"], prior["started_utc"])
        self.assertTrue(any(self.folder.glob("segments/nflasha.first/*.interrupted-*")))


if __name__ == "__main__":
    unittest.main()
