"""Offline acceptance for the only RP2040 write path."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import board_install as board


class BoardInstallTests(unittest.TestCase):
    def test_pinned_uf2_is_valid(self):
        self.assertEqual(board.validate_uf2(board.default_uf2())["blocks"], 299)

    def test_existing_bridge_skips_write(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(board, "_ports", return_value=["rp-port"]), \
                 patch.object(board, "_ping", return_value=b"uart_boot 1"), \
                 patch.object(board, "bootsel_volumes", side_effect=AssertionError("BOOTSEL must not be read")):
                result = board.install(evidence_dir=Path(folder))
            self.assertEqual(result["status"], "already_installed")
            self.assertTrue(Path(result["receipt"]).is_file())

    def test_existing_v2_bridge_skips_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(board, "_ports", return_value=["rp-port"]), \
                 patch.object(board, "_ping", return_value=b"uart_boot 2"), \
                 patch.object(board, "bootsel_volumes", side_effect=AssertionError("BOOTSEL must not be read")):
                result = board.install(evidence_dir=root)
            self.assertEqual(result["status"], "already_installed")
            self.assertFalse((root / "A7M4.UF2").exists())

    def test_bootsel_identity_required(self):
        with tempfile.TemporaryDirectory() as folder:
            volume = Path(folder)
            (volume / "INFO_UF2.TXT").write_text("Board-ID: other\nRP2040\n")
            with self.assertRaises(board.BoardInstallError):
                board.confirm_bootsel(volume)

    def test_write_needs_post_write_ping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            volume = root / "RPI-RP2"
            volume.mkdir()
            (volume / "INFO_UF2.TXT").write_text("UF2 Bootloader v3.0\nModel: Raspberry Pi RP2\nBoard-ID: RPI-RP2\n")
            with patch.object(board, "_ports", side_effect=[[], ["rp-port"]]), \
                 patch.object(board, "_ping", return_value=b"uart_boot 1"):
                result = board.install(volume=volume, evidence_dir=root / "evidence", wait_seconds=1)
            self.assertEqual(result["status"], "installed_and_verified")
            self.assertEqual((volume / "A7M4.UF2").read_bytes(), board.default_uf2().read_bytes())
            self.assertEqual(json.loads(Path(result["receipt"]).read_text())["status"], "installed_and_verified")

    def test_failed_post_write_ping_records_unverified_outcome(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            volume = root / "RPI-RP2"
            volume.mkdir()
            (volume / "INFO_UF2.TXT").write_text("UF2 Bootloader v3.0\nModel: Raspberry Pi RP2\nBoard-ID: RPI-RP2\n")
            with patch.object(board, "_ports", side_effect=lambda: [] if not (volume / "A7M4.UF2").exists() else ["rp-port"]), \
                 patch.object(board, "_ping", side_effect=RuntimeError("no response")), \
                 patch.object(board.time, "sleep", return_value=None):
                with self.assertRaisesRegex(board.BoardInstallError, "outcome unverified"):
                    board.install(volume=volume, evidence_dir=root / "evidence", wait_seconds=0.001)
            self.assertEqual((volume / "A7M4.UF2").read_bytes(), board.default_uf2().read_bytes())
            receipts = list((root / "evidence").glob("board-install-*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["status"], "failed")

    def test_no_board_fails_without_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(board, "_ports", return_value=[]), \
                 patch.object(board, "bootsel_volumes", return_value=[]):
                with self.assertRaisesRegex(board.BoardInstallError, "found 0"):
                    board.install(evidence_dir=root)
            receipts = list(root.glob("board-install-*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["status"], "failed")

    def test_copy_failure_remains_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            volume = root / "RPI-RP2"
            volume.mkdir()
            (volume / "INFO_UF2.TXT").write_text("Model: Raspberry Pi RP2\nBoard-ID: RPI-RP2\n")
            original_open = Path.open

            def failing_open(path, *args, **kwargs):
                if path.name == "A7M4.UF2":
                    raise OSError("synthetic copy failure")
                return original_open(path, *args, **kwargs)

            with patch.object(board, "_ports", return_value=[]), \
                 patch.object(Path, "open", failing_open), \
                 patch.object(board.time, "sleep", return_value=None):
                with self.assertRaisesRegex(board.BoardInstallError, "outcome unverified"):
                    board.install(volume=volume, evidence_dir=root / "evidence", wait_seconds=0.001)
            self.assertFalse((volume / "A7M4.UF2").exists())
            receipt = next((root / "evidence").glob("board-install-*.json"))
            self.assertEqual(json.loads(receipt.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
