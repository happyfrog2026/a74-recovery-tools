"""Offline checks for restore-only omission of read-only CP NOR."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import scoped_restore_plan, snapshot
from test_scoped_restore_plan import same_body_fixture


class TinyTransport:
    calls = []

    def __enter__(self): return self
    def __exit__(self, *_): return None
    def region_size(self, region): return snapshot.REGIONS[region]
    def read_region(self, region, output, *, offset=0, length=None):
        type(self).calls.append(region)
        size = snapshot.REGIONS[region]
        data = region.encode().ljust(size, b"\0")
        selected = data[offset:offset + length] if length is not None else data[offset:]
        Path(output).write_bytes(selected)
        return len(selected)


class RestoreNoNorTests(unittest.TestCase):
    def test_capture_profile_omits_nor_but_full_backup_keeps_it(self):
        sizes = {name: 512 for name in snapshot.REGIONS}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(snapshot.REGIONS, sizes, clear=True), \
             patch.dict(snapshot.RESTORE_CURRENT_REGIONS,
                        {name: 512 for name in sizes if name != "cp-nor"}, clear=True), \
             patch.object(snapshot, "DEFAULT_CHUNK_BYTES", 512), \
             patch.dict(snapshot.CHUNK_BYTES, {"cp-nor": 512}, clear=True):
            root = Path(tmp)
            TinyTransport.calls = []
            receipt = snapshot.capture(root / "current", transport_factory=TinyTransport,
                                       profile="restore_current")
            self.assertEqual(receipt["capture_profile"], snapshot.RESTORE_CURRENT_PROFILE)
            self.assertEqual(receipt["not_captured_regions"], ["cp-nor"])
            self.assertNotIn("cp-nor", TinyTransport.calls)
            self.assertNotIn("cp-nor", json.loads((root / "current/verified.json").read_text()))
            with self.assertRaisesRegex(ValueError, "profile differs"):
                snapshot.capture(root / "current", transport_factory=TinyTransport)
            TinyTransport.calls = []
            full = snapshot.capture(root / "backup", transport_factory=TinyTransport)
            self.assertEqual(full["status"], "VERIFIED")
            self.assertIn("cp-nor", TinyTransport.calls)

    def test_partial_current_plan_reports_nor_unknown_and_keeps_write_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, target = Path(tmp) / "current", Path(tmp) / "target"
            same_body_fixture(current, changed_slots=(2,))
            same_body_fixture(target)
            verified = json.loads((current / "verified.json").read_text())
            del verified["cp-nor"]
            (current / "verified.json").write_text(json.dumps(verified))
            receipt = json.loads((current / "capture-receipt.json").read_text())
            del receipt["reads"]["cp-nor"]
            receipt.update(capture_profile=snapshot.RESTORE_CURRENT_PROFILE,
                           not_captured_regions=["cp-nor"])
            (current / "capture-receipt.json").write_text(json.dumps(receipt))
            manifest = json.loads((current / "decrypted/manifest.json").read_text())
            manifest["source_snapshot"] = verified
            (current / "decrypted/manifest.json").write_text(json.dumps(manifest))
            (current / "cp-nor.raw.bin").unlink()
            (current / "cp-nor.repeat.bin").unlink()
            result = scoped_restore_plan.build_plan(current, target, profile="capability")
            self.assertEqual([p["region"] for p in result["partitions"]], ["nflasha2"])
            self.assertEqual(result["skipped_regions"], [{"name": "cp-nor",
                "reason": "not_captured_read_only", "changed": None}])
            self.assertFalse(result["fully_matches_snapshot_after_write"])
            self.assertNotIn("cp-nor", [row["region"] for row in
                              scoped_restore_plan.capability_identity(current, target, result)])
            del receipt["not_captured_regions"]
            (current / "capture-receipt.json").write_text(json.dumps(receipt))
            with self.assertRaises(scoped_restore_plan.ScopedPlanError):
                scoped_restore_plan.build_plan(current, target, profile="capability")


if __name__ == "__main__":
    unittest.main()
