"""Offline protocol replays; these tests never open USB or serial hardware."""

import tempfile
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recovery_tool.bridge import Bridge, BridgeError, boot_records, record
from recovery_tool.transport import (FastbootTransport, FastbootError,
                                     FastbootEnumerationUnavailable, USBReadError,
                                     USBCloseError, UsbBackend)


class FakeSerial:
    def __init__(self, replies):
        self.replies = bytearray(replies)
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read(self, count):
        count = min(count, 2)
        data = bytes(self.replies[:count])
        del self.replies[:count]
        return data

    def close(self):
        self.closed = True


def packet(kind, data=b""):
    return bytes((kind,)) + struct.pack("<H", len(data)) + data


class FakeUsb:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []
        self.closed = False

    def write(self, data, timeout_ms):
        self.writes.append(data)
        return len(data)

    def read(self, count, timeout_ms):
        if not self.replies:
            raise FastbootError("simulated timeout")
        data = self.replies.pop(0)
        if len(data) > count:
            raise AssertionError("test reply exceeds requested read")
        return data

    def close(self):
        self.closed = True


class BridgeTests(unittest.TestCase):
    def test_ping_and_mismatch(self):
        for version in (b"uart_boot 1", b"uart_boot 2"):
            serial = FakeSerial(packet(0x83, b"UART") + packet(0x81, version))
            with Bridge("fake", serial_factory=lambda *a, **kw: serial) as bridge:
                self.assertEqual(bridge.ping(), version)
            self.assertEqual(serial.writes, [packet(1)])
            self.assertTrue(serial.closed)
        serial = FakeSerial(packet(0x81, b"other"))
        with Bridge("fake", serial_factory=lambda *a, **kw: serial) as bridge:
            with self.assertRaisesRegex(BridgeError, "unexpected bridge firmware"):
                bridge.ping()

    def test_boot_records_match_upstream_format(self):
        payload = b"X" * 513
        records = list(boot_records(payload))
        self.assertEqual(records[0], record(0x0123, 0))
        self.assertEqual(records[1], record(1, 0xFE020000, payload[:512]))
        self.assertEqual(records[2], record(1, 0xFE020200, payload[512:]))
        self.assertEqual(len(records), 7)

    def test_boot_trace_includes_ack_stages_and_interleaved_uart(self):
        payload = b"X"
        records = list(boot_records(payload))
        replies = (packet(0x83, b"early") + packet(0x81, b"uart_boot 1") +
                   packet(0x81) * (2 + len(records)) +
                   packet(0x83, b"late") + packet(0x81))
        serial = FakeSerial(replies)
        stages, uart = [], []
        with Bridge("fake", serial_factory=lambda *a, **kw: serial) as bridge:
            bridge.boot_payload(payload, sleep=lambda _: None,
                                on_stage=lambda name, data: stages.append(name),
                                on_uart=uart.append)
        self.assertEqual(uart, [b"early", b"late"])
        self.assertEqual(stages.count("record_ack"), len(records))
        self.assertEqual(stages[:3], ["ping_ack", "reset_ack", "carrier_on_ack"])
        self.assertEqual(stages[-1], "carrier_off_ack")


class TransportTests(unittest.TestCase):
    def setUp(self):
        # These unit devices are shape fakes, not PyUSB Device objects with a
        # managed _ctx; production now disposes rejected descriptor handles.
        dispose = patch("usb.util.dispose_resources")
        self.descriptor_dispose = dispose.start()
        self.addCleanup(dispose.stop)

    def test_usb_open_and_product_handshake_keep_typed_transient_cause(self):
        import usb.core
        with patch("usb.core.find", side_effect=usb.core.USBError("Other error", -99)):
            with self.assertRaises(USBReadError) as caught:
                UsbBackend()
        self.assertEqual((caught.exception.stage, caught.exception.backend_error_code),
                         ("usb_open", -99))
        class TimeoutProduct(FakeUsb):
            def read(self, *_):
                raise usb.core.USBTimeoutError("timeout", -7)
        with self.assertRaises(USBReadError) as caught:
            FastbootTransport(TimeoutProduct([]))
        self.assertEqual((caught.exception.stage, caught.exception.backend_error_code),
                         ("usb_product", -7))

    def test_no_langid_from_transient_usb_error_keeps_serial_unverified(self):
        import usb.core
        class Device:
            iSerialNumber = 3
        with patch("usb.core.find", return_value=[Device()]), \
             patch("usb.util.get_langids", side_effect=usb.core.USBError("USB I/O", errno=5)), \
             patch("usb.util.claim_interface") as claim:
            with self.assertRaisesRegex(FastbootEnumerationUnavailable, "serial descriptor I/O"):
                UsbBackend()
            claim.assert_not_called()
            self.descriptor_dispose.assert_called_once()

    def test_failed_descriptor_disposal_stops_retry_with_unclosed_handle(self):
        import usb.core
        class Device:
            iSerialNumber = 3
        self.descriptor_dispose.side_effect = OSError(5, "dispose failed")
        with patch("usb.core.find", return_value=[Device()]), \
             patch("usb.util.get_langids", side_effect=usb.core.USBError("I/O", errno=5)):
            with self.assertRaises(USBReadError) as caught:
                UsbBackend()
        self.assertEqual(caught.exception.stage, "usb_open")
        self.assertTrue(caught.exception.usb_cleanup_dispose_failed)

    def test_no_langid_from_permission_error_does_not_reset(self):
        import usb.core
        class Device:
            iSerialNumber = 3
        with patch("usb.core.find", return_value=[Device()]), \
             patch("usb.util.get_langids", side_effect=usb.core.USBError("access denied", errno=13)):
            with self.assertRaisesRegex(FastbootError, "serial descriptor access failed") as caught:
                UsbBackend()
            self.assertNotIsInstance(caught.exception, FastbootEnumerationUnavailable)

    def test_empty_langid_without_usb_error_does_not_reset(self):
        class Device:
            iSerialNumber = 3
        with patch("usb.core.find", return_value=[Device()]), \
             patch("usb.util.get_langids", return_value=()):
            with self.assertRaisesRegex(FastbootError, "serial LANGID absent") as caught:
                UsbBackend()
            self.assertNotIsInstance(caught.exception, FastbootEnumerationUnavailable)

    def test_multiple_candidates_require_all_serial_descriptors_resolved(self):
        import usb.core
        from recovery_tool.transport import SERIAL
        class Device:
            iSerialNumber = 3
            def __init__(self, label): self.label = label
            def get_active_configuration(self): return SimpleNamespace(bConfigurationValue=1)
        good, other = Device("good"), Device("other")
        for error in (usb.core.USBError("I/O", errno=5),
                      usb.core.USBError("access", errno=13)):
            def language(device):
                if device is other: raise error
                return (0x409,)
            with self.subTest(error=error.errno), \
                 patch("usb.core.find", return_value=[good, other]), \
                 patch("usb.util.get_langids", side_effect=language), \
                 patch("usb.util.get_string", return_value=SERIAL), \
                 patch("usb.util.claim_interface") as claim:
                with self.assertRaisesRegex(FastbootError, "ambiguous Fastboot USB serial") as caught:
                    UsbBackend()
                self.assertNotIsInstance(caught.exception, FastbootEnumerationUnavailable)
                claim.assert_not_called()
        with patch("usb.core.find", return_value=[good, other]), \
             patch("usb.util.get_langids", return_value=(0x409,)), \
             patch("usb.util.get_string", return_value=SERIAL), \
             patch("usb.util.claim_interface") as claim:
            with self.assertRaisesRegex(FastbootError, "found 2"):
                UsbBackend()
            claim.assert_not_called()
        with patch("usb.core.find", return_value=[good, other]), \
             patch("usb.util.get_langids", return_value=(0x409,)), \
             patch("usb.util.get_string", side_effect=lambda device, *_: SERIAL if device is good else "other"), \
             patch("usb.util.claim_interface") as claim:
            selected = UsbBackend()
            self.assertIs(selected.device, good)
            claim.assert_called_once_with(good, 0)

    def test_r5_shared_and_native_payload_products_are_scoped_to_selection(self):
        for product in (b"ILCE-7M4", b"ILCE-7RM5"):
            with self.subTest(product=product):
                backend = FakeUsb([b"OKAY" + product])
                with FastbootTransport(backend, camera_model="ILCE-7RM5") as transport:
                    self.assertEqual(transport.expected_products, ("ILCE-7M4", "ILCE-7RM5"))
                    self.assertTrue(transport._verified)
        backend = FakeUsb([b"OKAYILCE-7RM5"])
        with self.assertRaisesRegex(FastbootError, "selected ILCE-7M4"):
            FastbootTransport(backend)
        self.assertTrue(backend.closed)

    def test_r5_native_usb_serial_is_accepted_by_selected_transport(self):
        device = SimpleNamespace(iSerialNumber=3,
                                 get_active_configuration=lambda: SimpleNamespace(bConfigurationValue=1))
        with patch("usb.core.find", return_value=[device]), \
             patch("usb.util.get_langids", return_value=(0x409,)), \
             patch("usb.util.get_string", return_value="ILCE7RM5-fastboot"), \
             patch("usb.util.claim_interface") as claim:
            selected = UsbBackend(serial=("ILCE7M4-fastboot", "ILCE7RM5-fastboot"))
        self.assertIs(selected.device, device)
        claim.assert_called_once_with(device, 0)

    def test_r5_normal_usb_discovery_does_not_use_m4_pid(self):
        from recovery_tool.transport import observe_normal_camera_usb
        class Device(list):
            idProduct = 0x1234
        device = Device([[SimpleNamespace(bInterfaceClass=8)]])
        with patch("usb.core.find", return_value=[device]) as find:
            found = observe_normal_camera_usb(camera_model="ILCE-7RM5")
        self.assertEqual(found["product"], "ILCE-7RM5")
        self.assertEqual(found["pid"], 0x1234)
        self.assertEqual(found["status"], "MSC")
        self.assertNotIn("idProduct", find.call_args.kwargs)

    def test_release_failure_still_disposes_usb_resources(self):
        calls = []
        class Util:
            def release_interface(self, *_):
                calls.append("release")
                raise OSError(19, "device gone")
            def dispose_resources(self, *_):
                calls.append("dispose")
        backend = object.__new__(UsbBackend)
        backend.util = Util()
        backend.device = object()
        with patch("recovery_tool.transport._record_usb_cleanup_failure") as record:
            with self.assertRaisesRegex(OSError, "device gone"):
                backend.close()
            self.assertIsNotNone(record.call_args.args[0])
            self.assertIsNone(record.call_args.args[1])
        self.assertEqual(calls, ["release", "dispose"])

    def test_dispose_failure_marks_primary_usb_error_as_unsafe_to_reenter(self):
        calls = []
        class Util:
            def release_interface(self, *_):
                calls.append("release")
            def dispose_resources(self, *_):
                calls.append("dispose")
                raise OSError(5, "dispose failed")
        backend = object.__new__(UsbBackend)
        backend.util = Util()
        backend.device = object()
        primary = USBReadError("bulk read failed", OSError(5, "EIO"))
        with patch("recovery_tool.transport._record_usb_cleanup_failure"):
            with self.assertRaises(USBReadError) as caught:
                with FastbootTransport(backend, require_product=False):
                    raise primary
        self.assertIs(caught.exception, primary)
        self.assertTrue(primary.usb_cleanup_dispose_failed)
        self.assertEqual(calls, ["release", "dispose"])

    def test_release_failure_with_successful_dispose_keeps_primary_retryable(self):
        class Util:
            def release_interface(self, *_):
                raise OSError(19, "release after disconnect")
            def dispose_resources(self, *_):
                pass
        backend = object.__new__(UsbBackend)
        backend.util = Util()
        backend.device = object()
        primary = USBReadError("bulk read failed", OSError(5, "EIO"))
        with patch("recovery_tool.transport._record_usb_cleanup_failure"):
            with self.assertRaises(USBReadError):
                with FastbootTransport(backend, require_product=False):
                    raise primary
        self.assertFalse(primary.usb_cleanup_dispose_failed)

    def test_constructor_cleanup_does_not_mask_product_failure(self):
        class FailingClose(FakeUsb):
            def close(self): raise OSError(19, "device disappeared during release")
        with self.assertRaisesRegex(FastbootError, "unexpected Fastboot product"):
            FastbootTransport(FailingClose([b"OKAYwrong"]))

    def test_cleanup_disconnect_preserves_primary_bulk_error(self):
        class FailingUsb(FakeUsb):
            def read(self, count, timeout_ms):
                if self.replies and self.replies[0] == b"FAIL_READ":
                    self.replies.pop(0)
                    raise OSError(5, "bulk read I/O error")
                return super().read(count, timeout_ms)
            def close(self):
                raise OSError(19, "device disappeared during release")
        usb = FailingUsb([b"OKAYILCE-7M4", b"OKAY0x200", b"OKAY",
                          b"DATA00000200", b"abc", b"FAIL_READ"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sector.bin"
            with self.assertRaisesRegex(USBReadError, "bulk read") as caught:
                with FastbootTransport(usb) as transport:
                    transport.read_region("nflasha", output, length=512)
            self.assertEqual(caught.exception.errno, 5)
            self.assertEqual((Path(folder) / "sector.bin.partial").read_bytes(), b"abc")

    def test_cleanup_failure_after_complete_read_is_transport_error(self):
        class FailingClose(FakeUsb):
            def close(self): raise OSError(19, "device disappeared during release")
        data = bytes(range(256)) * 2
        usb = FailingClose([b"OKAYILCE-7M4", b"OKAY0x200", b"OKAY",
                            b"DATA00000200", data, b"OKAY"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sector.bin"
            with self.assertRaises(USBCloseError) as caught:
                with FastbootTransport(usb) as transport:
                    transport.read_region("nflasha", output, length=512)
            self.assertEqual(caught.exception.errno, 19)
            self.assertEqual(output.read_bytes(), data)

    def test_upload_errno_is_identified_as_camera_usb_read(self):
        class FailingUsb(FakeUsb):
            def read(self, count, timeout_ms):
                if self.replies and self.replies[0] == b"FAIL_USB":
                    self.replies.pop(0)
                    raise OSError(19, "No such device")
                return super().read(count, timeout_ms)
        usb = FailingUsb([b"OKAYILCE-7M4", b"OKAY0x200", b"OKAY",
                          b"DATA00000200", b"abc", b"FAIL_USB"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sector.bin"
            with FastbootTransport(usb) as transport:
                with self.assertRaisesRegex(USBReadError, "camera USB bulk read") as caught:
                    transport.read_region("nflasha", output, length=512)
            self.assertEqual(caught.exception.errno, 19)
            self.assertEqual((Path(folder) / "sector.bin.partial").read_bytes(), b"abc")

    def test_libusb_other_error_keeps_backend_identity_and_bulk_stage(self):
        class USBError(OSError):
            def __init__(self):
                super().__init__(None, "Other error")
                self.backend_error_code = -99
        class FailingUsb(FakeUsb):
            def read(self, count, timeout_ms):
                if self.replies and self.replies[0] == b"FAIL_USB":
                    self.replies.pop(0)
                    raise USBError()
                return super().read(count, timeout_ms)
        usb = FailingUsb([b"OKAYILCE-7M4", b"OKAY0x200", b"OKAY",
                          b"DATA00000200", b"abc", b"FAIL_USB"])
        with tempfile.TemporaryDirectory() as folder:
            with FastbootTransport(usb) as transport:
                with self.assertRaises(USBReadError) as caught:
                    transport.read_region("nflasha", Path(folder) / "sector.bin", length=512)
        self.assertIsNone(caught.exception.errno)
        self.assertEqual(caught.exception.backend_error_code, -99)
        self.assertEqual(caught.exception.backend_error_type, "USBError")
        self.assertEqual(caught.exception.stage, "usb_bulk_read")

    def test_sector_read(self):
        data = bytes(range(256)) * 2
        usb = FakeUsb([b"OKAYILCE-7M4", b"OKAY0x200", b"INFOhint", b"OKAY",
                       b"DATA00000200", data[:200], data[200:], b"OKAY"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sector.bin"
            with FastbootTransport(usb) as transport:
                self.assertEqual(transport.read_region("nflasha", output, length=512), 512)
            self.assertEqual(output.read_bytes(), data)
            self.assertEqual(usb.writes, [b"getvar:product", b"getvar:partition-size:nflasha",
                                          b"oem partition dump nflasha 0 1", b"upload"])

    def test_failure_preserves_partial_and_blocks_write_commands(self):
        usb = FakeUsb([b"OKAYILCE-7M4", b"OKAY0x200", b"OKAY", b"DATA00000200", b"abc"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sector.bin"
            with FastbootTransport(usb) as transport:
                with self.assertRaises(ValueError):
                    transport.command("flash:nflasha")
                with self.assertRaisesRegex(FastbootError, "simulated timeout"):
                    transport.read_region("nflasha", output, length=512)
            self.assertFalse(output.exists())
            self.assertEqual((Path(folder) / "sector.bin.partial").read_bytes(), b"abc")

    def test_wrong_product_and_upload_length(self):
        usb = FakeUsb([b"OKAYwrong"])
        with self.assertRaisesRegex(FastbootError, "unexpected Fastboot product"):
            FastbootTransport(usb)
        self.assertTrue(usb.closed)
        usb = FakeUsb([b"OKAYILCE-7M4", b"OKAY0x200", b"OKAY", b"DATA00000400"])
        with tempfile.TemporaryDirectory() as folder:
            with FastbootTransport(usb) as transport:
                with self.assertRaisesRegex(FastbootError, "DATA length"):
                    transport.read_region("nflasha", Path(folder) / "out", length=512)


if __name__ == "__main__":
    unittest.main()
