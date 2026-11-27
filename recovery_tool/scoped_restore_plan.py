"""Offline, fail-closed plan for restoring changed decrypted SDM2 partitions.

The only output is a reviewable JSON plan. This module has no USB imports or
write command, and a plan does not authorize a physical restore.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import struct
from typing import Iterable

from . import snapshot_compare as comparison

SECTOR = 512
FRAGMENT_VALIDATION_MODE = "fragment-readback-v1"
MAX_DOWNLOAD = 512 * 1024
MAX_RAW_SECTORS = (MAX_DOWNLOAD - 28 - 3 * 12) // SECTOR
SPARSE_MAGIC = 0xED26FF3A
RAW = 0xCAC1
SKIP = 0xCAC3


class ScopedPlanError(RuntimeError):
    pass


def _require(ok: bool, reason: str):
    if not ok:
        raise ScopedPlanError(reason)


def _manifest_status(folder: Path, *, allow_restore_current: bool = False,
                     allow_dynamic: bool = False) -> None:
    for name in ("capture-receipt.json", "decrypted/manifest.json"):
        path = folder / name
        _require(path.is_file(), f"missing verified capture evidence: {path}")
        _require(json.loads(path.read_text(encoding="utf-8")).get("status") == "VERIFIED",
                 f"capture evidence is not VERIFIED: {path}")
    verified = json.loads((folder / "verified.json").read_text(encoding="utf-8"))
    receipt = json.loads((folder / "capture-receipt.json").read_text(encoding="utf-8"))
    decrypted = json.loads((folder / "decrypted/manifest.json").read_text(encoding="utf-8"))
    from .snapshot import RESTORE_CURRENT_PROFILE
    partial = (allow_restore_current and receipt.get("capture_profile") == RESTORE_CURRENT_PROFILE and
               receipt.get("not_captured_regions") == ["cp-nor"])
    _require((receipt.get("capture_profile") is None and receipt.get("not_captured_regions") is None)
             or partial, "unknown or disallowed raw capture profile")
    if allow_dynamic:
        from .snapshot import captured_regions
        expected = set(captured_regions(receipt))
    else:
        expected = set(comparison.REGIONS) - ({"cp-nor"} if partial else set())
    _require(set(verified) == expected, "raw capture manifest is incomplete for its recorded profile")
    _require(set(receipt.get("reads", {})) == expected,
             "double-read receipt region set differs from recorded capture profile")
    _require(decrypted.get("source_snapshot") == verified,
             "decrypted manifest is not pinned to this raw snapshot")
    for region, record in verified.items():
        if "capture_regions" in receipt:
            _require(record["bytes"] == receipt["capture_regions"][region],
                     f"capture extent differs from verified image: {region}")
        reads = receipt.get("reads", {}).get(region, {})
        for attempt, filename in (("first", f"{region}.raw.bin"),
                                  ("second", f"{region}.repeat.bin")):
            item = reads.get(attempt, {})
            _require((item.get("file"), item.get("bytes"), item.get("sha256")) ==
                     (filename, record["bytes"], record["sha256"]),
                     f"double-read receipt mismatch: {region} {attempt}")
            file = folder / filename
            _require(file.is_file() and file.stat().st_size == record["bytes"] and
                     _hash_file(file) == record["sha256"],
                     f"double-read file mismatch: {region} {attempt}")


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _chunk(kind: int, sectors: int, payload: bytes = b"") -> bytes:
    return struct.pack("<HHII", kind, 0, sectors, 12 + len(payload)) + payload


def sparse_fragment(total_sectors: int, start: int, data: bytes) -> bytes:
    """Single changed run within one partition, using full-partition sparse extent."""
    count = len(data) // SECTOR
    _require(len(data) % SECTOR == 0 and 0 < count <= MAX_RAW_SECTORS,
             "invalid sparse fragment size")
    _require(0 <= start < total_sectors and count <= total_sectors - start,
             "sparse fragment exceeds partition")
    chunks = []
    if start:
        chunks.append(_chunk(SKIP, start))
    chunks.append(_chunk(RAW, count, data))
    tail = total_sectors - start - count
    if tail:
        chunks.append(_chunk(SKIP, tail))
    header = struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12,
                         SECTOR, total_sectors, len(chunks), 0)
    encoded = header + b"".join(chunks)
    _require(len(encoded) <= MAX_DOWNLOAD, "sparse fragment exceeds download limit")
    return encoded


def _fragments(current: Path, target: Path, total_sectors: int,
               ranges: Iterable[list[int]], *, target_digests: bool = False) -> tuple[list[dict], str]:
    """Generate bounded fragments; hash a full virtual replay without copying an image."""
    result: list[dict] = []
    replay = hashlib.sha256()
    cursor = 0
    with current.open("rb") as before, target.open("rb") as after:
        for start, end in ranges:
            _require(cursor <= start < end <= total_sectors, "invalid or overlapping changed ranges")
            while cursor < start:
                chunk = before.read(min((start - cursor) * SECTOR, 4 * 1024 * 1024))
                _require(bool(chunk) and len(chunk) % SECTOR == 0, "short current image")
                replay.update(chunk)
                after.seek(len(chunk), 1)
                cursor += len(chunk) // SECTOR
            while cursor < end:
                sectors = min(end - cursor, MAX_RAW_SECTORS)
                old = before.read(sectors * SECTOR)
                new = after.read(sectors * SECTOR)
                _require(len(old) == len(new) == sectors * SECTOR and old != new,
                         "changed range does not match images")
                _require(all(old[i:i + SECTOR] != new[i:i + SECTOR]
                             for i in range(0, len(old), SECTOR)),
                         "range contains an unchanged sector")
                fragment = sparse_fragment(total_sectors, cursor, new)
                result.append({"start_sector": cursor, "sectors": sectors,
                               "bytes": len(fragment),
                               "sha256": hashlib.sha256(fragment).hexdigest(),
                               **({"target_sha256": hashlib.sha256(new).hexdigest()} if target_digests else {})})
                replay.update(new)
                cursor += sectors
        while cursor < total_sectors:
            chunk = before.read(min((total_sectors - cursor) * SECTOR, 4 * 1024 * 1024))
            _require(bool(chunk) and len(chunk) % SECTOR == 0, "short current image")
            replay.update(chunk)
            after.seek(len(chunk), 1)
            cursor += len(chunk) // SECTOR
        _require(before.read(1) == after.read(1) == b"", "image exceeds declared partition")
    return result, replay.hexdigest()


def _complete_ranges(current: Path, target: Path, row: dict) -> Iterable[list[int]]:
    """Use bounded report detail or rescan the pinned files without a range cap."""
    if row.get("ranges_truncated"):
        source = comparison.iter_changed_ranges(current, target, row["old_bytes"])
    else:
        source = iter(row["ranges"])
    count = sectors = 0
    last_end = 0
    for start, end in source:
        _require(last_end <= start < end <= row["old_bytes"] // SECTOR,
                 "invalid complete changed range")
        count += 1
        sectors += end - start
        last_end = end
        yield [start, end]
    _require(count == row["range_count"] and sectors == row["changed_sectors"],
             "complete changed ranges disagree with verified comparison")


def _decrypted_ranges(current_dec: dict, target_dec: dict, report: dict, slots: list[dict]):
    for slot in sorted(slots, key=lambda row: row["start_sector"]):
        name = slot["name"]
        row = report["decrypted"][name]
        if row["status"] != "changed":
            continue
        source, target = Path(current_dec[name]["path"]), Path(target_dec[name]["path"])
        for start, end in _complete_ranges(source, target, row):
            yield (name, start, end)


def _assert_same_range_positions(raw_ranges, decrypted_ranges):
    """Compare changed sector positions, never ciphertext to plaintext bytes."""
    sentinel = object()
    for raw, plain in itertools.zip_longest(raw_ranges, decrypted_ranges, fillvalue=sentinel):
        _require(raw == plain, "raw/decrypted changed-sector positions differ")


def _fragment_plan(current_dir: Path, target_dir: Path, report: dict) -> dict:
    """Plan A!=B on live-addressable logical regions; unsupported routes are explicit."""
    old_slots, new_slots = report["sdm2"]["old_slots"], report["sdm2"]["new_slots"]
    current_dec = comparison._decrypted_manifest(current_dir / "decrypted/manifest.json", old_slots)
    target_dec = comparison._decrypted_manifest(target_dir / "decrypted/manifest.json", new_slots)
    partitions, skipped = [], []
    user = report["raw"]["nflasha"]
    if (not report["sdm2"]["mapping_same"] or not report["sdm2"]["header_sector_same"] or
            user["status"] == "size_changed" or user.get("outside_valid_partitions_changed_sectors", 0)):
        skipped.append({"name": "nflasha", "reason": "raw_layout_or_unmapped_write_route_unavailable",
                        "mapping_same": report["sdm2"]["mapping_same"],
                        "header_sector_same": report["sdm2"]["header_sector_same"],
                        "unmapped_changed_sectors": user.get("outside_valid_partitions_changed_sectors"),
                        "changed": True})
    # When positions have the same meaning, retain exact ciphertext/plaintext
    # changed-sector correlation. Raw header/gap differences are separately skipped.
    if report["sdm2"]["mapping_same"] and user["status"] == "changed":
        def mapped():
            for start, end in _complete_ranges(current_dir / "nflasha.raw.bin",
                                                target_dir / "nflasha.raw.bin", user):
                for slot in sorted(old_slots, key=lambda row: row["start_sector"]):
                    low, high = max(start, slot["start_sector"]), min(end, slot["start_sector"] + slot["sectors"])
                    if low < high:
                        yield (slot["name"], low - slot["start_sector"], high - slot["start_sector"])
        _assert_same_range_positions(mapped(), _decrypted_ranges(current_dec, target_dec, report, old_slots))
    for name in sorted(set(current_dec) | set(target_dec), key=lambda value: int(value.removeprefix("nflasha"))):
        source, target = current_dec.get(name), target_dec.get(name)
        slot_number = int(name.removeprefix("nflasha"))
        reason = ("fastboot_slot_out_of_range" if slot_number > 126 else
                  "target_partition_missing" if target is None else
                  "live_partition_missing" if source is None else
                  "live_target_partition_size_differs" if source["bytes"] != target["bytes"] else
                  "unsupported_or_changed_partition_type" if source["partition_type"] not in (4, 5, 6) or
                  source["partition_type"] != target["partition_type"] else None)
        if reason:
            skipped.append({"name": name, "reason": reason,
                            "current_bytes": source["bytes"] if source else None,
                            "target_bytes": target["bytes"] if target else None})
            continue
        row = report["decrypted"][name]
        if row["status"] == "unchanged":
            continue
        _require(row["status"] == "changed", f"invalid compatible partition comparison: {name}")
        fragments, replay = _fragments(Path(source["path"]), Path(target["path"]), source["sectors"],
                                        _complete_ranges(Path(source["path"]), Path(target["path"]), row),
                                        target_digests=True)
        _require(replay == target["sha256"] and sum(x["sectors"] for x in fragments) == row["changed_sectors"],
                 f"planned fragment coverage differs: {name}")
        partitions.append({"region": name, "start_sector": source["start_sector"],
                           "target_start_sector": target["start_sector"],
                           "sectors": source["sectors"], "partition_type": source["partition_type"],
                           "current_image": source["path"], "current_sha256": source["sha256"],
                           "target_image": target["path"], "target_sha256": target["sha256"],
                           "changed_sectors": row["changed_sectors"], "fragments": fragments,
                           "replay_equals_target": True})
    for name in ("nflashaB1", "nflashaB0"):
        row = report["raw"][name]
        if row["status"] == "unchanged":
            continue
        if row["status"] != "changed":
            skipped.append({"name": name, "reason": "live_target_boot_size_differs"})
            continue
        source, target = current_dir / f"{name}.raw.bin", target_dir / f"{name}.raw.bin"
        fragments, replay = _fragments(source, target, row["old_bytes"] // SECTOR,
                                        _complete_ranges(source, target, row), target_digests=True)
        _require(replay == row["new_sha256"], f"boot sparse replay differs: {name}")
        partitions.append({"region": name, "start_sector": 0, "sectors": row["old_bytes"] // SECTOR,
                           "partition_type": "raw_emmc_boot", "current_image": str(source),
                           "current_sha256": row["old_sha256"], "target_image": str(target),
                           "target_sha256": row["new_sha256"], "changed_sectors": row["changed_sectors"],
                           "fragments": fragments, "replay_equals_target": True})
    nor = report["raw"]["cp-nor"]
    if nor["status"] != "unchanged":
        skipped.append({"name": "cp-nor", "reason": "not_captured_read_only" if nor["status"] == "not_captured"
                        else "read_only_no_write_route", "changed": None if nor["status"] == "not_captured" else True})
    sector0_hash = None
    mapping_pin_bytes = None
    mapping_pin_hash = None
    if user.get("old_bytes") is not None:
        user_slots = [int(row["region"].removeprefix("nflasha")) for row in partitions
                      if row["region"].startswith("nflasha") and
                      row["region"] not in {"nflashaB0", "nflashaB1"}]
        mapping_pin_bytes = max(SECTOR, ((32 + max(user_slots, default=0) * 16 + SECTOR - 1)
                                         // SECTOR) * SECTOR)
        _require(mapping_pin_bytes <= 4 * SECTOR and mapping_pin_bytes <= user["old_bytes"],
                 "live SDM2 mapping table cannot be pinned")
        with (current_dir / "nflasha.raw.bin").open("rb") as stream:
            sector0_hash = hashlib.sha256(stream.read(SECTOR)).hexdigest()
            stream.seek(0)
            mapping_pin_hash = hashlib.sha256(stream.read(mapping_pin_bytes)).hexdigest()
    result = {"schema": "scoped-restore-plan-v1", "status": "OFFLINE_PLAN_ONLY", "profile": "capability",
              "validation_mode": FRAGMENT_VALIDATION_MODE,
              "current_snapshot": str(current_dir), "target_snapshot": str(target_dir),
              "same_camera_identity_proven": False, "sdm2_sector0_sha256": sector0_hash,
              "sdm2_mapping_pin_bytes": mapping_pin_bytes,
              "sdm2_mapping_pin_sha256": mapping_pin_hash,
              "raw_user_sha256": {"current": user["old_sha256"], "target": user["new_sha256"]},
              "partitions": partitions, "skipped_regions": skipped,
              "fully_matches_snapshot_after_write": False, "write_authorized": False,
              "completion_scope": "planned_changed_fragments",
              "requirements_before_any_apply": ["live address map matches captured current map",
                  "write only planned A!=B fragments", "immediate target fragment readback; at most two writes",
                  "record persistent mismatches and continue; preserve communication uncertainty"]}
    for label, folder in (("current", current_dir), ("target", target_dir)):
        for key, name in (("verified_manifest", "verified.json"), ("capture_receipt", "capture-receipt.json"),
                          ("decrypted_manifest", "decrypted/manifest.json")):
            result[f"{label}_{key}_sha256"] = _hash_file(folder / name)
    return result


def build_plan(current_dir: Path, target_dir: Path, *, profile: str = "capability",
               validation_mode: str = FRAGMENT_VALIDATION_MODE) -> dict:
    """Pin A and B, then plan only their differences on supported live routes."""
    _require(profile == "capability" and validation_mode == FRAGMENT_VALIDATION_MODE,
             "restore supports only the target-fragment capability workflow")
    current_dir, target_dir = Path(current_dir).resolve(), Path(target_dir).resolve()
    _require(current_dir != target_dir, "current and target snapshot must differ")
    dynamic = validation_mode == FRAGMENT_VALIDATION_MODE
    _manifest_status(current_dir, allow_restore_current=profile == "capability", allow_dynamic=dynamic)
    _manifest_status(target_dir, allow_dynamic=dynamic)
    report = comparison.compare(current_dir, target_dir,
                                old_decrypted=current_dir / "decrypted/manifest.json",
                                new_decrypted=target_dir / "decrypted/manifest.json",
                                allow_restore_current=profile == "capability", allow_dynamic=dynamic)
    if validation_mode == FRAGMENT_VALIDATION_MODE:
        return _fragment_plan(current_dir, target_dir, report)


def capability_identity(current_dir: Path, target_dir: Path, plan: dict) -> list[dict]:
    """Pin each table sector needed to resolve every planned live user slot."""
    return ([{"region": "nflasha", "bytes": plan["sdm2_mapping_pin_bytes"],
              "sha256": plan["sdm2_mapping_pin_sha256"]}]
            if plan.get("sdm2_mapping_pin_sha256") else [])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline scoped differential restore plan")
    parser.add_argument("current", type=Path, help="verified current snapshot (preimage)")
    parser.add_argument("target", type=Path, help="verified accepted target snapshot")
    parser.add_argument("--output", type=Path, required=True, help="new private JSON plan path")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.current, args.target)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as out:
            os.chmod(args.output, 0o600)
            json.dump(plan, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        print(json.dumps({"status": plan["status"], "partitions": len(plan["partitions"]),
                          "output": str(args.output.resolve())}))
        return 0
    except (ScopedPlanError, comparison.SnapshotError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"scoped restore plan: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
