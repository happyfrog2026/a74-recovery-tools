"""Offline updater input validation using synthetic data."""
import unittest
from pathlib import Path
from recovery_tool.stock_common import StockRecoveryError
from recovery_tool.stock_updater_source import reconstruct


class StockUpdaterSourceTest(unittest.TestCase):
    def test_unrecognized_dat_rejected(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            wrong = Path(directory) / "BODYDATA.DAT"
            wrong.write_bytes(b"not official firmware")
            with self.assertRaises(StockRecoveryError) as caught:
                reconstruct(b"\0" * 25_100_288, wrong)
        self.assertEqual(caught.exception.code, "DAT_PROFILE")
