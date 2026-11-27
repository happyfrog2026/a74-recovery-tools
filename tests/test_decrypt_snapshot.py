"""Offline SDM2 geometry checks for the local decryption entry point."""

from pathlib import Path
import hashlib
import json
import errno
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import decrypt_snapshot
from recovery_tool.decrypt_snapshot import VALID_SLOTS, decrypt, table, verify_snapshot
from recovery_tool.snapshot import RESTORE_CURRENT_PROFILE


class DecryptSnapshotTableAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = Path(self.temp.name) / "nflasha.raw.bin"

    def write_table(self, *, gap=False):
        header = bytearray(512)
        header[:8] = b"82462.00"
        struct.pack_into("<I", header, 8, max(VALID_SLOTS))
        cursor = 128
        for slot in VALID_SLOTS:
            if gap and slot == 8:
                cursor += 1
            struct.pack_into("<4I", header, 32 + (slot - 1) * 16,
                             cursor, 1, 5, 1)
            cursor += 1
        self.raw.write_bytes(header + bytes(cursor * 512 - len(header)))

    def verified_raw(self):
        self.write_table()
        folder = self.raw.parent
        verified, reads = {}, {}
        for region in ("nflashaB0", "nflashaB1", "nflasha", "cp-nor"):
            payload = self.raw.read_bytes() if region == "nflasha" else bytes(512)
            digest = hashlib.sha256(payload).hexdigest()
            verified[region] = {"bytes": len(payload), "sha256": digest}
            reads[region] = {}
            for attempt, suffix in (("first", "raw"), ("second", "repeat")):
                name = f"{region}.{suffix}.bin"
                (folder / name).write_bytes(payload)
                reads[region][attempt] = {"file": name, "bytes": len(payload),
                                          "sha256": digest}
        (folder / "verified.json").write_text(json.dumps(verified))
        (folder / "capture-receipt.json").write_text(
            json.dumps({"status": "VERIFIED", "reads": reads}))
        return folder

    def test_explicit_coverage_size_mismatch_and_empty_evidence_are_rejected(self):
        folder = self.verified_raw()
        with patch.object(decrypt_snapshot, "derive_key", return_value=bytes(range(64))):
            decrypt(folder)
        receipt_path = folder / "capture-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        verified = json.loads((folder / "verified.json").read_text())
        receipt["capture_regions"] = {name: spec["bytes"] for name, spec in verified.items()}
        receipt["capture_regions"]["nflasha"] += 512
        receipt_path.write_text(json.dumps(receipt))
        for operation in (decrypt, verify_snapshot):
            with self.assertRaisesRegex(RuntimeError, "sizes differ"):
                operation(folder)
        (folder / "verified.json").write_text("{}")
        receipt.update(capture_regions={}, reads={})
        receipt_path.write_text(json.dumps(receipt))
        for operation in (decrypt, verify_snapshot):
            with self.assertRaisesRegex(RuntimeError, "no captured raw evidence"):
                operation(folder)

    def test_orphan_final_after_rename_is_preserved_and_rebuilt(self):
        folder = self.verified_raw()
        original_save = decrypt_snapshot.save
        def interrupted_save(path, data):
            if path.name == "manifest.json":
                raise RuntimeError("crash after slot rename")
            return original_save(path, data)
        with patch.object(decrypt_snapshot, "derive_key", return_value=bytes(range(64))), \
             patch.object(decrypt_snapshot, "save", side_effect=interrupted_save):
            with self.assertRaisesRegex(RuntimeError, "crash after slot rename"):
                decrypt(folder)
        outdir = folder / "decrypted"
        first = outdir / "nflasha01.decrypted.bin"
        orphan_digest = hashlib.sha256(first.read_bytes()).hexdigest()
        self.assertFalse((outdir / "manifest.json").exists())
        with patch.object(decrypt_snapshot, "derive_key", return_value=bytes(range(64))):
            result = decrypt(folder)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(len(result["interrupted_files"]), 1)
        archived = outdir / result["interrupted_files"][0]["file"]
        self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(), orphan_digest)
        self.assertEqual(verify_snapshot(folder)["decrypted_slots"], 15)

    def test_enospc_partial_and_manifest_tmp_are_preserved_on_resume(self):
        folder = self.verified_raw()
        original_decrypt = decrypt_snapshot.decrypt_one
        def interrupted_decrypt(raw, out, entry, key):
            out.with_name(out.name + ".partial").write_bytes(b"incomplete")
            raise OSError(errno.ENOSPC, "disk full")
        with patch.object(decrypt_snapshot, "derive_key", return_value=bytes(range(64))), \
             patch.object(decrypt_snapshot, "decrypt_one", side_effect=interrupted_decrypt):
            with self.assertRaises(OSError) as caught:
                decrypt(folder)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        outdir = folder / "decrypted"
        (outdir / "manifest.tmp").write_bytes(b"unfinished manifest")
        with patch.object(decrypt_snapshot, "derive_key", return_value=bytes(range(64))):
            result = decrypt(folder)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(len(result["interrupted_files"]), 2)
        preserved = [outdir / item["file"] for item in result["interrupted_files"]]
        self.assertEqual({path.read_bytes() for path in preserved},
                         {b"incomplete", b"unfinished manifest"})
        self.assertEqual(verify_snapshot(folder)["decrypted_slots"], 15)

    def test_exact_valid_slots_and_extent(self):
        self.write_table()
        rows = table(self.raw)
        self.assertEqual(tuple(row["slot"] for row in rows), VALID_SLOTS)
        self.assertEqual(rows[0]["start_sector"], 128)
        self.assertEqual(rows[-1]["start_sector"] + rows[-1]["sectors"],
                         self.raw.stat().st_size // 512)

    def test_internal_gap_is_preserved(self):
        self.write_table(gap=True)
        self.assertEqual(len(table(self.raw)), len(VALID_SLOTS))

    def test_different_slots_and_tail_are_supported_but_overlap_is_not(self):
        header = bytearray(128 * 512)
        header[:8] = b"82462.00"
        struct.pack_into("<I", header, 8, 2)
        struct.pack_into("<4I", header, 32, 128, 2, 5, 1)
        struct.pack_into("<4I", header, 48, 132, 1, 6, 1)
        self.raw.write_bytes(header + bytes(6 * 512))
        self.assertEqual([x["slot"] for x in table(self.raw)], [1, 2])
        struct.pack_into("<I", header, 48, 129)
        self.raw.write_bytes(header + bytes(6 * 512))
        with self.assertRaisesRegex(RuntimeError, "overlap"):
            table(self.raw)

    def test_unsupported_layout_keeps_verified_raw_and_reports_raw_only(self):
        folder = self.verified_raw()
        data = bytearray(self.raw.read_bytes())
        data[:8] = b"UNKNOWN!"
        # Update both independent saved observations and their committed hashes.
        verified = json.loads((folder / "verified.json").read_text())
        receipt = json.loads((folder / "capture-receipt.json").read_text())
        digest = hashlib.sha256(data).hexdigest()
        verified["nflasha"]["sha256"] = digest
        for attempt, suffix in (("first", "raw"), ("second", "repeat")):
            (folder / f"nflasha.{suffix}.bin").write_bytes(data)
            receipt["reads"]["nflasha"][attempt]["sha256"] = digest
        (folder / "verified.json").write_text(json.dumps(verified))
        (folder / "capture-receipt.json").write_text(json.dumps(receipt))
        with patch.object(decrypt_snapshot, "derive_key", side_effect=AssertionError("no key needed")):
            result = decrypt(folder)
        self.assertEqual(result["decryption_status"], "UNSUPPORTED")
        self.assertEqual(verify_snapshot(folder)["decrypted_slots"], 0)
        self.assertEqual(self.raw.read_bytes(), data)

    def test_decrypt_requires_matching_second_read(self):
        self.write_table()
        folder = self.raw.parent
        verified = {}
        reads = {}
        for region in ("nflashaB0", "nflashaB1", "nflasha", "cp-nor"):
            payload = self.raw.read_bytes() if region == "nflasha" else bytes(512)
            digest = hashlib.sha256(payload).hexdigest()
            verified[region] = {"bytes": len(payload), "sha256": digest}
            reads[region] = {}
            for attempt, suffix in (("first", "raw"), ("second", "repeat")):
                name = f"{region}.{suffix}.bin"
                (folder / name).write_bytes(payload)
                reads[region][attempt] = {"file": name, "bytes": len(payload),
                                          "sha256": digest}
        (folder / "verified.json").write_text(json.dumps(verified))
        (folder / "capture-receipt.json").write_text(
            json.dumps({"status": "VERIFIED", "reads": reads}))
        decrypted = folder / "decrypted"
        decrypted.mkdir()
        rows = []
        for entry in table(self.raw):
            name = f"nflasha{entry['slot']:02d}.decrypted.bin"
            payload = bytes(entry["bytes"])
            (decrypted / name).write_bytes(payload)
            rows.append(dict(entry, file=name, sha256=hashlib.sha256(payload).hexdigest()))
        (decrypted / "manifest.json").write_text(json.dumps({
            "status": "VERIFIED", "source_snapshot": verified, "partitions": rows}))
        self.assertEqual(verify_snapshot(folder)["decrypted_slots"], 15)
        # A restore-only current snapshot records the omission explicitly;
        # its three captured regions still require both matching raw reads.
        del verified["cp-nor"]
        del reads["cp-nor"]
        (folder / "cp-nor.raw.bin").unlink()
        (folder / "cp-nor.repeat.bin").unlink()
        (folder / "verified.json").write_text(json.dumps(verified))
        (folder / "capture-receipt.json").write_text(json.dumps({
            "status": "VERIFIED", "reads": reads,
            "capture_profile": RESTORE_CURRENT_PROFILE,
            "not_captured_regions": ["cp-nor"]}))
        manifest = json.loads((decrypted / "manifest.json").read_text())
        manifest["source_snapshot"] = verified
        (decrypted / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(verify_snapshot(folder)["raw_regions"], 3)
        (folder / "nflasha.repeat.bin").write_bytes(b"X" + self.raw.read_bytes()[1:])
        with self.assertRaisesRegex(RuntimeError, "double-read evidence mismatch"):
            decrypt(folder)
        with self.assertRaisesRegex(RuntimeError, "snapshot double-read mismatch"):
            verify_snapshot(folder)


if __name__ == "__main__":
    unittest.main()
