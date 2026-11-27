"""Small offline fixtures for raw snapshot mapping and safety boundaries."""

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import snapshot_compare as cmp
from recovery_tool import stock_sd
from recovery_tool.test_stock_sd import settings_fixture


def snapshot(folder: Path, *, user_change=False, boot_change=False, nor_change=False):
    header = bytearray(512)
    header[:8] = b"82462.00"
    struct.pack_into("<I", header, 8, 2)
    struct.pack_into("<4I", header, 32, 1, 4, 5, 1)
    struct.pack_into("<4I", header, 48, 5, 3, 5, 1)
    user = header + bytearray(15 * 512)
    boot = bytearray(2 * 512)
    nor = bytearray(4 * 512)
    if user_change:
        user[2 * 512 + 9] = 3
        user[6 * 512 + 9] = 4
    if boot_change:
        boot[10] = 5
    if nor_change:
        nor[7] = 6
    data = {"nflashaB0": boot, "nflashaB1": bytes(boot) if not boot_change else bytes(2 * 512),
            "nflasha": user, "cp-nor": nor}
    manifest = {}
    for region, content in data.items():
        content = bytes(content)
        (folder / f"{region}.raw.bin").write_bytes(content)
        manifest[region] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (folder / "verified.json").write_text(json.dumps(manifest))


def decrypted(folder: Path):
    directory = folder / "decrypted"
    directory.mkdir()
    raw = (folder / "nflasha.raw.bin").read_bytes()
    entries = []
    for number, start, sectors in ((1, 1, 4), (2, 5, 3)):
        name = f"nflasha{number:02d}.decrypted.bin"
        content = raw[start * 512:(start + sectors) * 512]
        (directory / name).write_bytes(content)
        entries.append({"file": name, "start_sector": start, "sectors": sectors,
                        "partition_type": 5, "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest()})
    path = directory / "manifest.json"
    path.write_text(json.dumps({"partitions": entries}))
    return path


class SnapshotCompareTests(unittest.TestCase):
    def test_maps_changed_sectors_and_never_authorizes_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            old, new = Path(temp) / "old", Path(temp) / "new"
            old.mkdir(); new.mkdir()
            snapshot(old)
            snapshot(new, user_change=True, boot_change=True, nor_change=True)
            result = cmp.compare(old, new)
            self.assertEqual(result["raw"]["nflasha"]["changed_sectors"], 2)
            mapped = result["raw"]["nflasha"]["sdm2_map"]["partitions"]
            self.assertEqual(mapped["nflasha1"]["ranges"], [[1, 2]])
            self.assertEqual(mapped["nflasha2"]["ranges"], [[1, 2]])
            self.assertEqual(result["raw"]["nflashaB0"]["changed_sectors"], 1)
            self.assertEqual(result["raw"]["cp-nor"]["changed_sectors"], 1)
            self.assertIsNone(next(x for x in result["restore_candidates"] if x["region"] == "cp-nor")["payload_route"])
            self.assertTrue(all(not x["automatic_apply_allowed"] for x in result["restore_candidates"]))

    def test_equal_hash_skips_sector_scan_and_corruption_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            old, new = Path(temp) / "old", Path(temp) / "new"
            old.mkdir(); new.mkdir()
            snapshot(old); snapshot(new)
            with patch.object(cmp, "_changed_sectors", side_effect=AssertionError("equal files scanned")):
                self.assertTrue(all(row["status"] == "unchanged" for row in cmp.compare(old, new)["raw"].values()))
            with (new / "nflasha.raw.bin").open("r+b") as stream:
                stream.seek(512)
                stream.write(b"X")
            with self.assertRaisesRegex(cmp.SnapshotError, "SHA-256 mismatch"):
                cmp.compare(old, new)

    def test_decrypted_manifest_scopes_restore_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            old, new = Path(temp) / "old", Path(temp) / "new"
            old.mkdir(); new.mkdir()
            snapshot(old); snapshot(new, user_change=True)
            a, b = decrypted(old), decrypted(new)
            result = cmp.compare(old, new, old_decrypted=a, new_decrypted=b)
            self.assertEqual(result["decrypted"]["nflasha1"]["changed_sectors"], 1)
            self.assertEqual(result["decrypted"]["nflasha2"]["changed_sectors"], 1)
            self.assertEqual([x["payload_route"] for x in result["restore_candidates"] if x["region"].startswith("nflasha")],
                             ["flash:nflasha", "flash:nflasha1", "flash:nflasha2"])

    def test_fat_file_inventory_names_content_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            before = Path(temp) / "before.bin"
            after = Path(temp) / "after.bin"
            original = settings_fixture(b"\x04\x06")
            with patch.object(stock_sd, "SETTINGS_SIZE", len(original)):
                candidate, _ = stock_sd.make_trigger(original)
            before.write_bytes(original)
            after.write_bytes(candidate)
            result = cmp._fat_changes(before, after)
            self.assertEqual(result["status"], "compared")
            self.assertEqual(result["changed_paths"], ["UPDATER/DAT4", "UPDATER/MODE", "UPDATER/MODE5"])

    def test_size_change_is_reported_without_restore_route(self):
        with tempfile.TemporaryDirectory() as temp:
            old, new = Path(temp) / "old", Path(temp) / "new"
            old.mkdir(); new.mkdir()
            snapshot(old); snapshot(new)
            target = new / "cp-nor.raw.bin"
            target.write_bytes(target.read_bytes() + bytes(512))
            manifest = json.loads((new / "verified.json").read_text())
            manifest["cp-nor"] = {"bytes": target.stat().st_size,
                                  "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
            (new / "verified.json").write_text(json.dumps(manifest))
            result = cmp.compare(old, new)
            self.assertEqual(result["raw"]["cp-nor"]["status"], "size_changed")
            self.assertIsNone(next(x for x in result["restore_candidates"] if x["region"] == "cp-nor")["payload_route"])

    def test_partition_counts_survive_range_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            old, new = Path(temp) / "old", Path(temp) / "new"
            old.mkdir(); new.mkdir()
            snapshot(old); snapshot(new, user_change=True)
            with patch.object(cmp, "MAX_RANGES", 1):
                result = cmp.compare(old, new)
            user = result["raw"]["nflasha"]
            self.assertTrue(user["ranges_truncated"])
            self.assertEqual(user["sdm2_map"]["partition_changed_sectors"],
                             {"nflasha1": 1, "nflasha2": 1})


if __name__ == "__main__":
    unittest.main()
