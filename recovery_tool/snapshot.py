"""Private, resumable double-read raw snapshot of the four RAM Fastboot regions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4

from .bridge import RamPayloadNotReady
from .transport import (FastbootTransport, FastbootError, FastbootEnumerationUnavailable,
                        FastbootIdentityError, USBReadError,
                        USBCloseError, DestinationIOError, PRODUCT)

REGIONS = {
    "nflashaB0": 4 * 1024 * 1024,
    "nflashaB1": 4 * 1024 * 1024,
    "nflasha": 1_914_699_776,
    "cp-nor": 64 * 1024 * 1024,
}
RESTORE_CURRENT_PROFILE = "restore-current-no-cp-nor-v1"
EMMC_RECAPTURE_PROFILE = "emmc-region-recapture-v1"
REGIONAL_RECAPTURE_PROFILE = "raw-region-recapture-v1"
RESTORE_CURRENT_REGIONS = {name: size for name, size in REGIONS.items() if name != "cp-nor"}


def captured_regions(receipt: dict) -> dict[str, int]:
    """Derive the exact recorded region set; never infer a missing NOR file."""
    if "capture_regions" in receipt:
        regions = receipt["capture_regions"]
        if (not isinstance(regions, dict) or set(regions) - set(REGIONS) or
                any(type(size) is not int or size <= 0 or size % 512 for size in regions.values())):
            raise ValueError("invalid recorded capture regions")
        return dict(regions)
    profile = receipt.get("capture_profile")
    omitted = receipt.get("not_captured_regions")
    if profile is None and omitted is None:
        return REGIONS
    if profile == RESTORE_CURRENT_PROFILE and omitted == ["cp-nor"]:
        return RESTORE_CURRENT_REGIONS
    raise ValueError("invalid snapshot capture profile or omitted regions")
CHUNK_BYTES = {"cp-nor": 8 * 1024 * 1024}
DEFAULT_CHUNK_BYTES = 64 * 1024 * 1024
NONTRANSIENT_USB_ERRNOS = {errno.EACCES, errno.EINVAL, errno.ENOTSUP}
NONTRANSIENT_LIBUSB_CODES = {-3, -2, -12}  # access, invalid parameter, unsupported
MAX_USB_RETRIES = 5
WINDOWS_BACKUP_USB_RETRIES = 3


def is_transient_camera_usb(exc: USBReadError) -> bool:
    if getattr(exc, "usb_cleanup_dispose_failed", False):
        return False
    if exc.errno in NONTRANSIENT_USB_ERRNOS or exc.backend_error_code in NONTRANSIENT_LIBUSB_CODES:
        return False
    return (exc.endpoint_missing_during_transfer or
            exc.errno in {errno.EIO, errno.ENODEV, errno.ETIMEDOUT} or
            (exc.backend_error_type in {"USBError", "USBTimeoutError"} and
             exc.backend_error_code in {-1, -4, -7, -99}))


class BackupAnchorMismatch(RuntimeError):
    """The resumed reader returned different content for a saved anchor."""

    def __init__(self, anchor: dict, live_sha256: str):
        self.evidence = {
            "region": anchor["region"], "offset": anchor["offset"], "bytes": anchor["bytes"],
            "saved_sha256": anchor["sha256"], "live_sha256": live_sha256,
        }
        super().__init__(f"backup checkpoint content changed: {anchor['region']} at {anchor['offset']} ({anchor['bytes']} bytes)")


class CaptureFailure(RuntimeError):
    def __init__(self, *, origin: str, region: str, attempt: str, offset: int,
                 path: Path, cause: Exception, fastboot_state: str | None = None):
        observation = {
            "departed": "Windows confirmed Fastboot departed; checkpoint retained",
            "unknown": "Windows could not confirm Fastboot presence; checkpoint retained",
        }.get(fastboot_state)
        detail = f"{cause} ({observation})" if observation else str(cause)
        super().__init__(f"{origin} during {region} {attempt} at offset {offset} to {path}: {detail}")
        self.details = {"origin": origin, "region": region, "pass": attempt,
                        "offset": offset, "path": str(path), "error": str(cause)}
        if isinstance(cause, BackupAnchorMismatch):
            self.details["anchor_mismatch"] = cause.evidence
        if fastboot_state is not None:
            self.details["fastboot_pnp_state"] = fastboot_state
        if isinstance(cause, USBReadError):
            self.details.update({"usb_stage": cause.stage,
                                 "usb_errno": cause.errno,
                                 "usb_backend_error_type": cause.backend_error_type,
                                 "usb_backend_error_code": cause.backend_error_code})


def resume_anchors(folder: Path, receipt: dict, *, only_region: str | None = None) -> list[dict]:
    """Validate available double-read anchors; content continuity is not body identity."""
    folder = Path(folder).resolve()
    anchors = []
    for region, size in captured_regions(receipt).items():
        if only_region is not None and region != only_region:
            continue
        reads = receipt.get("reads", {}).get(region, {})
        first, second = reads.get("first"), reads.get("second")
        if first is not None or second is not None:
            if not first or not second:
                # A whole-file hash may be committed between passes; segment
                # pairs below still provide complete double-read evidence.
                first = second = None
            elif (first.get("bytes") != size or second.get("bytes") != size or
                  first.get("sha256") != second.get("sha256")):
                raise RuntimeError(f"saved double-read anchor differs: {region}")
        if first and second and (region != "nflasha" or size <= DEFAULT_CHUNK_BYTES):
            for item, suffix in ((first, "raw"), (second, "repeat")):
                path = folder / f"{region}.{suffix}.bin"
                if (item.get("file") != path.name or path.is_symlink() or
                        not path.is_file() or path.stat().st_size != size or
                        sha256(path) != item.get("sha256")):
                    raise RuntimeError(f"saved double-read anchor changed: {region}")
            anchors.append({"region": region, "offset": 0, "bytes": size,
                            "sha256": first["sha256"]})
            continue
        segments = receipt.get("segments", {}).get(region, {})
        pairs = {row["offset"]: row for row in segments.get("second", [])}
        for item in sorted(segments.get("first", []), key=lambda row: row["offset"]):
            repeat = pairs.get(item["offset"])
            if repeat is None:
                continue
            offset, length = item.get("offset"), item.get("bytes")
            if (type(offset) is not int or type(length) is not int or offset < 0 or
                    length <= 0 or offset % 512 or length % 512 or offset + length > size or
                    repeat.get("bytes") != length or repeat.get("sha256") != item.get("sha256")):
                raise RuntimeError(f"saved double-read segment anchor differs: {region}")
            for row in (item, repeat):
                path = folder / row["file"]
                if (path.is_symlink() or not path.resolve().is_relative_to(folder) or
                        not path.is_file() or path.stat().st_size != length or
                        sha256(path) != row.get("sha256")):
                    raise RuntimeError(f"saved double-read segment anchor changed: {region}")
            anchors.append({"region": region, "offset": offset, "bytes": length,
                            "sha256": item["sha256"]})
            break
    if not anchors:
        raise RuntimeError("no saved double-read anchor is available to confirm capture continuity; saved data retained")
    return anchors


def _verify_resume_identity(transport, folder: Path, receipt: dict) -> None:
    """Compare strongest available saved double-read anchors with the live reader."""
    anchors = resume_anchors(folder, receipt)
    if transport.verify_product() != PRODUCT:
        raise RuntimeError("camera product changed during backup retry")
    with tempfile.TemporaryDirectory(dir=folder, prefix="identity-check-") as directory:
        for index, item in enumerate(anchors):
            path = Path(directory) / f"anchor-{index}.bin"
            transport.read_region(item["region"], path, offset=item["offset"], length=item["bytes"])
            live_sha256 = sha256(path)
            if live_sha256 != item["sha256"]:
                raise BackupAnchorMismatch(item, live_sha256)


def sha256(path: Path, *, idle_hook=None, on_bytes=None) -> str:
    with path.open("rb") as stream:
        sha = hashlib.sha256()
        while chunk := stream.read(8 * 1024 * 1024):
            sha.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
            if idle_hook is not None:
                idle_hook()
        return sha.hexdigest()


def saved_emmc_drift(folder: Path) -> dict | None:
    """Return documented eMMC drift from an incomplete backup's local receipt."""
    path = Path(folder) / "capture-receipt.json"
    if not path.is_file():
        return None
    receipt = json.loads(path.read_text())
    mismatch = (receipt.get("failure") or {}).get("anchor_mismatch") or {}
    if receipt.get("status") == "INCOMPLETE" and mismatch.get("region") == "nflasha":
        return mismatch
    return None


def save(path: Path, data: dict):
    pending = path.with_suffix(".tmp")
    with pending.open("w") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.chmod(0o600)
    pending.replace(path)


def _preserve_partial(path: Path, receipt: dict):
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        archived = path.with_name(path.name + f".interrupted-{uuid4().hex}")
        partial.replace(archived)
        receipt.setdefault("interrupted_files", []).append(
            {"file": str(archived), "bytes": archived.stat().st_size,
             "preserved_utc": datetime.now(timezone.utc).isoformat()})


def _record_backup_retry(receipt, receipt_path, retry_state, progress, sleep, *,
                         region, attempt, offset, error, reentry):
    retry_state["used"] += 1
    receipt["retry_consecutive_used"] = retry_state["used"]
    limit = retry_state["limit"]
    events = receipt.setdefault("retry_events", [])
    events.append({
        "region": region, "pass": attempt, "offset": offset,
        "retry": retry_state["used"], "max_retries": limit,
        "total_retries": len(events) + 1, "reason": str(error),
        "reentry_requested": reentry,
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
    })
    save(receipt_path, receipt)
    if progress:
        progress({"stage": "backup_retry", "message":
                  f"Retrying {region} {attempt} "
                  f"at {offset} (retry {retry_state['used']}/{limit}): {error}",
                  "retry": retry_state["used"], "max_retries": limit,
                  "total_retries": len(events), "reentry_requested": reentry})
    sleep(0.5)


def _windows_retry_route(exc, probe, *, managed_backup, reentry_factory):
    """Return the observed state and the next route for recoverable Windows failures."""
    transient_usb = isinstance(exc, USBReadError) and (
        is_transient_camera_usb(exc) or
        (exc.stage in {"usb_bulk_read", "usb_partition_size", "usb_product", "usb_open"} and
         exc.backend_error_type == "USBError" and exc.backend_error_code == -9 and
         exc.errno not in NONTRANSIENT_USB_ERRNOS and
         not getattr(exc, "usb_cleanup_dispose_failed", False)))
    if not (isinstance(exc, FastbootEnumerationUnavailable) or transient_usb or
            (managed_backup and isinstance(exc, RamPayloadNotReady))):
        return None, None
    state = probe()
    if state == "departed" and reentry_factory is not None:
        return state, "reentry"
    if state == "present" or (state == "unknown" and managed_backup):
        return state, "direct"
    return state, None


def _read_region_size(transport, region):
    try:
        return transport.region_size(region)
    except OSError as exc:
        raise USBReadError(f"camera USB partition-size query failed for {region}: {exc}",
                           exc, stage="usb_partition_size") from exc


def _verify_backup_reconnect(transport, folder, receipt, progress, *, windows, reentry=False,
                             managed_backup=False):
    if managed_backup:
        try:
            product = transport.verify_product()
        except OSError as exc:
            raise USBReadError(f"camera USB product check interrupted: {exc}", exc,
                               stage="usb_product") from exc
        if product != PRODUCT:
            raise FastbootIdentityError("unexpected camera product on backup reconnect")
        if progress:
            progress({"stage": "backup_fastboot_direct", "message":
                      "Fastboot ready; continuing from the first missing backup read"})
        return
    if windows and progress:
        progress({"stage": "backup_fastboot_direct", "message":
                  ("Reopened RAM Fastboot; checking saved and live backup anchors" if reentry
                   else "Opened existing Fastboot; checking saved and live backup anchors")})
    _verify_resume_identity(transport, folder, receipt)
    if windows and progress:
        progress({"stage": "backup_anchor_verified", "message":
                  "Live backup anchors match the saved checkpoint"})


def _capture_pass(folder: Path, region: str, attempt: str, output: Path,
                  size: int, receipt: dict, receipt_path: Path, transport_factory,
                  *, selected_offset: int | None = None, finalize: bool = True,
                  auto_retry_usb: bool = False, progress=None, sleep=time.sleep,
                  retry_state: dict | None = None, reentry_factory=None,
                  idle_hook=None, backup_fastboot_state=None, managed_backup=False):
    _preserve_partial(output, receipt)
    if output.exists():
        if output.stat().st_size != size:
            raise RuntimeError(f"existing capture has wrong size: {output}")
        return
    parts = folder / "segments" / f"{region}.{attempt}"
    parts.mkdir(mode=0o700, parents=True, exist_ok=True)
    parts.chmod(0o700)
    segments = receipt.setdefault("segments", {}).setdefault(region, {}).setdefault(attempt, [])
    chunk_bytes = CHUNK_BYTES.get(region, DEFAULT_CHUNK_BYTES)
    offsets = list(range(0, size, chunk_bytes))
    segment_map = {item["offset"]: item for item in segments}
    if len(segment_map) != len(segments) or set(segment_map) - set(offsets):
        raise RuntimeError(f"invalid saved segment offsets: {region} {attempt}")
    for offset in ([selected_offset] if selected_offset is not None else offsets):
        if offset not in offsets:
            raise ValueError("segment offset outside region")
        length = min(chunk_bytes, size - offset)
        chunk = parts / f"{offset:010x}.bin"
        _preserve_partial(chunk, receipt)
        if not chunk.exists():
            retry = 0
            use_reentry = False
            while True:
                try:
                    factory = reentry_factory if use_reentry else transport_factory
                    try:
                        transport = factory()
                    except OSError as exc:
                        raise USBReadError(f"camera USB connection failed for {region}: {exc}", exc) from exc
                    with transport:
                        if retry:
                            _verify_backup_reconnect(transport, folder, receipt, progress,
                                                     windows=backup_fastboot_state is not None, reentry=use_reentry,
                                                     managed_backup=managed_backup)
                        live_size = _read_region_size(transport, region)
                        if live_size != size:
                            raise RuntimeError(f"live region size changed: {region}")
                        print(f"{region} {attempt}: {offset:,}/{size:,}", flush=True)
                        transport.read_region(region, chunk, offset=offset, length=length)
                    break
                except Exception as exc:
                    if (isinstance(exc, USBCloseError) and
                            not getattr(exc, "usb_cleanup_dispose_failed", False) and
                            chunk.is_file() and chunk.stat().st_size == length):
                        receipt.setdefault("transport_warnings", []).append({
                            "region": region, "pass": attempt, "offset": offset,
                            "warning": str(exc), "recorded_utc": datetime.now(timezone.utc).isoformat()})
                        save(receipt_path, receipt)
                        break  # Complete fsynced segment still receives its second read/hash check.
                    unavailable = isinstance(exc, FastbootEnumerationUnavailable)
                    transient = isinstance(exc, USBReadError) and is_transient_camera_usb(exc)
                    fastboot_state = None
                    windows = backup_fastboot_state is not None
                    budget = (auto_retry_usb and retry_state is not None and
                              retry_state["used"] < retry_state["limit"])
                    if windows:
                        route = None
                        if budget:
                            fastboot_state, route = _windows_retry_route(
                                exc, backup_fastboot_state, managed_backup=managed_backup,
                                reentry_factory=reentry_factory)
                        transient = route == "direct"
                        can_reenter = route == "reentry"
                    else:
                        can_reenter = unavailable and reentry_factory is not None
                    if not budget or not (transient or can_reenter or (unavailable and not windows)):
                        if auto_retry_usb:
                            origin = ("camera_usb_read" if isinstance(exc, (USBReadError, RamPayloadNotReady)) or unavailable else
                                      "destination_io" if isinstance(exc, (OSError, DestinationIOError)) else
                                      "safety_guard" if "automatic RAM reentry blocked" in str(exc) else
                                      "identity_or_protocol")
                            raise CaptureFailure(origin=origin, region=region, attempt=attempt,
                                                 offset=offset, path=chunk, cause=exc,
                                                 fastboot_state=fastboot_state) from exc
                        raise
                    _preserve_partial(chunk, receipt)
                    retry += 1
                    use_reentry = can_reenter
                    _record_backup_retry(
                        receipt, receipt_path, retry_state, progress, sleep, region=region,
                        attempt=attempt, offset=offset, error=exc, reentry=use_reentry)
            chunk.chmod(0o600)
        if chunk.stat().st_size != length:
            raise RuntimeError(f"existing segment has wrong size: {chunk}")
        digest = sha256(chunk, idle_hook=idle_hook)
        previous = segment_map.get(offset)
        expected_name = str(chunk.relative_to(folder))
        if previous and (previous["file"] != expected_name or
                         previous["bytes"] != length or previous["sha256"] != digest):
            raise RuntimeError(f"existing segment differs from receipt: {chunk}")
        if not previous:
            item = {"file": expected_name, "offset": offset,
                    "bytes": length, "sha256": digest,
                    "checked_utc": datetime.now(timezone.utc).isoformat()}
            segments.append(item)
            segment_map[offset] = item
            save(receipt_path, receipt)
    if not finalize:
        return
    if set(segment_map) != set(offsets):
        raise RuntimeError(f"missing capture segments: {region} {attempt}")
    partial = output.with_name(output.name + ".partial")
    with partial.open("xb") as dest:
        for offset in offsets:
            chunk = parts / f"{offset:010x}.bin"
            with chunk.open("rb") as source:
                while data := source.read(8 * 1024 * 1024):
                    dest.write(data)
                    if idle_hook is not None:
                        idle_hook()
        dest.flush()
        os.fsync(dest.fileno())
    if partial.stat().st_size != size:
        raise RuntimeError(f"assembled capture has wrong size: {partial}")
    partial.chmod(0o600)
    partial.replace(output)


def capture(folder: Path, *, transport_factory=None, auto_retry_usb: bool = False,
            progress=None, sleep=time.sleep, reentry_factory=None,
            profile: str = "full", idle_hook=None, backup_fastboot_state=None,
            require_all_regions: bool = False, only_region: str | None = None):
    regional = only_region is not None
    if profile not in ("full", "restore_current"):
        raise ValueError("unknown snapshot capture profile")
    if regional and (profile != "full" or only_region not in REGIONS or
                     auto_retry_usb or reentry_factory is not None):
        raise ValueError("regional recapture requires one exposed region and a single attach-only session")
    if regional:
        backup_fastboot_state = None
    regions = ({"full": REGIONS, "restore_current": RESTORE_CURRENT_REGIONS}[profile]
               if not regional else {only_region: REGIONS[only_region]})
    managed_backup = profile == "full" and backup_fastboot_state is not None and reentry_factory is not None
    transport_factory = transport_factory or FastbootTransport
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    manifest = folder / ("verified-region.json" if regional else "verified.json")
    receipt_path = folder / "capture-receipt.json"
    if regional and (manifest.exists() or receipt_path.exists()):
        raise FileExistsError("choose a fresh directory for a single-session regional recapture")
    verified = json.loads(manifest.read_text()) if manifest.exists() else {}
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {
        "started_utc": datetime.now(timezone.utc).isoformat(), "reads": {}, "status": "IN_PROGRESS"}
    expected_profile = (EMMC_RECAPTURE_PROFILE if only_region == "nflasha" else REGIONAL_RECAPTURE_PROFILE
                        ) if regional else RESTORE_CURRENT_PROFILE if profile == "restore_current" else None
    if receipt_path.exists() and receipt.get("capture_profile") != expected_profile:
        raise ValueError("saved snapshot capture profile differs")
    if set(verified) - set(regions):
        raise ValueError("saved snapshot contains regions outside capture profile")
    if regional:
        receipt["capture_profile"] = expected_profile
        receipt["not_captured_regions"] = sorted(set(REGIONS) - set(regions))
    if profile == "restore_current":
        if receipt_path.exists() and receipt.get("not_captured_regions") != ["cp-nor"]:
            raise ValueError("saved restore capture omission differs")
        receipt["capture_profile"] = RESTORE_CURRENT_PROFILE
        receipt["not_captured_regions"] = ["cp-nor"]
    if receipt.get("status") == "INCOMPLETE":
        mismatch = (receipt.get("failure") or {}).get("anchor_mismatch") or {}
        if (not managed_backup and profile == "full" and mismatch.get("region") == "nflasha"
                and "nflasha" in verified):
            raise RuntimeError("saved eMMC content changed; confirm the same camera and recapture into a new directory")
        receipt.setdefault("previous_failures", []).append({
            "recorded_utc": datetime.now(timezone.utc).isoformat(),
            "error": receipt.get("error"), "failure": receipt.get("failure")})
    receipt["status"] = "IN_PROGRESS"
    receipt.pop("error", None)
    receipt.pop("failure", None)
    save(receipt_path, receipt)
    retry_state = {
        "used": receipt.get("retry_consecutive_used", len(receipt.get("retry_events", []))),
        "limit": WINDOWS_BACKUP_USB_RETRIES if managed_backup else MAX_USB_RETRIES,
    }
    def idle_host():
        if idle_hook is not None:
            idle_hook(folder, receipt, receipt_path, retry_state)
    try:
        if "capture_regions" in receipt:
            captured_regions(receipt)  # Validate persisted sizes before trusting them.
        recorded_regions = receipt.setdefault("capture_regions", {})
        coverage = receipt.setdefault("region_coverage", {})
        receipt["physical_device_complete"] = False
        receipt["coverage_note"] = "Only RAM-reader exposed extents; physical eMMC capacity/RPMB are unavailable."
        for region, fallback_size in regions.items():
            expected_size = recorded_regions.get(region)
            if expected_size is None and region in verified:
                expected_size = verified[region]["bytes"]
                recorded_regions[region] = expected_size
            if expected_size is None:
                reopening = False
                use_reentry = False
                while True:
                    try:
                        factory = reentry_factory if use_reentry else transport_factory
                        with factory() as transport:
                            if reopening:
                                _verify_backup_reconnect(transport, folder, receipt, progress,
                                                         windows=True, reentry=use_reentry,
                                                         managed_backup=managed_backup)
                            expected_size = _read_region_size(transport, region)
                        if type(expected_size) is not int or expected_size <= 0 or expected_size % 512:
                            raise FastbootError("invalid reader extent")
                        break
                    except (FastbootError, BackupAnchorMismatch, RamPayloadNotReady) as exc:
                        state, route = None, None
                        if (auto_retry_usb and backup_fastboot_state is not None and
                                retry_state["used"] < retry_state["limit"] and
                                (verified or managed_backup)):
                            state, route = _windows_retry_route(
                                exc, backup_fastboot_state, managed_backup=managed_backup,
                                reentry_factory=reentry_factory if verified else None)
                        if route:
                            use_reentry = route == "reentry"
                            _record_backup_retry(
                                receipt, receipt_path, retry_state, progress, sleep, region=region,
                                attempt="region-size", offset=0, error=exc, reentry=use_reentry)
                            reopening = bool(verified)
                            continue
                        if isinstance(exc, (FastbootEnumerationUnavailable, BackupAnchorMismatch,
                                            USBReadError, RamPayloadNotReady)):
                            if not auto_retry_usb or backup_fastboot_state is None:
                                raise
                            raise CaptureFailure(
                                origin="checkpoint_mismatch" if isinstance(exc, BackupAnchorMismatch)
                                else "camera_usb_read", region=region, attempt="region-size",
                                offset=0, path=folder / f"{region}.raw.bin", cause=exc,
                                fastboot_state=state) from exc
                        if require_all_regions or reopening or isinstance(exc, FastbootIdentityError):
                            raise
                        expected_size = None
                        coverage[region] = {"status": "UNAVAILABLE", "error": str(exc)}
                        save(receipt_path, receipt)
                        break
                if expected_size is None:
                    continue
                recorded_regions[region] = expected_size
            coverage[region] = {"status": "IN_PROGRESS", "bytes": expected_size,
                                "extent_source": "payload-fixed-nor" if region == "cp-nor" else
                                "payload-fixed-boot" if region.startswith("nflashaB") else
                                "sdm2-highest-valid-end-lower-bound"}
            save(receipt_path, receipt)
            first = folder / f"{region}.raw.bin"
            second = folder / f"{region}.repeat.bin"
            chunk_bytes = CHUNK_BYTES.get(region, DEFAULT_CHUNK_BYTES)
            for offset in range(0, expected_size, chunk_bytes):
                old_segments = receipt.get("segments", {}).get(region, {})
                pair_preexisting = all(any(item["offset"] == offset
                                           for item in old_segments.get(attempt, []))
                                       for attempt in ("first", "second"))
                while True:
                    for attempt, path in (("first", first), ("second", second)):
                        _capture_pass(folder, region, attempt, path, expected_size,
                                      receipt, receipt_path, transport_factory,
                                      selected_offset=offset, finalize=False,
                                      auto_retry_usb=auto_retry_usb, progress=progress, sleep=sleep,
                                      retry_state=retry_state, reentry_factory=reentry_factory,
                                      idle_hook=idle_host, backup_fastboot_state=backup_fastboot_state,
                                      managed_backup=managed_backup)
                    first_part = next((item for item in receipt.get("segments", {}).get(region, {}).get("first", [])
                                       if item["offset"] == offset), None)
                    second_part = next((item for item in receipt.get("segments", {}).get(region, {}).get("second", [])
                                        if item["offset"] == offset), None)
                    if not first_part or not second_part or first_part["sha256"] == second_part["sha256"]:
                        break
                    disagreement = RuntimeError(f"two independent segment reads differ: {region} at {offset}")
                    if not managed_backup:
                        raise disagreement
                    if retry_state["used"] >= retry_state["limit"]:
                        raise CaptureFailure(origin="segment_mismatch", region=region, attempt="compare",
                                             offset=offset, path=folder, cause=disagreement) from disagreement
                    _record_backup_retry(
                        receipt, receipt_path, retry_state, progress, sleep, region=region,
                        attempt="compare", offset=offset, error=disagreement, reentry=False)
                    # Retire only the disputed pair. Every other committed segment
                    # stays in place and is checked locally during assembly.
                    archived = folder / "disputed-segments" / uuid4().hex
                    archived.mkdir(parents=True)
                    receipt["segments"][region]["first"].remove(first_part)
                    receipt["segments"][region]["second"].remove(second_part)
                    receipt.setdefault("disputed_segments", []).append(
                        {"region": region, "offset": offset, "reads": [first_part, second_part],
                         "archive": str(archived.relative_to(folder))})
                    save(receipt_path, receipt)
                    for attempt, row in (("first", first_part), ("second", second_part)):
                        (folder / row["file"]).rename(archived / f"{attempt}.bin")
                    pair_preexisting = False
                if (not pair_preexisting and first_part and second_part and
                        first_part["sha256"] == second_part["sha256"] and retry_state["used"]):
                    receipt.setdefault("retry_reset_events", []).append({
                        "region": region, "offset": offset,
                        "total_retries": len(receipt.get("retry_events", [])),
                        "verified_utc": datetime.now(timezone.utc).isoformat()})
                    retry_state["used"] = 0
                    receipt["retry_consecutive_used"] = 0
                    save(receipt_path, receipt)
                    if progress:
                        progress({"stage": "backup_retry_reset",
                                  "message": f"Verified matching double-read segment for {region} at {offset}; consecutive USB retry count reset",
                                  "retry": 0, "max_retries": retry_state["limit"],
                                  "total_retries": len(receipt.get("retry_events", []))})
            for attempt, path in (("first", first), ("second", second)):
                _capture_pass(folder, region, attempt, path, expected_size,
                              receipt, receipt_path, transport_factory,
                              auto_retry_usb=auto_retry_usb, progress=progress, sleep=sleep,
                              retry_state=retry_state, reentry_factory=reentry_factory,
                              idle_hook=idle_host)
                digest = sha256(path, idle_hook=idle_host)
                receipt["reads"].setdefault(region, {})[attempt] = {
                    "file": path.name, "bytes": expected_size, "sha256": digest,
                    "checked_utc": datetime.now(timezone.utc).isoformat()}
                save(receipt_path, receipt)
                print(f"{region} {attempt}: {digest}", flush=True)
            if receipt["reads"][region]["first"]["sha256"] != receipt["reads"][region]["second"]["sha256"]:
                raise RuntimeError(f"two independent reads differ: {region}")
            verified[region] = {"bytes": expected_size,
                                "sha256": receipt["reads"][region]["first"]["sha256"]}
            save(manifest, verified)
            coverage[region]["status"] = "VERIFIED"
        if (require_all_regions or regional) and set(verified) != set(regions):
            raise RuntimeError("not all requested raw regions were read twice")
        if not verified:
            raise RuntimeError("no readable regions captured")
        receipt["status"] = "VERIFIED"
        receipt.pop("error", None)
        receipt.pop("failure", None)
        receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save(receipt_path, receipt)
    except BaseException as exc:
        receipt["status"] = "INCOMPLETE"
        receipt["error"] = str(exc)
        for details in receipt.get("region_coverage", {}).values():
            if details.get("status") == "IN_PROGRESS":
                details.update(status="INCOMPLETE", error=str(exc))
        if isinstance(exc, CaptureFailure):
            receipt["failure"] = exc.details
        save(receipt_path, receipt)
        raise
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Double-read raw snapshot; read-only camera commands")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    capture(args.folder)


if __name__ == "__main__":
    main()
