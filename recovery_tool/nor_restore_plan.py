"""Plan a complete saved raw NOR image using proposed erase units, offline.

This module has no device transport or executor. Sony's reader/writer byte
representation, current flash geometry and controlled Linux environment still
need qualification. An offline plan never admits a NOR write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import BinaryIO


NOR_BYTES = 64 * 1024 * 1024
CANDIDATE_ERASE_BYTES = (4096, 65536)
QUALIFICATION_REQUIRED = (
    "controlled_ap_updater_and_host_control",
    "sony_read_write_matches_saved_raw_representation",
    "current_device_erase_geometry_and_flash_identity",
    "cp_init_and_mode_configuration_side_effects",
    "durable_erase_program_and_independent_readback_executor",
)


class NorPlanError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NorPlanError(message)


def swap16(data: bytes) -> bytes:
    """A candidate representation comparison, never selected automatically."""
    _require(len(data) % 2 == 0, "swap16 requires a complete even-byte range")
    result = bytearray(len(data))
    result[0::2], result[1::2] = data[1::2], data[0::2]
    return bytes(result)


def _plan_streams(current: BinaryIO, target: BinaryIO, *, capacity: int,
                  erase_bytes: int) -> dict:
    """Use all bytes of A as the target for each unit where A differs from B."""
    _require(type(capacity) is int and capacity > 0,
             "capacity must be a positive integer")
    _require(type(erase_bytes) is int and erase_bytes > 0
             and capacity % erase_bytes == 0, "erase units must exactly cover the image")
    old_hash, new_hash, replay = (hashlib.sha256() for _ in range(3))
    units = []
    changed_bytes = 0
    for offset in range(0, capacity, erase_bytes):
        before, after = current.read(erase_bytes), target.read(erase_bytes)
        _require(len(before) == len(after) == erase_bytes,
                 f"short raw NOR input at byte offset {offset}")
        old_hash.update(before)
        new_hash.update(after)
        if before == after:
            replay.update(before)
            continue
        count = sum(a != b for a, b in zip(before, after))
        changed_bytes += count
        units.append({
            "index": offset // erase_bytes,
            "byte_offset": offset,
            "bytes": erase_bytes,
            "changed_bytes": count,
            "unchanged_neighbor_bytes": erase_bytes - count,
            "current_sha256": hashlib.sha256(before).hexdigest(),
            "target_sha256": hashlib.sha256(after).hexdigest(),
            "target_source_byte_offset": offset,
        })
        # Complete A units include saved training/configuration and neighbors;
        # no current B bytes may silently replace them within a changed unit.
        replay.update(after)
    _require(current.read(1) == target.read(1) == b"",
             "raw NOR input exceeds the declared image extent")
    replay_digest = replay.hexdigest()
    _require(replay_digest == new_hash.hexdigest(), "complete NOR replay differs from target")
    return {
        "current_sha256": old_hash.hexdigest(),
        "target_sha256": new_hash.hexdigest(),
        "planned_replay_sha256": replay_digest,
        "replay_equals_complete_target": True,
        "changed_bytes": changed_bytes,
        "planned_erase_bytes": len(units) * erase_bytes,
        "planned_program_bytes": len(units) * erase_bytes,
        "unchanged_neighbor_bytes": len(units) * erase_bytes - changed_bytes,
        "erase_units": units,
    }


def _file_state(stat: os.stat_result) -> tuple:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def build_plan(current: Path, target: Path, *, erase_bytes: int) -> dict:
    """Produce a non-executable 64 MiB proposal; neither image is modified."""
    _require(type(erase_bytes) is int and erase_bytes in CANDIDATE_ERASE_BYTES,
             "candidate erase size must be 4096 or 65536 bytes")
    current, target = Path(current), Path(target)
    _require(current.is_file() and target.is_file(), "raw NOR inputs must be regular files")
    with current.open("rb") as before, target.open("rb") as after:
        initial = [_file_state(os.fstat(f.fileno())) for f in (before, after)]
        _require(all(row[2] == NOR_BYTES for row in initial),
                 "complete saved raw NOR inputs must each be exactly 64 MiB")
        result = _plan_streams(before, after, capacity=NOR_BYTES, erase_bytes=erase_bytes)
        final = [_file_state(os.fstat(f.fileno())) for f in (before, after)]
        _require(initial == final, "raw NOR input changed during offline planning")
        _require(final == [_file_state(p.stat()) for p in (current, target)],
                 "raw NOR input path changed during offline planning")
    return {
        "schema": "nor-restore-offline-proposal-v1",
        "status": "OFFLINE_PROPOSAL_ONLY",
        "camera_io": False,
        "can_execute": False,
        "qualification_required": list(QUALIFICATION_REQUIRED),
        "scope": "complete_saved_raw_nor_image",
        "capacity_bytes": NOR_BYTES,
        "capacity_basis": "existing_backup_contract_not_live_flash_probe",
        "current_image": str(current.resolve()),
        "target_image": str(target.resolve()),
        "candidate_geometry": {
            "erase_bytes": erase_bytes,
            "qualified": False,
            "basis": "Sony_script_units_not_current_device_geometry_proof",
        },
        "saved_representation": "david-dumpnor-bytes-v1",
        "writer_representation": "unqualified_identity_candidate",
        "representation_transform_applied": None,
        "raw_replay_must_not_use_encrypted_y": True,
        **result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--candidate-erase-bytes", type=int, choices=CANDIDATE_ERASE_BYTES,
                        required=True)
    args = parser.parse_args()
    print(json.dumps(build_plan(args.current, args.target,
                                erase_bytes=args.candidate_erase_bytes), indent=2))


if __name__ == "__main__":
    main()
