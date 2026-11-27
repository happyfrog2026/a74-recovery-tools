"""One-shot, scoped stock SD MODE5 trigger for verified ILCE-7M4 state."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct

from .stock_sd import (PROFILE, SETTINGS_SIZE,
                       STOCK_DAT_SHA256, file_sha256, inspect_sd_environment,
                       make_trigger)
from .transport import FastbootTransport
from .stock_profiles import inspect_package
from .updater_restore import (Fragment, OEM_SHA256, RestoreError, SDM2_SECTOR0_SHA256,
                              SECTOR, SIZE as UPDATER_SIZE, SPARSE_MAGIC, RAW, SKIP,
                              _chunk, _download_flash, _journal, replay, sha256)

REGION = "nflasha2"


def _verify_sdm2(data: bytes, *, camera_model: str = "ILCE-7M4"):
    if camera_model == "ILCE-7RM5":
        if len(data) != SECTOR or data[:4] != b"8246":
            raise RestoreError("nflasha SDM2 header invalid; expected 512 bytes and 8246 signature")
        for region, offset, expected_start, expected_size in (
                ("nflasha1", 32, 128, UPDATER_SIZE),
                ("nflasha2", 48, 49152, SETTINGS_SIZE)):
            start, count, kind, flags = struct.unpack_from("<4I", data, offset)
            if (start, count, kind, flags & 1) != (expected_start, expected_size // SECTOR, 5, 1):
                raise RestoreError(f"{region} SDM2 mapping mismatch: expected start={expected_start}, "
                                   f"bytes={expected_size}, type=5, enabled=1; actual start={start}, "
                                   f"bytes={count * SECTOR}, type={kind}, enabled={flags & 1}")
        return
    if len(data) != SECTOR or sha256(data) != SDM2_SECTOR0_SHA256:
        raise RestoreError("live SDM2 sector0 is not the reviewed same-camera mapping")
    start, count, kind, flags = struct.unpack_from("<4I", data, 48)
    if (start, count, kind, flags & 1) != (49152, SETTINGS_SIZE // SECTOR, 5, 1):
        raise RestoreError("live settings partition mapping changed")


def build(current: bytes, updater: bytes, dat_path: Path | None = None, *,
          expected_updater_sha256: str | None = None,
          camera_model: str = "ILCE-7M4") -> tuple[Fragment, bytes, dict]:
    expected_updater_sha256 = sha256(updater) if expected_updater_sha256 is None else expected_updater_sha256
    if len(current) != SETTINGS_SIZE or len(updater) != UPDATER_SIZE:
        raise RestoreError(f"partition size mismatch: nflasha2 expected={SETTINGS_SIZE} actual={len(current)}; "
                           f"nflasha1 expected={UPDATER_SIZE} actual={len(updater)}")
    if sha256(updater) != expected_updater_sha256:
        raise RestoreError("live updater differs from pinned OEM readback")
    package = ((inspect_package(dat_path, camera_model=camera_model)
                if camera_model == "ILCE-7RM5" else inspect_package(dat_path))
               if dat_path is not None else None)
    environment = (inspect_sd_environment(updater, PROFILE, camera_model=camera_model)
                   if camera_model == "ILCE-7RM5" else inspect_sd_environment(updater, PROFILE))
    candidate, plan = make_trigger(current)
    changed = tuple(i for i in range(SETTINGS_SIZE // SECTOR)
                    if current[i * SECTOR:(i + 1) * SECTOR] != candidate[i * SECTOR:(i + 1) * SECTOR])
    semantic_sectors = tuple(sorted({offset // SECTOR for offset in plan["changed_offsets"]}))
    if changed != semantic_sectors:
        raise RestoreError(f"unexpected trigger sectors or byte count: {changed}")
    chunks = []
    cursor = 0
    for sector in changed:
        if sector > cursor:
            chunks.append(_chunk(SKIP, sector - cursor))
        chunks.append(_chunk(RAW, 1, candidate[sector * SECTOR:(sector + 1) * SECTOR]))
        cursor = sector + 1
    if cursor < SETTINGS_SIZE // SECTOR:
        chunks.append(_chunk(SKIP, SETTINGS_SIZE // SECTOR - cursor))
    header = struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12, SECTOR,
                         SETTINGS_SIZE // SECTOR, len(chunks), 0)
    sparse = header + b"".join(chunks)
    fragment = Fragment(changed[0], len(changed), sparse) if changed else None
    if len(sparse) > 512 * 1024 or replay(current, [fragment] if fragment else []) != candidate:
        raise RestoreError("sparse trigger does not replay to exact full candidate")
    return fragment, candidate, {"settings_prewrite_sha256": sha256(current),
                                 "updater_sha256": sha256(updater),
                                 "host_dat_sha256": package["sha256"] if package else None,
                                 "dat_sha256": package["sha256"] if package else None,
                                 "target_package_version": package["version"] if package else None,
                                 "sd_slot_1_file_verified_by_host": False,
                                 "candidate_sha256": sha256(candidate),
                                 "candidate_bytes": len(candidate),
                                 "sparse_sha256": sha256(sparse),
                                 "sparse_bytes": len(sparse),
                                 "sparse_sectors": list(changed),
                                 "plan": plan, "updater_environment": environment,
                                 "replay_equals_candidate": True}


def apply(dat_path: Path, evidence_dir: Path, transport_factory=FastbootTransport) -> dict:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    receipt_path = evidence_dir / "trigger-receipt.json"
    receipt = {"started_utc": datetime.now(timezone.utc).isoformat(),
               "status": "PREWRITE_READ", "camera_storage_write_may_have_occurred": False,
               "reboot_may_have_occurred": False}
    _journal(receipt_path, receipt)
    try:
        with transport_factory() as transport:
            sector0 = evidence_dir / "sector0.prewrite.bin"
            transport.read_region("nflasha", sector0, length=SECTOR)
            sector0.chmod(0o600)
            _verify_sdm2(sector0.read_bytes())
            if transport.region_size("nflasha1") != UPDATER_SIZE or \
               transport.region_size(REGION) != SETTINGS_SIZE:
                raise RestoreError("live partition sizes changed")
            updater_path = evidence_dir / "updater.prewrite.bin"
            transport.read_region("nflasha1", updater_path)
            updater_path.chmod(0o600)
            settings_path = evidence_dir / "settings.prewrite.bin"
            transport.read_region(REGION, settings_path)
            settings_path.chmod(0o600)
            part, candidate, plan = build(settings_path.read_bytes(), updater_path.read_bytes(), Path(dat_path))
            if part is None:
                raise RestoreError("stock trigger already present; use the composite task for a confirmed reboot")
            receipt["plan"] = plan
            _journal(receipt_path, receipt)
            receipt["status"] = "FLASH_INFLIGHT"
            receipt["camera_storage_write_may_have_occurred"] = True
            _journal(receipt_path, receipt)
            _download_flash(transport, part, REGION)
            receipt["status"] = "READBACK_STARTED"
            _journal(receipt_path, receipt)
            readback = evidence_dir / "settings.readback.bin"
            transport.read_region(REGION, readback)
            readback.chmod(0o600)
            if readback.read_bytes() != candidate:
                raise RestoreError("full settings readback differs from exact candidate")
            receipt["status"] = "READBACK_VERIFIED"
            receipt["readback_sha256"] = sha256(candidate)
            _journal(receipt_path, receipt)
            receipt["status"] = "REBOOT_INFLIGHT"
            receipt["reboot_may_have_occurred"] = True
            _journal(receipt_path, receipt)
            transport._send("reboot")
            transport._reply()
            receipt["status"] = "REBOOT_ACKNOWLEDGED"
            receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
            _journal(receipt_path, receipt)
    except BaseException as exc:
        if receipt["status"] != "REBOOT_ACKNOWLEDGED":
            receipt["status"] = ("UNKNOWN_AFTER_WRITE" if receipt["camera_storage_write_may_have_occurred"]
                                 else "NO_WRITE")
            receipt["error"] = str(exc)
            _journal(receipt_path, receipt)
        raise
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verified stock SD trigger; apply writes settings and reboots once")
    sub = parser.add_subparsers(dest="action", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("settings", type=Path)
    plan.add_argument("updater", type=Path)
    plan.add_argument("dat", type=Path)
    execute = sub.add_parser("apply")
    execute.add_argument("dat", type=Path)
    execute.add_argument("evidence_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            _, _, result = build(args.settings.read_bytes(), args.updater.read_bytes(), args.dat)
        else:
            result = apply(args.dat, args.evidence_dir)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        parser.exit(1, f"stock-trigger: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
