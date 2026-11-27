"""Offline-only scoped restore planning and fail-closed boundaries."""

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from recovery_tool import scoped_restore_plan as plan
from recovery_tool import snapshot_compare as comparison
from test_snapshot_compare import snapshot, decrypted


def fixture(root: Path, *, change=False, nor_change=False, kind=5):
    root.mkdir()
    snapshot(root, user_change=change, nor_change=nor_change)
    path = decrypted(root)
    rows = json.loads(path.read_text())
    rows["status"] = "VERIFIED"
    if kind != 5:
        raw_path = root / "nflasha.raw.bin"
        data = bytearray(raw_path.read_bytes())
        struct.pack_into("<I", data, 32 + 8, kind)
        raw_path.write_bytes(data)
        verified = json.loads((root / "verified.json").read_text())
        verified["nflasha"]["sha256"] = hashlib.sha256(data).hexdigest()
        (root / "verified.json").write_text(json.dumps(verified))
        rows["partitions"][0]["partition_type"] = kind
    verified = json.loads((root / "verified.json").read_text())
    rows["source_snapshot"] = verified
    path.write_text(json.dumps(rows))
    reads = {}
    for region, spec in verified.items():
        first, second = f"{region}.raw.bin", f"{region}.repeat.bin"
        (root / second).write_bytes((root / first).read_bytes())
        reads[region] = {"first": {"file": first, **spec},
                         "second": {"file": second, **spec}}
    (root / "capture-receipt.json").write_text(json.dumps({"status": "VERIFIED", "reads": reads}))


def p0_fixture(root: Path, *, changed_slots=()):
    """Tiny verified capture with the real slot numbers and AP/CP types."""
    root.mkdir()
    header = bytearray(512)
    header[:8] = b"82462.00"
    struct.pack_into("<I", header, 8, 15)
    struct.pack_into("<4I", header, 32 + 7 * 16, 1, 4, 5, 1)
    struct.pack_into("<4I", header, 32 + 14 * 16, 5, 4, 4, 1)
    user = header + bytearray(8 * 512)
    for slot, sector in ((8, 2), (15, 6)):
        if slot in changed_slots:
            user[sector * 512 + 9] = slot
    values = {"nflashaB0": bytes(2 * 512), "nflashaB1": bytes(2 * 512),
              "nflasha": bytes(user), "cp-nor": bytes(4 * 512)}
    verified, reads = {}, {}
    for region, content in values.items():
        digest = hashlib.sha256(content).hexdigest()
        verified[region] = {"bytes": len(content), "sha256": digest}
        for suffix in ("raw", "repeat"):
            (root / f"{region}.{suffix}.bin").write_bytes(content)
        reads[region] = {kind: {"file": f"{region}.{suffix}.bin", **verified[region]}
                         for kind, suffix in (("first", "raw"), ("second", "repeat"))}
    (root / "verified.json").write_text(json.dumps(verified))
    (root / "capture-receipt.json").write_text(json.dumps({"status": "VERIFIED", "reads": reads}))
    dec = root / "decrypted"
    dec.mkdir()
    rows = []
    for slot, start, kind in ((8, 1, 5), (15, 5, 4)):
        content = bytes(user[start * 512:(start + 4) * 512])
        name = f"nflasha{slot:02d}.decrypted.bin"
        (dec / name).write_bytes(content)
        rows.append({"file": name, "start_sector": start, "sectors": 4,
                     "partition_type": kind, "bytes": len(content),
                     "sha256": hashlib.sha256(content).hexdigest()})
    (dec / "manifest.json").write_text(json.dumps({"status": "VERIFIED",
                                                   "source_snapshot": verified, "partitions": rows}))


def same_body_fixture(root: Path, *, changed_slots=(), changed_types=None,
                      nor_change=False, boot_change=False, raw_gap_change=False,
                      gap_layout=False):
    """Tiny verified double-read capture with the four reviewed partition types."""
    root.mkdir()
    header = bytearray(512)
    header[:8] = b"82462.00"
    struct.pack_into("<I", header, 8, 15)
    gap_layout = gap_layout or raw_gap_change
    layout = (((2, 128, 5), (5, 132, 6), (8, 136, 5), (15, 140, 4))
              if gap_layout else
              ((2, 1, 5), (5, 5, 6), (8, 9, 5), (15, 13, 4)))
    user = header + bytearray((143 if gap_layout else 16) * 512)
    if raw_gap_change:
        user[9 * 512 + 7] = 1
    for slot, start, kind in layout:
        kind = (changed_types or {}).get(slot, kind)
        struct.pack_into("<4I", user, 32 + (slot - 1) * 16, start, 4, kind, 1)
        if slot in changed_slots:
            user[start * 512 + 9] = slot
    boot = bytearray(2 * 512)
    nor = bytearray(4 * 512)
    if boot_change:
        boot[17] = 1
    if nor_change:
        nor[19] = 1
    values = {"nflashaB0": bytes(boot), "nflashaB1": bytes(2 * 512),
              "nflasha": bytes(user), "cp-nor": bytes(nor)}
    verified, reads = {}, {}
    for region, content in values.items():
        digest = hashlib.sha256(content).hexdigest()
        verified[region] = {"bytes": len(content), "sha256": digest}
        for suffix in ("raw", "repeat"):
            (root / f"{region}.{suffix}.bin").write_bytes(content)
        reads[region] = {kind: {"file": f"{region}.{suffix}.bin", **verified[region]}
                        for kind, suffix in (("first", "raw"), ("second", "repeat"))}
    (root / "verified.json").write_text(json.dumps(verified))
    (root / "capture-receipt.json").write_text(json.dumps({"status": "VERIFIED", "reads": reads}))
    dec = root / "decrypted"
    dec.mkdir()
    rows = []
    for slot, start, expected_kind in layout:
        kind = (changed_types or {}).get(slot, expected_kind)
        content = bytes(user[start * 512:(start + 4) * 512])
        name = f"nflasha{slot:02d}.decrypted.bin"
        (dec / name).write_bytes(content)
        rows.append({"file": name, "start_sector": start, "sectors": 4,
                     "partition_type": kind, "bytes": len(content),
                     "sha256": hashlib.sha256(content).hexdigest()})
    (dec / "manifest.json").write_text(json.dumps({"status": "VERIFIED",
                                                   "source_snapshot": verified, "partitions": rows}))


class ScopedPlanTests(unittest.TestCase):
    def test_raw_and_decrypted_changed_positions_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, target = Path(tmp) / "current", Path(tmp) / "target"
            p0_fixture(current, changed_slots=(8,))
            p0_fixture(target)
            decrypted_file = current / "decrypted/nflasha08.decrypted.bin"
            data = bytearray(decrypted_file.read_bytes())
            data[512 + 9] = 0
            data[1024 + 9] = 8
            decrypted_file.write_bytes(data)
            manifest_path = current / "decrypted/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["partitions"][0]["sha256"] = hashlib.sha256(data).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(plan.ScopedPlanError, "raw/decrypted changed-sector positions differ"):
                plan.build_plan(current, target, profile="capability")

    def test_capability_streams_more_than_4096_mapped_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, target = Path(tmp) / "current", Path(tmp) / "target"
            for folder, changed in ((current, True), (target, False)):
                folder.mkdir()
                user = bytearray(8196 * 512)
                user[:8] = b"82462.00"
                struct.pack_into("<I", user, 8, 15)
                struct.pack_into("<4I", user, 32 + 16, 1, 8195, 5, 1)
                plain = bytearray(8195 * 512)
                if changed:
                    for sector in range(1, 8195, 2):
                        plain[sector * 512] = 1
                        user[(sector + 1) * 512] = 1
                values = {"nflashaB0": bytes(512), "nflashaB1": bytes(512),
                          "nflasha": bytes(user), "cp-nor": bytes(512)}
                verified, reads = {}, {}
                for name, content in values.items():
                    spec = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                    verified[name] = spec
                    for suffix in ("raw", "repeat"):
                        (folder / f"{name}.{suffix}.bin").write_bytes(content)
                    reads[name] = {kind: {"file": f"{name}.{suffix}.bin", **spec}
                                   for kind, suffix in (("first", "raw"), ("second", "repeat"))}
                (folder / "verified.json").write_text(json.dumps(verified))
                (folder / "capture-receipt.json").write_text(json.dumps({"status": "VERIFIED", "reads": reads}))
                dec = folder / "decrypted"
                dec.mkdir()
                (dec / "nflasha02.decrypted.bin").write_bytes(plain)
                (dec / "manifest.json").write_text(json.dumps({
                    "status": "VERIFIED", "source_snapshot": verified,
                    "partitions": [{"file": "nflasha02.decrypted.bin", "start_sector": 1,
                                    "sectors": 8195, "partition_type": 5, "bytes": len(plain),
                                    "sha256": hashlib.sha256(plain).hexdigest()}]}))
            result = plan.build_plan(current, target, profile="capability")
            report = comparison.compare(current, target,
                                        old_decrypted=current / "decrypted/manifest.json",
                                        new_decrypted=target / "decrypted/manifest.json")
            self.assertEqual(report["raw"]["nflasha"]["range_count"], 4097)
            self.assertTrue(report["raw"]["nflasha"]["ranges_truncated"])
            self.assertEqual(result["partitions"][0]["region"], "nflasha2")
            self.assertEqual(result["partitions"][0]["changed_sectors"], 4097)
            self.assertEqual(len(result["partitions"][0]["fragments"]), 4097)

            # A later unmapped sector must remain visible after the display cap.
            for folder, extra in ((current, b"\x01" + bytes(511)), (target, bytes(512))):
                data = (folder / "nflasha.raw.bin").read_bytes() + extra
                for suffix in ("raw", "repeat"):
                    (folder / f"nflasha.{suffix}.bin").write_bytes(data)
                verified = json.loads((folder / "verified.json").read_text())
                verified["nflasha"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                (folder / "verified.json").write_text(json.dumps(verified))
                receipt = json.loads((folder / "capture-receipt.json").read_text())
                for kind, suffix in (("first", "raw"), ("second", "repeat")):
                    receipt["reads"]["nflasha"][kind] = {
                        "file": f"nflasha.{suffix}.bin", **verified["nflasha"]}
                (folder / "capture-receipt.json").write_text(json.dumps(receipt))
                manifest = json.loads((folder / "decrypted/manifest.json").read_text())
                manifest["source_snapshot"] = verified
                (folder / "decrypted/manifest.json").write_text(json.dumps(manifest))
            result = plan.build_plan(current, target, profile="capability")
            self.assertEqual(result["skipped_regions"][0]["name"], "nflasha")
            self.assertEqual(result["skipped_regions"][0]["unmapped_changed_sectors"], 1)
            self.assertEqual(len(result["partitions"][0]["fragments"]), 4097)


    def test_capability_plan_all_slots_boot_last_and_changed_nor_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, target = Path(tmp) / "current", Path(tmp) / "target"
            same_body_fixture(current, changed_slots=(2, 5, 8, 15),
                              nor_change=True, boot_change=True)
            same_body_fixture(target)
            result = plan.build_plan(current, target, profile="capability")
            self.assertEqual([p["region"] for p in result["partitions"]],
                             ["nflasha2", "nflasha5", "nflasha8", "nflasha15", "nflashaB0"])
            self.assertEqual(result["skipped_regions"][0]["name"], "cp-nor")
            self.assertFalse(result["fully_matches_snapshot_after_write"])
            self.assertFalse(result["write_authorized"])

    def test_capability_plan_nor_only_has_zero_writes_and_explicit_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, target = Path(tmp) / "current", Path(tmp) / "target"
            same_body_fixture(current, nor_change=True)
            same_body_fixture(target)
            result = plan.build_plan(current, target, profile="capability")
            self.assertEqual(result["partitions"], [])
            self.assertEqual(result["skipped_regions"][0]["reason"], "read_only_no_write_route")
            self.assertFalse(result["fully_matches_snapshot_after_write"])


    def test_changed_partition_target_pin_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target = root / "post", root / "baseline"
            fixture(current, change=True)
            fixture(target)
            result = plan.build_plan(current, target)
            self.assertEqual(result["status"], "OFFLINE_PLAN_ONLY")
            self.assertFalse(result["write_authorized"])
            self.assertEqual([p["region"] for p in result["partitions"]], ["nflasha1", "nflasha2"])
            self.assertEqual([p["changed_sectors"] for p in result["partitions"]], [1, 1])
            self.assertTrue(all(p["replay_equals_target"] for p in result["partitions"]))
            self.assertTrue(all(len(p["fragments"]) == 1 for p in result["partitions"]))

    def test_unchanged_capture_plans_zero_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target = root / "post", root / "baseline"
            fixture(current); fixture(target)
            self.assertEqual(plan.build_plan(current, target)["partitions"], [])


    def test_fragment_limit_and_strict_bounds(self):
        data = b"X" * (plan.MAX_RAW_SECTORS * 512)
        encoded = plan.sparse_fragment(plan.MAX_RAW_SECTORS + 2, 1, data)
        self.assertLessEqual(len(encoded), plan.MAX_DOWNLOAD)
        with self.assertRaisesRegex(plan.ScopedPlanError, "invalid sparse fragment"):
            plan.sparse_fragment(plan.MAX_RAW_SECTORS + 2, 1, data + b"Y" * 512)
        with self.assertRaisesRegex(plan.ScopedPlanError, "exceeds partition"):
            plan.sparse_fragment(plan.MAX_RAW_SECTORS, 1, data)

    def test_raw_decrypted_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target = root / "post", root / "baseline"
            fixture(current, change=True); fixture(target)
            path = current / "decrypted/nflasha01.decrypted.bin"
            data = bytearray(path.read_bytes()); data[512 + 9] = 0; path.write_bytes(data)
            manifest_path = current / "decrypted/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["partitions"][0]["sha256"] = hashlib.sha256(data).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(plan.ScopedPlanError, "changed-sector positions differ"):
                plan.build_plan(current, target)

    def test_missing_repeat_blocks_even_when_receipt_says_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target = root / "post", root / "baseline"
            fixture(current, change=True); fixture(target)
            (target / "nflasha.repeat.bin").unlink()
            with self.assertRaisesRegex(plan.ScopedPlanError, "double-read file mismatch"):
                plan.build_plan(current, target)


if __name__ == "__main__":
    unittest.main()
