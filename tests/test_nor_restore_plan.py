"""Purpose checks for complete erase-unit planning, without firmware execution."""

import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from recovery_tool import nor_restore_plan as nor


class NorRestorePlanTests(unittest.TestCase):
    def test_boundary_changes_expand_once_and_replay_exact_saved_neighbors(self):
        current = bytearray(b"B" * 4 * 4096)
        target = bytearray(current)
        for position in (4095, 4096, 4097, len(target) - 1):
            target[position] = ord("A")
        plan = nor._plan_streams(io.BytesIO(current), io.BytesIO(target),
                                 capacity=len(current), erase_bytes=4096)
        self.assertEqual([u["index"] for u in plan["erase_units"]], [0, 1, 3])
        self.assertEqual(plan["changed_bytes"], 4)
        self.assertEqual(plan["planned_erase_bytes"], 3 * 4096)
        self.assertEqual(plan["unchanged_neighbor_bytes"], 3 * 4096 - 4)
        replay = bytearray(current)
        for unit in plan["erase_units"]:
            start, size = unit["byte_offset"], unit["bytes"]
            source = target[start:start + size]
            self.assertEqual(hashlib.sha256(source).hexdigest(), unit["target_sha256"])
            replay[start:start + size] = source
        self.assertEqual(replay, target)
        self.assertEqual(plan["planned_replay_sha256"], hashlib.sha256(target).hexdigest())

    def test_exact_erase_boundary_does_not_erase_an_extra_unit(self):
        current = b"B" * (3 * 65536)
        target = b"A" * 65536 + current[65536:]
        plan = nor._plan_streams(io.BytesIO(current), io.BytesIO(target),
                                 capacity=len(current), erase_bytes=65536)
        self.assertEqual([u["index"] for u in plan["erase_units"]], [0])
        self.assertEqual(plan["planned_erase_bytes"], 65536)
        self.assertEqual(plan["changed_bytes"], 65536)

    def test_full_saved_target_includes_training_bytes(self):
        current = b"B" * 8192
        target = bytearray(current)
        target[400:404] = b"OLD!"
        plan = nor._plan_streams(io.BytesIO(current), io.BytesIO(target),
                                 capacity=len(current), erase_bytes=4096)
        self.assertEqual(plan["erase_units"][0]["target_sha256"],
                         hashlib.sha256(target[:4096]).hexdigest())
        self.assertEqual(plan["changed_bytes"], 4)

    def test_short_extra_and_partial_unit_images_fail(self):
        cases = [(b"a" * 4095, b"b" * 4096, 4096, 4096),
                 (b"a" * 4097, b"b" * 4096, 4096, 4096),
                 (b"a" * 4096, b"b" * 4096, 4096, 3000)]
        for before, after, capacity, erase in cases:
            with self.subTest(capacity=capacity, erase=erase), self.assertRaises(nor.NorPlanError):
                nor._plan_streams(io.BytesIO(before), io.BytesIO(after),
                                  capacity=capacity, erase_bytes=erase)

    def test_exact_full_image_still_cannot_execute_or_claim_live_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, target = Path(tmp) / "current.raw.bin", Path(tmp) / "target.raw.bin"
            for path in (current, target):
                with path.open("wb") as output:
                    output.truncate(nor.NOR_BYTES)
            with target.open("r+b") as output:
                output.seek(nor.NOR_BYTES - 1)
                output.write(b"A")
            result = nor.build_plan(current, target, erase_bytes=65536)
            self.assertEqual(result["erase_units"][0]["byte_offset"], nor.NOR_BYTES - 65536)
            self.assertEqual(result["planned_program_bytes"], 65536)
            self.assertEqual(result["changed_bytes"], 1)
            self.assertFalse(result["can_execute"])
            self.assertFalse(result["candidate_geometry"]["qualified"])
            self.assertIsNone(result["representation_transform_applied"])
            self.assertTrue(result["replay_equals_complete_target"])
            self.assertEqual(result["scope"], "complete_saved_raw_nor_image")
            self.assertGreater(len(result["qualification_required"]), 0)
            with target.open("r+b") as output:
                output.truncate(nor.NOR_BYTES - 1)
            with self.assertRaisesRegex(nor.NorPlanError, "exactly 64 MiB"):
                nor.build_plan(current, target, erase_bytes=65536)

    def test_unchanged_image_has_no_erase_units(self):
        plan = nor._plan_streams(io.BytesIO(b"A" * 8192), io.BytesIO(b"A" * 8192),
                                 capacity=8192, erase_bytes=4096)
        self.assertEqual(plan["erase_units"], [])
        self.assertEqual(plan["planned_program_bytes"], 0)
        self.assertTrue(plan["replay_equals_complete_target"])

    def test_representation_is_explicit_and_swap16_is_reversible(self):
        sample = b"EXBL\x00\x01\xaa\x55"
        self.assertEqual(nor.swap16(sample), b"XELB\x01\x00\x55\xaa")
        self.assertEqual(nor.swap16(nor.swap16(sample)), sample)
        with self.assertRaisesRegex(nor.NorPlanError, "even-byte"):
            nor.swap16(b"odd")


if __name__ == "__main__":
    unittest.main()
