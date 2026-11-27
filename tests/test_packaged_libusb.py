"""Offline check that a frozen app selects its bundled libusb backend."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import usb.backend.libusb1
import usb.core

from recovery_tool.transport import UsbBackend, FastbootError


class BundledLibusbAcceptance(unittest.TestCase):
    def test_frozen_backend_path_is_passed_to_pyusb(self):
        with tempfile.TemporaryDirectory() as directory:
            dylib = Path(directory) / "libusb-1.0.0.dylib"
            dylib.write_bytes(b"test-only")
            sentinel = object()
            with patch("recovery_tool.transport.sys.frozen", True, create=True), \
                 patch("recovery_tool.transport.sys._MEIPASS", directory, create=True), \
                 patch.object(usb.backend.libusb1, "get_backend", side_effect=lambda find_library: (
                     sentinel if find_library("usb-1.0") == str(dylib) else None)) as backend, \
                 patch.object(usb.core, "find", return_value=[]) as find:
                with self.assertRaisesRegex(FastbootError, "unavailable"):
                    UsbBackend()
                self.assertEqual(backend.call_count, 1)
                self.assertIs(find.call_args.kwargs["backend"], sentinel)


if __name__ == "__main__":
    unittest.main()
