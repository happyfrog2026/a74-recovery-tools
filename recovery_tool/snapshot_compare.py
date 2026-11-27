"""Offline comparison and non-executable restore planning for camera snapshots.

No USB import, command dispatch, or firmware write exists in this module.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import json
import os
from pathlib import Path
import re
import struct

SECTOR = 512
REGIONS = ("nflashaB0", "nflashaB1", "nflasha", "cp-nor")
MAX_RANGES = 4096
FAT_FILE_LIMIT = 600 * 1024 * 1024


class SnapshotError(RuntimeError):
    pass


def _require(ok: bool, message: str):
    if not ok:
        raise SnapshotError(message)


def _sha_file(path: Path, expected_size: int, expected_hash: str) -> None:
    _require(path.is_file() and path.stat().st_size == expected_size, f"missing or wrong-size snapshot file: {path}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    _require(actual == expected_hash, f"snapshot SHA-256 mismatch: {path}: {actual}")


def _raw_manifest(folder: Path, *, allow_restore_current: bool = False,
                  allow_dynamic: bool = False) -> dict:
    path = folder / "verified.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    partial = False
    if allow_restore_current:
        from .snapshot import RESTORE_CURRENT_PROFILE
        receipt = json.loads((folder / "capture-receipt.json").read_text(encoding="utf-8"))
        partial = (receipt.get("capture_profile") == RESTORE_CURRENT_PROFILE and
                   receipt.get("not_captured_regions") == ["cp-nor"])
        _require((receipt.get("capture_profile") is None and
                  receipt.get("not_captured_regions") is None) or partial,
                 "unknown current restore capture profile")
    if allow_dynamic:
        from .snapshot import captured_regions
        receipt = json.loads((folder / "capture-receipt.json").read_text(encoding="utf-8"))
        expected = tuple(captured_regions(receipt))
    else:
        expected = tuple(region for region in REGIONS if not (partial and region == "cp-nor"))
    _require(set(data) == set(expected), f"raw manifest must contain exactly {expected}: {path}")
    for region in expected:
        record = data[region]
        _require(isinstance(record.get("bytes"), int) and record["bytes"] > 0 and
                 re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")), f"invalid {region} manifest entry")
        _require(record["bytes"] % SECTOR == 0, f"{region} size is not sector aligned")
        _sha_file(folder / f"{region}.raw.bin", record["bytes"], record["sha256"])
    return data


def _sdm2(path: Path) -> tuple[list[dict], bytes]:
    with path.open("rb") as stream:
        header = stream.read(4096)
    _require(len(header) >= SECTOR and header[:4] == b"8246", f"invalid SDM2 header: {path}")
    count = struct.unpack_from("<I", header, 8)[0]
    _require(0 < count <= (len(header) - 32) // 16, f"unexpected SDM2 slot count {count}: {path}")
    slots = []
    for i in range(count):
        start, sectors, kind, flags = struct.unpack_from("<4I", header, 32 + 16 * i)
        if flags & 1:
            _require(sectors > 0, f"empty valid SDM2 slot {i + 1}")
            slots.append({"name": f"nflasha{i + 1}", "start_sector": start,
                          "sectors": sectors, "partition_type": kind})
    ordered = sorted(slots, key=lambda s: s["start_sector"])
    for left, right in zip(ordered, ordered[1:]):
        _require(left["start_sector"] + left["sectors"] <= right["start_sector"], "overlapping SDM2 slots")
    return slots, header[:SECTOR]


def _changed_sectors(before: Path, after: Path, size: int, slots: list[dict] | None = None) -> dict:
    """Compare two verified files in bounded memory; retain bounded range detail."""
    sectors = range_count = 0
    ranges: list[list[int]] = []
    last_start = last_end = None
    ordered = sorted(slots or [], key=lambda s: s["start_sector"])
    starts = [s["start_sector"] for s in ordered]
    mapped_counts = {s["name"]: 0 for s in ordered}
    outside = 0
    with before.open("rb") as old, after.open("rb") as new:
        index = 0
        while index * SECTOR < size:
            amount = min(4 * 1024 * 1024, size - index * SECTOR)
            a, b = old.read(amount), new.read(amount)
            _require(len(a) == len(b) == amount, "snapshot truncated while comparing")
            if a == b:
                index += amount // SECTOR
                continue
            for pos in range(0, amount, SECTOR):
                if a[pos:pos + SECTOR] != b[pos:pos + SECTOR]:
                    at = index + pos // SECTOR
                    sectors += 1
                    if slots is not None:
                        slot_index = bisect_right(starts, at) - 1
                        if slot_index >= 0 and at < ordered[slot_index]["start_sector"] + ordered[slot_index]["sectors"]:
                            mapped_counts[ordered[slot_index]["name"]] += 1
                        else:
                            outside += 1
                    if last_end == at:
                        last_end += 1
                        if range_count <= MAX_RANGES:
                            ranges[-1][1] = last_end
                    else:
                        last_start, last_end = at, at + 1
                        range_count += 1
                        if range_count <= MAX_RANGES:
                            ranges.append([last_start, last_end])
            index += amount // SECTOR
    result = {"changed_sectors": sectors, "range_count": range_count,
              "ranges": ranges, "ranges_truncated": range_count > MAX_RANGES}
    if slots is not None:
        result["partition_changed_sectors"] = {k: v for k, v in mapped_counts.items() if v}
        result["outside_valid_partitions_changed_sectors"] = outside
    return result


def iter_changed_ranges(before: Path, after: Path, size: int):
    """Yield every changed sector run using bounded memory, independent of report limits."""
    _require(size >= 0 and size % SECTOR == 0, "comparison size is not sector aligned")
    run_start = run_end = None
    with before.open("rb") as old, after.open("rb") as new:
        index = 0
        while index * SECTOR < size:
            amount = min(4 * 1024 * 1024, size - index * SECTOR)
            a, b = old.read(amount), new.read(amount)
            _require(len(a) == len(b) == amount, "snapshot truncated while streaming ranges")
            if a == b:
                if run_start is not None:
                    yield [run_start, run_end]
                    run_start = run_end = None
            else:
                for pos in range(0, amount, SECTOR):
                    at = index + pos // SECTOR
                    if a[pos:pos + SECTOR] != b[pos:pos + SECTOR]:
                        if run_start is None:
                            run_start = at
                        run_end = at + 1
                    elif run_start is not None:
                        yield [run_start, run_end]
                        run_start = run_end = None
            index += amount // SECTOR
        _require(old.read(1) == new.read(1) == b"", "image exceeds declared comparison size")
    if run_start is not None:
        yield [run_start, run_end]


def _decrypted_manifest(path: Path | None, raw_slots: list[dict]) -> dict:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    available = {row["name"]: row for row in raw_slots}
    result = {}
    for row in data["partitions"]:
        name = Path(row["file"]).stem.split(".")[0]
        match = re.fullmatch(r"nflasha(\d{2,3})", name)
        _require(match is not None, f"invalid decrypted partition filename: {name}")
        region = f"nflasha{int(match.group(1))}"
        _require(region in available and region not in result, f"decrypted partition not in SDM2: {region}")
        slot = available[region]
        _require((row["start_sector"], row["sectors"], row["partition_type"]) ==
                 (slot["start_sector"], slot["sectors"], slot["partition_type"]),
                 f"decrypted partition mapping mismatch: {region}")
        _require(row["bytes"] == row["sectors"] * SECTOR, f"decrypted size mismatch: {region}")
        file = path.parent / row["file"]
        _sha_file(file, row["bytes"], row["sha256"])
        result[region] = {**row, "path": str(file.resolve())}
    return result


def _fat_changes(left: Path, right: Path) -> dict:
    """Optional content-level inventory for manageable FAT12/16 partitions."""
    try:
        from .stock_fat import Fat
        def inventory(path):
            fs = Fat(path.read_bytes())
            found = {}
            def walk(first=None, prefix=""):
                for name, (_, cluster, size, attr) in fs.entries(first).items():
                    if name in (".", ".."):
                        continue
                    if attr & 16:
                        walk(cluster, prefix + name + "/")
                    else:
                        found[prefix + name] = {"bytes": size,
                                                "sha256": hashlib.sha256(fs.read(prefix + name)).hexdigest()}
            walk()
            return found
        a, b = inventory(left), inventory(right)
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc)}
    changed = sorted(name for name in a.keys() | b.keys() if a.get(name) != b.get(name))
    return {"status": "compared", "changed_paths": changed,
            "changed_count": len(changed), "metadata_or_free_space_changes_possible": True}


def _map_ranges(ranges: list[list[int]], slots: list[dict]) -> dict:
    mapped = {s["name"]: {"changed_sectors": 0, "ranges": []} for s in slots}
    outside = 0
    for start, end in ranges:
        covered = 0
        for slot in slots:
            lo, hi = slot["start_sector"], slot["start_sector"] + slot["sectors"]
            a, b = max(start, lo), min(end, hi)
            if a < b:
                mapped[slot["name"]]["changed_sectors"] += b - a
                mapped[slot["name"]]["ranges"].append([a - lo, b - lo])
                covered += b - a
        outside += end - start - covered
    return {"partitions": {k: v for k, v in mapped.items() if v["changed_sectors"]},
            "outside_valid_partitions_changed_sectors": outside}


def compare(old_dir: Path, new_dir: Path, *, old_decrypted: Path | None = None,
            new_decrypted: Path | None = None,
            allow_restore_current: bool = False, allow_dynamic: bool = False) -> dict:
    """Verify both manifests and return evidence-backed, non-executable plans."""
    old_dir, new_dir = Path(old_dir).resolve(), Path(new_dir).resolve()
    old = _raw_manifest(old_dir, allow_restore_current=allow_restore_current, allow_dynamic=allow_dynamic)
    new = _raw_manifest(new_dir, allow_dynamic=allow_dynamic)
    layout_errors = {}
    def layout(folder, records, label):
        try:
            if allow_dynamic:
                manifest_path = folder / "decrypted/manifest.json"
                if manifest_path.is_file():
                    decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
                    _require(decoded.get("decryption_status") != "UNSUPPORTED",
                             f"{label} user-area decryption is unsupported: {decoded.get('decryption_error', '')}")
            _require("nflasha" in records, "user area was not captured")
            slots, header = _sdm2(folder / "nflasha.raw.bin")
            _require(all(s["start_sector"] + s["sectors"] <= records["nflasha"]["bytes"] // SECTOR
                         for s in slots), f"{label} SDM2 slot exceeds captured user area")
            return slots, header
        except SnapshotError as exc:
            if not allow_dynamic:
                raise
            layout_errors[label] = str(exc)
            return [], None
    old_slots, old_header = layout(old_dir, old, "old")
    new_slots, new_header = layout(new_dir, new, "new")
    same_mapping = not layout_errors and old_slots == new_slots
    differences = {}
    for region in REGIONS:
        if allow_dynamic and (region not in old or region not in new):
            a, b = old.get(region), new.get(region)
            differences[region] = {"status": "not_captured" if b else "missing_from_target",
                                   "old_sha256": a["sha256"] if a else None,
                                   "new_sha256": b["sha256"] if b else None,
                                   "old_bytes": a["bytes"] if a else None,
                                   "new_bytes": b["bytes"] if b else None}
            continue
        if region not in old:
            _require(allow_restore_current and region == "cp-nor", "unexpected uncaptured raw region")
            differences[region] = {"status": "not_captured", "old_sha256": None,
                                   "new_sha256": new[region]["sha256"],
                                   "old_bytes": None, "new_bytes": new[region]["bytes"]}
            continue
        same_size = old[region]["bytes"] == new[region]["bytes"]
        same = same_size and old[region]["sha256"] == new[region]["sha256"]
        differences[region] = {"status": "size_changed" if not same_size else "unchanged" if same else "changed",
                               "old_sha256": old[region]["sha256"],
                               "new_sha256": new[region]["sha256"],
                               "old_bytes": old[region]["bytes"], "new_bytes": new[region]["bytes"]}
        if same_size and not same:
            differences[region].update(_changed_sectors(old_dir / f"{region}.raw.bin",
                                                        new_dir / f"{region}.raw.bin", old[region]["bytes"],
                                                        new_slots if region == "nflasha" and same_mapping else None))
    old_dec = _decrypted_manifest(old_decrypted, old_slots)
    new_dec = _decrypted_manifest(new_decrypted, new_slots)
    decrypted = {}
    for name in sorted(old_dec.keys() | new_dec.keys()):
        a, b = old_dec.get(name), new_dec.get(name)
        if not a or not b:
            decrypted[name] = {"status": "missing_from_one_snapshot"}
            continue
        same_size = a["bytes"] == b["bytes"]
        same = same_size and a["sha256"] == b["sha256"]
        row = {"status": "size_changed" if not same_size else "unchanged" if same else "changed",
               "old_sha256": a["sha256"], "new_sha256": b["sha256"],
               "old_bytes": a["bytes"], "new_bytes": b["bytes"]}
        if same_size and not same:
            row.update(_changed_sectors(Path(a["path"]), Path(b["path"]), a["bytes"]))
            if a["partition_type"] == b["partition_type"] == 5 and a["bytes"] <= FAT_FILE_LIMIT:
                row["files"] = _fat_changes(Path(a["path"]), Path(b["path"]))
            else:
                row["files"] = {"status": "unavailable",
                                "reason": "file-level parser supports FAT12/16 partitions up to 600 MiB only"}
        decrypted[name] = row
    raw_user = differences["nflasha"]
    if raw_user["status"] == "changed" and same_mapping and not raw_user["ranges_truncated"]:
        raw_user["sdm2_map"] = _map_ranges(raw_user["ranges"], new_slots)
    elif raw_user["status"] == "changed" and same_mapping:
        raw_user["sdm2_map"] = {"status": "range_detail_truncated",
                                "partition_changed_sectors": raw_user["partition_changed_sectors"],
                                "outside_valid_partitions_changed_sectors": raw_user["outside_valid_partitions_changed_sectors"]}
    elif raw_user["status"] == "changed":
        raw_user["sdm2_map"] = {"status": "unavailable", "reason": "mapping changed or range detail truncated"}
    candidates = []
    for region in REGIONS:
        if differences[region]["status"] == "unchanged":
            continue
        if differences[region]["status"] == "size_changed":
            candidates.append({"region": region, "payload_route": None,
                               "reason": "captured region sizes differ; review layout and capacity before restoration",
                               "automatic_apply_allowed": False})
            continue
        if region in ("nflashaB0", "nflashaB1"):
            candidates.append({"region": region, "representation": "raw", "payload_route": f"flash:{region}",
                               "old_source": str((old_dir / f"{region}.raw.bin").resolve()),
                               "prerequisites": ["same-camera identity", "fresh complete prewrite", "complete readback"],
                               "automatic_apply_allowed": False})
        elif region == "cp-nor":
            candidates.append({"region": region, "representation": "raw", "payload_route": None,
                               "reason": "payload has NOR read and low-level norcmd, no general scoped flash command",
                               "automatic_apply_allowed": False})
        else:
            candidates.append({"region": "nflasha", "representation": "raw ciphertext/user table",
                               "payload_route": "flash:nflasha", "automatic_apply_allowed": False,
                               "reason": "raw whole-user write is possible in payload, but requires separate layout and encryption review"})
    if same_mapping:
        for slot in old_slots:
            name = slot["name"]
            if decrypted.get(name, {}).get("status") == "changed":
                candidates.append({"region": name, "representation": "decrypted partition",
                                   "payload_route": f"flash:{name}", "old_source": old_dec[name]["path"],
                                   "prerequisites": ["same-camera identity", "matching SDM2 mapping",
                                                     "fresh complete prewrite", "complete readback"],
                                   "automatic_apply_allowed": False})
    return {"schema": "snapshot-comparison-v1", "offline_only": True,
            "old_manifest": str((old_dir / "verified.json").resolve()),
            "new_manifest": str((new_dir / "verified.json").resolve()),
            "raw_files_sha256_verified": True, "uncaptured_regions": ["cp-nor"] if "cp-nor" not in old else [],
            "decrypted_files_sha256_verified": bool(old_decrypted and new_decrypted),
            "same_camera_identity_proven": False,
            "sdm2": {"mapping_same": same_mapping, "header_sector_same": old_header is not None and old_header == new_header,
                     **({"layout_errors": layout_errors} if layout_errors else {}),
                     "old_slots": old_slots, "new_slots": new_slots},
            "raw": differences, "decrypted": decrypted, "restore_candidates": candidates,
            "notes": ["No restore command is authorized by this report.",
                      "Raw user-area and decrypted partition routes are alternatives, not cumulative actions.",
                      "The payload's nflasha extent is SDM2-derived, not physical eMMC capacity.",
                      "Changed CP NOR sectors use 512-byte comparison units, not erase/program granularity.",
                      "Physical CP NOR and other hardware state require separate restore methods."]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify and compare two offline camera snapshots")
    parser.add_argument("old", type=Path, help="snapshot directory containing verified.json and raw files")
    parser.add_argument("new", type=Path, help="new snapshot directory with the same layout")
    parser.add_argument("--old-decrypted", type=Path, help="old decrypted/manifest.json")
    parser.add_argument("--new-decrypted", type=Path, help="new decrypted/manifest.json")
    parser.add_argument("--output", type=Path, required=True, help="new JSON evidence path")
    args = parser.parse_args(argv)
    try:
        report = compare(args.old, args.new, old_decrypted=args.old_decrypted,
                         new_decrypted=args.new_decrypted)
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except (SnapshotError, OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"snapshot-compare: {exc}\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
