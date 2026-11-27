"""Durable, sector-aware write transactions for reviewed Fastboot partitions.

Legacy plans use full-region old/target/live reconciliation and readback.
Explicit fragment-readback plans write only target-vs-capture differences,
check each written range immediately, and keep ambiguous transport outcomes
separate from complete readback mismatches. No route is widened implicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import os
import re
import sys
import time
import usb.core
from pathlib import Path
from uuid import uuid4

from .bridge import RamPayloadNotReady
from .resumable_region_read import read_full_region
from .scoped_restore_plan import _fragments, sparse_fragment
from .snapshot_compare import _fat_changes
from .transport import (FastbootTransport, FastbootError, FastbootEnumerationUnavailable,
                        FastbootIdentityError, USBReadError)

SECTOR = 512
SUPPORTED = {"nflasha1", "nflasha2", "nflasha10", "nflasha11"}
CAPABILITY_PROFILE = "verified-capability-snapshot-v1"
FRAGMENT_READBACK_MODE = "fragment-readback-v1"
CAPABILITY_REGIONS = {f"nflasha{slot}" for slot in
                      (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 18, 23)} | {"nflashaB0", "nflashaB1"}
TRANSIENT_CAMERA_ERRNOS = {errno.EIO, errno.ENODEV, errno.ETIMEDOUT}
TRANSIENT_LIBUSB_CODES = {-1, -4, -7, -99}  # I/O, no device, timeout, other.
MAX_RESTORE_RECONNECTS = 5


class TransactionError(RuntimeError):
    pass


class CameraWriteIOError(TransactionError):
    """USB failure during the exact flash command/response window."""

    def __init__(self, cause: OSError):
        super().__init__(f"camera USB flash interrupted: {cause}")
        self.errno = cause.errno
        self.backend_error_type = type(cause).__name__
        self.backend_error_code = getattr(cause, "backend_error_code", None)
        self.stage = "flash"


def require(ok: bool, message: str):
    if not ok:
        raise TransactionError(message)


def digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()






def _validate_capability_scope(regions: list[dict], identity: list[dict], metadata: dict,
                               reboot: bool, input_files: list[Path]) -> None:
    """Rebuild the entire verified comparison before admitting a generic route."""
    from . import scoped_restore_plan
    require(not reboot and metadata.get("capability_profile") == CAPABILITY_PROFILE,
            "invalid snapshot capability profile or reboot")
    require(metadata.get("validation_mode") == FRAGMENT_READBACK_MODE,
            "old restore task format is unsupported; select the saved snapshot for a new restore")
    current = Path(metadata.get("current_snapshot", "")).resolve()
    target = Path(metadata.get("target_snapshot", "")).resolve()
    plan = scoped_restore_plan.build_plan(current, target, profile="capability",
                                          validation_mode=metadata.get("validation_mode"))
    require(metadata.get("offline_plan") == plan and
            [row.get("name") for row in regions] ==
            [row["region"] for row in plan["partitions"]],
            "snapshot capability plan or region order differs")
    expected_files = {folder / name for folder in (current, target) for name in
                      ("verified.json", "capture-receipt.json", "decrypted/manifest.json")}
    require({Path(path).resolve() for path in input_files} == expected_files,
            "snapshot capability receipt pins differ")
    require(identity == scoped_restore_plan.capability_identity(current, target, plan),
            "snapshot capability identity pins differ")
    for region, row in zip(regions, plan["partitions"]):
        reviewed = _ranges({sector for fragment in row["fragments"]
                            for sector in range(fragment["start_sector"],
                                                fragment["start_sector"] + fragment["sectors"])})
        reviewed_name = (re.fullmatch(r"nflasha[1-9][0-9]*", region["name"]) is not None or
                         region["name"] in {"nflashaB0", "nflashaB1"})
        require(reviewed_name and
                Path(region["original"]).resolve() == Path(row["current_image"]).resolve() and
                Path(region["target"]).resolve() == Path(row["target_image"]).resolve() and
                region["original_sha256"] == row["current_sha256"] and
                region["target_sha256"] == row["target_sha256"] and
                region["bytes"] == row["sectors"] * SECTOR and
                region["scope"] == reviewed and
                not region.get("approved_drift") and not region.get("approved_drift_paths"),
                f"unreviewed snapshot capability region: {region['name']}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(progress, stage: str, message: str, *, fraction=None, status=None, **details):
    if progress is None:
        return
    event = {"stage": stage, "message": message}
    if fraction is not None:
        event["fraction"] = fraction
    if status is not None:
        event["status"] = status
    event.update(details)
    try:
        progress(event)
    except Exception:
        pass  # UI telemetry cannot alter a device transaction.


def durable_json(path: Path, value: dict):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.chmod(0o600)
    from .durable_file import durable_replace
    durable_replace(temp, path)


def _ranges(indices: set[int]) -> list[list[int]]:
    output = []
    start = end = None
    for index in sorted(indices):
        if start is None:
            start = end = index
        elif index == end + 1:
            end = index
        else:
            output.append([start, end + 1])
            start = end = index
    if start is not None:
        output.append([start, end + 1])
    return output


def _compare(original: Path, target: Path, live: Path,
             approved_drift: set[int]) -> tuple[set[int], set[int], set[int]]:
    size = Path(original).stat().st_size
    require(size == Path(target).stat().st_size == Path(live).stat().st_size and size % SECTOR == 0,
            "partition image sizes differ")
    changed, pending, unexpected = set(), set(), set()
    with Path(original).open("rb") as a, Path(target).open("rb") as b, Path(live).open("rb") as c:
        sector = 0
        while sector < size // SECTOR:
            before, after, actual = a.read(SECTOR), b.read(SECTOR), c.read(SECTOR)
            if before != after:
                changed.add(sector)
            if actual == after:
                pass
            elif actual == before or sector in approved_drift:
                pending.add(sector)
            else:
                unexpected.add(sector)
            sector += 1
    return changed, pending, unexpected


def prepare(job_dir: Path, *, kind: str, regions: list[dict],
            identity: list[dict], input_files: list[Path], reboot: bool = False,
            metadata: dict | None = None) -> dict:
    """Create a private immutable plan and mutable journal. No device access."""
    job_dir = Path(job_dir).resolve()
    require(not (job_dir / "manifest.json").exists() and
            not (job_dir / "journal.json").exists(), "transaction already prepared")
    require(bool(regions), "transaction has no regions")
    capability_restore = kind == "scoped_restore"
    if capability_restore:
        require((metadata or {}).get("capability_profile") == CAPABILITY_PROFILE and
                (metadata or {}).get("validation_mode") == FRAGMENT_READBACK_MODE,
                "old restore task format is unsupported; select the saved snapshot for a new restore")
    job_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    job_dir.chmod(0o700)
    seen = set()
    prepared = []
    for region in regions:
        name = region["name"]
        allowed = CAPABILITY_REGIONS if capability_restore else SUPPORTED
        fragment_capability = capability_restore and (metadata or {}).get("validation_mode") == FRAGMENT_READBACK_MODE
        dynamic_capability_name = (re.fullmatch(r"nflasha[1-9][0-9]*", name) is not None or
                                   name in {"nflashaB0", "nflashaB1"})
        require((dynamic_capability_name if fragment_capability else name in allowed) and
                name not in seen, f"unsupported or duplicate write region: {name}")
        seen.add(name)
        original, target = Path(region["original"]).resolve(), Path(region["target"]).resolve()
        require(original.is_file() and target.is_file() and original.stat().st_size == target.stat().st_size,
                f"missing or resized partition image: {name}")
        require(original.stat().st_size % SECTOR == 0, "unaligned partition image")
        changed, _, _ = _compare(original, target, original, set())
        zero_scope_stock_reboot = (kind == "stock_sd_recovery" and reboot and
                                   name == "nflasha2" and
                                   bool((metadata or {}).get("preexisting_trigger_verified")))
        require(changed or zero_scope_stock_reboot,
                f"no changed sectors in {name}")
        scope = _ranges(changed)
        prepared.append({"name": name, "original": str(original), "target": str(target),
                         "bytes": original.stat().st_size, "original_sha256": digest(original),
                         "target_sha256": digest(target), "scope": scope,
                         "approved_drift": sorted(set(region.get("approved_drift", []))),
                         "approved_drift_paths": sorted(set(region.get("approved_drift_paths", [])))})
    pins = [{"path": str(Path(file).resolve()), "sha256": digest(Path(file))}
            for file in input_files]
    if capability_restore:
        _validate_capability_scope(prepared, identity, metadata or {}, reboot, input_files)
    manifest = {"schema": "write-transaction-v1", "kind": kind, "created_utc": now(),
                "regions": prepared, "identity": identity, "input_files": pins,
                "reboot": reboot, "metadata": metadata or {}}
    durable_json(job_dir / "manifest.json", manifest)
    journal = {"schema": "write-transaction-state-v1", "status": "READY",
               "created_utc": now(), "epoch": 0, "write_may_have_occurred": False,
               "reboot_may_have_occurred": False, "confirmed_fragments": 0}
    durable_json(job_dir / "journal.json", journal)
    return manifest


def load(job_dir: Path, *, progress=None, cancelled=None) -> tuple[dict, dict]:
    job_dir = Path(job_dir)
    manifest = json.loads((job_dir / "manifest.json").read_text())
    state = json.loads((job_dir / "journal.json").read_text())
    require(manifest.get("schema") == "write-transaction-v1" and
            state.get("schema") == "write-transaction-state-v1", "unsupported transaction schema")
    if manifest.get("kind") == "scoped_restore":
        require(manifest.get("metadata", {}).get("capability_profile") == CAPABILITY_PROFILE and
                manifest.get("metadata", {}).get("validation_mode") == FRAGMENT_READBACK_MODE,
                "old restore task format is unsupported; select the saved snapshot for a new restore")
        _validate_capability_scope(manifest.get("regions", []), manifest.get("identity", []),
                                   manifest.get("metadata", {}), manifest.get("reboot"),
                                   [Path(item["path"]) for item in manifest.get("input_files", [])])
    files = [(f"input-{index + 1}", Path(pin["path"]), pin["sha256"], None)
             for index, pin in enumerate(manifest["input_files"])]
    files += [(f"{region['name']}.{key}", Path(region[key]), region[hash_key], region["bytes"])
              for region in manifest["regions"]
              for key, hash_key in (("original", "original_sha256"), ("target", "target_sha256"))]
    total = sum(path.stat().st_size for _, path, _, _ in files)
    done = 0
    started = time.monotonic()
    def report(phase, label):
        if progress is not None:
            try:
                progress({"stage": "restore_local_verify", "phase": phase,
                          "file": label, "files": len(files), "bytes_done": done,
                          "bytes_total": total,
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "message": f"Verifying saved restore files: {label} ({done}/{total} bytes)"})
            except Exception:
                pass
    report("start", "saved inputs")
    for label, path, expected_sha, expected_size in files:
        size = path.stat().st_size
        require(expected_size is None or size == expected_size, f"pinned partition image changed: {path}")
        report("file_start", label)
        sha = hashlib.sha256()
        next_report = 64 * 1024 * 1024
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError("local restore pin verification cancelled")
                sha.update(chunk)
                done += len(chunk)
                if done >= next_report:
                    report("progress", label)
                    next_report = done + 64 * 1024 * 1024
        require(sha.hexdigest() == expected_sha,
                f"pinned {'partition image' if expected_size is not None else 'input'} changed: {path}")
        report("file_done", label)
    report("done", "saved inputs")
    return manifest, state


def _write_response(transport: FastbootTransport, name: str, payload: bytes, sectors: int,
                    *, capability_restore: bool = False,
                    fragment_mode: bool = False):
    allowed = CAPABILITY_REGIONS if capability_restore else SUPPORTED
    dynamic_name = (re.fullmatch(r"nflasha[1-9][0-9]*", name) is not None or
                    name in {"nflashaB0", "nflashaB1"})
    require((dynamic_name if fragment_mode else name in allowed) and
            0 < len(payload) <= 512 * 1024, "unsupported write route/size")
    transport._send(f"download:{len(payload):08x}")
    _, length = transport._reply((b"DATA",))
    require(length == f"{len(payload):08x}".encode(), "download size response differs")
    for offset in range(0, len(payload), 65536):
        chunk = payload[offset:offset + 65536]
        require(transport.backend.write(chunk, 3500) == len(chunk), "short download write")
    transport._reply()
    transport._send(f"flash:{name}")
    expected = f"wrote {sectors} sectors to {name}".encode()
    count_seen = False
    for _ in range(128):
        response = transport.backend.read(64, 3500)
        require(4 <= len(response) <= 64, "invalid flash response")
        tag, value = response[:4], response[4:]
        if tag == b"INFO":
            count_seen |= value == expected
            continue
        if tag == b"FAIL":
            raise TransactionError("flash rejected: " + value.decode("utf-8", "replace"))
        require(tag == b"OKAY" and count_seen, "flash did not confirm sector count")
        return
    raise TransactionError("flash response missing completion")


def _identity(transport: FastbootTransport, identity: list[dict], epoch_dir: Path):
    identity_dir = epoch_dir / f"identity-check-{uuid4().hex}"
    identity_dir.mkdir(mode=0o700)
    for item in identity:
        path = identity_dir / f"identity-{item['region']}-{item.get('offset',0)}.bin"
        transport.read_region(item["region"], path,
                              offset=item.get("offset", 0), length=item["bytes"])
        require(digest(path) == item["sha256"], f"device identity pin differs: {item['region']}")


def _plan_live_region(region: dict, live: Path, state: dict) -> dict:
    """Accept only old/target sectors and reviewed drift in this full live read."""
    name = region["name"]
    original, target = Path(region["original"]), Path(region["target"])
    drift = set(region["approved_drift"])
    changed, pending, unexpected = _compare(original, target, live, drift)
    reviewed = {n for start, end in region["scope"] for n in range(start, end)}
    require(changed == reviewed, f"reviewed scope changed: {name}")
    require(not unexpected, f"unrecognized live sectors in {name}: {len(unexpected)}")
    if drift and region["approved_drift_paths"]:
        actual_drift = set()
        with original.open("rb") as a, target.open("rb") as b, live.open("rb") as c:
            for sector in drift:
                a.seek(sector*SECTOR); b.seek(sector*SECTOR); c.seek(sector*SECTOR)
                old, new, value = a.read(SECTOR), b.read(SECTOR), c.read(SECTOR)
                if value not in (old, new):
                    actual_drift.add(sector)
        if actual_drift and not state["write_may_have_occurred"]:
            fat = _fat_changes(original, live)
            require(fat.get("status") == "compared" and
                    set(fat.get("changed_paths", [])) <= set(region["approved_drift_paths"]),
                    f"unreviewed FAT path drift: {name}")
    fragments, replay = _fragments(live, target, region["bytes"]//SECTOR, _ranges(pending))
    require(replay == region["target_sha256"], f"fresh replay differs from target: {name}")
    return {"name": name, "live_file": str(live), "live_sha256": digest(live),
            "pending_sectors": len(pending), "already_target_sectors": len(changed - pending),
            "fragments": fragments}


def _fragment_mode(manifest: dict) -> bool:
    return (manifest.get("kind") == "scoped_restore" and
            manifest.get("metadata", {}).get("capability_profile") == CAPABILITY_PROFILE and
            manifest.get("metadata", {}).get("validation_mode") == FRAGMENT_READBACK_MODE)


def _planned_fragments(manifest: dict) -> list[tuple[dict, int, dict, str]]:
    """Use the offline plan already revalidated by load, without another full hash."""
    rows = manifest.get("metadata", {}).get("offline_plan", {}).get("partitions", [])
    require(isinstance(rows, list) and len(rows) == len(manifest["regions"]),
            "saved fragment plan region count differs")
    result = []
    for spec, row in zip(manifest["regions"], rows):
        require(row.get("region") == spec["name"] and
                row.get("current_sha256") == spec["original_sha256"] and
                row.get("target_sha256") == spec["target_sha256"],
                f"saved fragment plan image pin differs: {spec['name']}")
        cursor = -1
        fragments = row.get("fragments")
        require(isinstance(fragments, list) and fragments, "saved fragment list is empty")
        for index, fragment in enumerate(fragments):
            start, sectors = fragment.get("start_sector"), fragment.get("sectors")
            require(type(start) is int and type(sectors) is int and
                    0 <= start and 0 < sectors <= (512 * 1024 - 64) // SECTOR and
                    start > cursor and start + sectors <= spec["bytes"] // SECTOR and
                    type(fragment.get("bytes")) is int and fragment["bytes"] <= 512 * 1024 and
                    isinstance(fragment.get("sha256"), str) and len(fragment["sha256"]) == 64 and
                    isinstance(fragment.get("target_sha256"), str) and
                    len(fragment["target_sha256"]) == 64,
                    f"saved fragment geometry or digest invalid: {spec['name']} {index}")
            cursor = start + sectors - 1
            result.append((spec, index, fragment, fragment["target_sha256"]))
    return result


def verify_fragment_results(job_dir: Path, manifest: dict, state: dict) -> dict:
    """Validate the durable ordered prefix of immediate fragment readbacks."""
    require(_fragment_mode(manifest), "not a fragment-readback transaction")
    job_dir = Path(job_dir).resolve()
    planned = _planned_fragments(manifest)
    rows = state.get("fragment_results", [])
    require(isinstance(rows, list) and len(rows) <= len(planned),
            "saved fragment result count invalid")
    directory = job_dir / "fragment-readbacks"
    require(not directory.is_symlink(), "fragment readback directory is a link")
    verified = failed = 0
    for position, (row, (spec, index, fragment, expected)) in enumerate(zip(rows, planned)):
        require(isinstance(row, dict) and row.get("region") == spec["name"] and
                row.get("index") == index and row.get("start_sector") == fragment["start_sector"] and
                row.get("sectors") == fragment["sectors"] and
                row.get("target_sha256") == expected and
                type(row.get("attempts")) is int and 1 <= row["attempts"] <= 2 and
                row.get("status") in ("verified", "failed") and
                type(row.get("epoch")) is int and 1 <= row["epoch"] <= state.get("epoch", 0),
                f"saved fragment result differs at {position}")
        relative = row.get("readback_path")
        require(isinstance(relative, str) and relative and
                Path(relative).parts[:1] == ("fragment-readbacks",) and
                ".." not in Path(relative).parts and not Path(relative).is_absolute(),
                "fragment readback path invalid")
        path = job_dir / relative
        require(path.parent.resolve() == directory.resolve() and not path.is_symlink() and
                path.is_file() and path.stat().st_size == fragment["sectors"] * SECTOR and
                digest(path) == row.get("readback_sha256"),
                f"saved fragment readback missing or changed at {position}")
        observations = row.get("attempt_readbacks")
        require(isinstance(observations, list) and 1 <= len(observations) <= 2 and
                observations[-1] == {"attempt": row["attempts"],
                                     "readback_path": relative,
                                     "readback_sha256": row["readback_sha256"]},
                f"saved fragment attempt evidence differs at {position}")
        for expected_attempt, prior in enumerate(observations, 1):
            require(isinstance(prior, dict) and type(prior.get("attempt")) is int and
                    prior["attempt"] == expected_attempt and
                    isinstance(prior.get("readback_path"), str) and
                    Path(prior["readback_path"]).parts[:1] == ("fragment-readbacks",) and
                    ".." not in Path(prior["readback_path"]).parts,
                    f"saved fragment attempt malformed at {position}")
            prior_path = job_dir / prior["readback_path"]
            require(prior_path.parent.resolve() == directory.resolve() and
                    not prior_path.is_symlink() and prior_path.is_file() and
                    prior_path.stat().st_size == fragment["sectors"] * SECTOR and
                    digest(prior_path) == prior.get("readback_sha256"),
                    f"saved fragment attempt changed at {position}")
        matches = row["readback_sha256"] == expected
        require(matches == (row["status"] == "verified") and
                (row["status"] != "failed" or row["attempts"] == 2),
                f"saved fragment outcome differs at {position}")
        verified += matches
        failed += not matches
    require(state.get("confirmed_fragments", 0) == verified,
            "saved verified fragment count differs")
    if state.get("status") in ("VERIFIED", "PARTIAL"):
        require(len(rows) == len(planned) and not state.get("inflight") and
                (state["status"] == "VERIFIED") == (failed == 0),
                "saved terminal fragment result sequence incomplete")
    return {"total": len(planned), "completed": len(rows), "verified": verified,
            "failed": failed}


def _fragment_boundary(job_dir: Path, manifest: dict, state: dict) -> bool:
    if not _fragment_mode(manifest) or state.get("inflight") or state.get("reboot_may_have_occurred"):
        return False
    try:
        summary = verify_fragment_results(job_dir, manifest, state)
        return summary["completed"] > 0 and summary["completed"] == summary["verified"] + summary["failed"]
    except (TransactionError, OSError, KeyError, TypeError, ValueError):
        return False


def _validate_fragment_inflight(job_dir: Path, manifest: dict, state: dict) -> None:
    """Pin the one uncertain write before a guarded RAM reset or readback."""
    require(_fragment_mode(manifest) and state.get("write_may_have_occurred") is True and
            state.get("reboot_may_have_occurred") is False and
            state.get("status") in {"WRITE_INFLIGHT", "UNKNOWN_AFTER_WRITE"},
            "fragment reentry state is not an uncertain write")
    summary = verify_fragment_results(job_dir, manifest, state)
    planned = _planned_fragments(manifest)
    inflight = state.get("inflight")
    require(isinstance(inflight, dict) and summary["completed"] < len(planned) and
            inflight.get("position") == summary["completed"],
            "saved fragment intent position differs")
    spec, index, fragment, expected = planned[summary["completed"]]
    require(inflight.get("region") == spec["name"] and inflight.get("index") == index and
            inflight.get("start_sector") == fragment["start_sector"] and
            inflight.get("sectors") == fragment["sectors"] and
            inflight.get("target_sha256") == expected and
            type(inflight.get("attempts")) is int and 1 <= inflight["attempts"] <= 2,
            "saved fragment intent differs")
    with Path(spec["target"]).open("rb") as source:
        source.seek(fragment["start_sector"] * SECTOR)
        target = source.read(fragment["sectors"] * SECTOR)
    require(len(target) == fragment["sectors"] * SECTOR and
            hashlib.sha256(target).hexdigest() == expected,
            "saved fragment target changed")
    readback_dir = Path(job_dir) / "fragment-readbacks"
    require(not readback_dir.is_symlink(), "fragment readback directory is a link")
    saved_reads = inflight.get("attempt_readbacks", [])
    require(isinstance(saved_reads, list) and len(saved_reads) <= inflight["attempts"] and
            inflight.get("last_observed_attempt") ==
            (saved_reads[-1].get("attempt") if saved_reads and
             saved_reads[-1].get("attempt") == inflight["attempts"] else None),
            "saved inflight readback sequence differs")
    for expected_attempt, observation in enumerate(saved_reads, 1):
        require(isinstance(observation, dict) and
                observation.get("attempt") == expected_attempt and
                isinstance(observation.get("readback_path"), str) and
                Path(observation["readback_path"]).parts[:1] == ("fragment-readbacks",) and
                ".." not in Path(observation["readback_path"]).parts,
                "saved inflight readback malformed")
        observed = Path(job_dir) / observation["readback_path"]
        require(observed.parent.resolve() == readback_dir.resolve() and
                not observed.is_symlink() and observed.is_file() and
                observed.stat().st_size == fragment["sectors"] * SECTOR and
                digest(observed) == observation.get("readback_sha256"),
                "saved inflight readback changed")


def _fragment_inflight_reentry_boundary(job_dir: Path, manifest: dict, state: dict) -> bool:
    try:
        _validate_fragment_inflight(job_dir, manifest, state)
        return True
    except (TransactionError, OSError, KeyError, TypeError, ValueError):
        return False


def _retryable_fragment_usb(exc: Exception) -> bool:
    if getattr(exc, "usb_cleanup_dispose_failed", False):
        return False
    if not isinstance(exc, (USBReadError, CameraWriteIOError)):
        return False
    if isinstance(exc, USBReadError) and exc.endpoint_missing_during_transfer:
        return True
    if exc.errno in TRANSIENT_CAMERA_ERRNOS:
        return True
    if (sys.platform == "win32" and exc.backend_error_type == "USBError" and
            exc.backend_error_code == -9 and exc.errno in (None, errno.EPIPE) and
            exc.stage in {"usb_bulk_read", "usb_handshake", "usb_completion", "usb_open", "flash"}):
        return True
    # PyUSB maps a timeout to the host OS errno (60 on macOS), while
    # retaining the libusb code. The code is only trusted for a typed USB
    # exception from the exact camera read/flash window.
    return (exc.backend_error_type in {"USBError", "USBTimeoutError"} and
            exc.backend_error_code in TRANSIENT_LIBUSB_CODES)


def _fragment_transport_unavailable(exc: Exception) -> bool:
    if getattr(exc, "usb_cleanup_dispose_failed", False):
        return False
    if isinstance(exc, (USBReadError, CameraWriteIOError)):
        return _retryable_fragment_usb(exc)
    if isinstance(exc, FastbootIdentityError):
        return False
    return isinstance(exc, (RamPayloadNotReady, FastbootEnumerationUnavailable))


def _apply_fragment_mode(job_dir: Path, manifest: dict, state: dict, *,
                         transport_factory, validator=None, progress=None) -> dict:
    """Write only A!=B fragments, checking each written range immediately."""
    require(not manifest["reboot"] and not state.get("reboot_may_have_occurred"),
            "fragment restore does not reboot")
    planned = _planned_fragments(manifest)
    summary = verify_fragment_results(job_dir, manifest, state)
    require(not state.get("write_may_have_occurred") or
            summary["completed"] > 0 or state.get("inflight"),
            "possible fragment write lacks durable outcome or intent")
    if summary["completed"] == summary["total"]:
        require(state.get("inflight") is None, "completed fragments still have an inflight write")
        state["status"] = "PARTIAL" if summary["failed"] else "VERIFIED"
        state["fragment_summary"] = summary
        state.pop("error", None)
        durable_json(job_dir / "journal.json", state)
        return state
    readback_dir = job_dir / "fragment-readbacks"
    readback_dir.mkdir(mode=0o700, exist_ok=True)
    require(not readback_dir.is_symlink(), "fragment readback directory is a link")
    if state.get("epoch", 0) == 0:
        state["epoch"] = 1
        durable_json(job_dir / "journal.json", state)
    with transport_factory() as transport:
        require(transport.verify_product() == "ILCE-7M4",
                "unexpected fragment restore Fastboot product")
        _identity(transport, manifest["identity"], job_dir)
        if validator is not None:
            validator(transport, job_dir)
        _emit(progress, "fragment_write_begin", "Writing planned restore fragments",
              completed_fragments=summary["completed"], planned_fragments=len(planned),
              verified_fragments=summary["verified"], failed_fragments=summary["failed"])
        last_region = None
        try:
            for position in range(summary["completed"], len(planned)):
                spec, index, fragment, expected = planned[position]
                name = spec["name"]
                if name != last_region:
                    require(transport.region_size(name) == spec["bytes"],
                            f"live region size changed: {name}")
                    last_region = name
                with Path(spec["target"]).open("rb") as source:
                    source.seek(fragment["start_sector"] * SECTOR)
                    data = source.read(fragment["sectors"] * SECTOR)
                require(len(data) == fragment["sectors"] * SECTOR and
                        hashlib.sha256(data).hexdigest() == expected,
                        f"target fragment changed: {name} {index}")
                payload = sparse_fragment(spec["bytes"] // SECTOR,
                                          fragment["start_sector"], data)
                require(len(payload) == fragment["bytes"] and
                        hashlib.sha256(payload).hexdigest() == fragment["sha256"],
                        f"sparse target fragment changed: {name} {index}")
                inflight = state.get("inflight")
                recovered_attempt = inflight["attempts"] if inflight else None
                if inflight:
                    _validate_fragment_inflight(job_dir, manifest, state)
                else:
                    inflight = {"position": position, "region": name, "index": index,
                                "start_sector": fragment["start_sector"],
                                "sectors": fragment["sectors"],
                                "target_sha256": expected, "attempts": 0,
                                "attempt_readbacks": []}
                def finish_observed(observation: dict) -> None:
                    actual = observation["readback_sha256"]
                    status = "verified" if actual == expected else "failed"
                    row = {"region": name, "index": index, "start_sector": fragment["start_sector"],
                           "sectors": fragment["sectors"], "target_sha256": expected,
                           "readback_sha256": actual,
                           "readback_path": observation["readback_path"],
                           "attempts": inflight["attempts"],
                           "attempt_readbacks": inflight["attempt_readbacks"],
                           "status": status, "epoch": state["epoch"]}
                    state.setdefault("fragment_results", []).append(row)
                    if status == "verified":
                        state["confirmed_fragments"] += 1
                    state.pop("inflight", None)
                    state["status"] = "FRAGMENT_VERIFIED" if status == "verified" else "FRAGMENT_FAILED"
                    durable_json(job_dir / "journal.json", state)
                    stage_name = ("fragment_write_verified" if status == "verified"
                                  else "fragment_write_failed")
                    message = (f"{name} fragment {index + 1} written and readback verified"
                               if status == "verified"
                               else f"{name} fragment {index + 1} write check failed after 2 attempts")
                    completed = position + 1
                    verified = state["confirmed_fragments"]
                    failed = completed - verified
                    message += (f"; {completed}/{len(planned)} completed, "
                                f"{verified} verified, {failed} failed")
                    _emit(progress, stage_name, message,
                          fraction=completed / len(planned),
                          completed_fragments=completed,
                          planned_fragments=len(planned),
                          verified_fragments=verified,
                          failed_fragments=failed)
                if (inflight.get("last_observed_attempt") == inflight["attempts"] and
                        (inflight["attempt_readbacks"][-1]["readback_sha256"] == expected or
                         inflight["attempts"] == 2)):
                    finish_observed(inflight["attempt_readbacks"][-1])
                    continue
                while True:
                    if (inflight["attempts"] == 0 or
                            (inflight["attempts"] < 2 and
                             inflight.get("last_observed_attempt") == inflight["attempts"])):
                        require(inflight["attempts"] < 2, "fragment write budget exhausted")
                        inflight["attempts"] += 1
                        inflight.pop("last_observed_attempt", None)
                        state["status"] = "WRITE_INFLIGHT"
                        state["write_may_have_occurred"] = True
                        state["inflight"] = inflight
                        durable_json(job_dir / "journal.json", state)
                        try:
                            _write_response(transport, name, payload, fragment["sectors"],
                                            capability_restore=True, fragment_mode=True)
                        except OSError as exc:
                            if isinstance(exc, usb.core.USBError) or exc.errno in TRANSIENT_CAMERA_ERRNOS:
                                raise CameraWriteIOError(exc) from exc
                            raise
                    recovering = recovered_attempt == inflight["attempts"]
                    if recovering:
                        _emit(progress, "restore_retry_readback",
                              f"{name} fragment {index + 1}: reading back the last uncertain write")
                    readback = readback_dir / f"{position:06d}-attempt{inflight['attempts']}-{uuid4().hex}.bin"
                    transport.read_region(name, readback,
                                          offset=fragment["start_sector"] * SECTOR,
                                          length=fragment["sectors"] * SECTOR)
                    require(readback.is_file() and
                            readback.stat().st_size == fragment["sectors"] * SECTOR,
                            f"fragment readback incomplete: {name} {index}")
                    actual = digest(readback)
                    observation = {"attempt": inflight["attempts"],
                                   "readback_path": str(readback.relative_to(job_dir)),
                                   "readback_sha256": actual}
                    inflight.setdefault("attempt_readbacks", []).append(observation)
                    inflight["last_observed_attempt"] = inflight["attempts"]
                    state["inflight"] = inflight
                    durable_json(job_dir / "journal.json", state)
                    if recovering:
                        decision = ("matches backup; skipping rewrite" if actual == expected
                                    else "differs from backup; rewriting once" if inflight["attempts"] == 1
                                    else "differs from backup; write limit reached")
                        _emit(progress, "restore_retry_decision", f"{name} fragment {index + 1}: {decision}")
                    if actual == expected or inflight["attempts"] == 2:
                        finish_observed(observation)
                        break
                    _emit(progress, "readback", f"{name} fragment {index + 1} differs; retrying once")
            _emit(progress, "saved_fragment_evidence",
                  "Checking saved local fragment readback evidence")
            summary = verify_fragment_results(job_dir, manifest, state)
            require(summary["completed"] == summary["total"], "fragment result sequence incomplete")
            state["fragment_summary"] = summary
            state["status"] = "PARTIAL" if summary["failed"] else "VERIFIED"
            state.pop("error", None)
            state["last_verified_utc"] = now()
            durable_json(job_dir / "journal.json", state)
            return state
        except BaseException as exc:
            state["status"] = "UNKNOWN_AFTER_WRITE" if state.get("write_may_have_occurred") else "NO_WRITE"
            state["error"] = str(exc)
            state["error_detail"] = {
                "type": type(exc).__name__,
                "stage": getattr(exc, "stage", None),
                "errno": getattr(exc, "errno", None),
                "backend_error_type": getattr(exc, "backend_error_type", None),
                "backend_error_code": getattr(exc, "backend_error_code", None),
            }
            durable_json(job_dir / "journal.json", state)
            raise


def _verified_region_boundary(job_dir: Path, manifest: dict, state: dict) -> bool:
    """Only completed fragment readbacks permit guarded restore reentry."""
    return _fragment_boundary(job_dir, manifest, state)


def _full_reconcile_evidence_valid(job_dir: Path, manifest: dict, state: dict) -> bool:
    """Validate the fresh, complete live images used by a zero-pending plan."""
    try:
        plan_path = Path(state["last_reconcile"])
        epoch = state["epoch"]
        epoch_dir = job_dir / f"reconcile-{epoch:03d}"
        if (type(epoch) is not int or epoch < 1 or
                plan_path.resolve() != (epoch_dir / "plan.json").resolve() or
                epoch_dir.is_symlink() or plan_path.is_symlink() or not plan_path.is_file()):
            return False
        proof = state.get("storage_verification")
        if proof is not None and (not isinstance(proof, dict) or
                                  proof.get("kind") != "full_reconcile" or
                                  proof.get("epoch") != epoch or
                                  proof.get("plan_sha256") != digest(plan_path)):
            return False
        plan = json.loads(plan_path.read_text())
        if (plan.get("status") != "VERIFIED" or plan.get("epoch") != epoch or
                [row.get("name") for row in plan.get("regions", [])] !=
                [row["name"] for row in manifest["regions"]]):
            return False
        for spec, row in zip(manifest["regions"], plan["regions"]):
            live = epoch_dir / f"{spec['name']}.live.bin"
            if (Path(row.get("live_file", "")).resolve() != live.resolve() or live.is_symlink() or
                    not live.is_file() or live.stat().st_size != spec["bytes"] or
                    row.get("live_sha256") != spec["target_sha256"] or
                    row.get("pending_sectors") != 0 or
                    digest(live) != spec["target_sha256"]):
                return False
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


def reconcile(job_dir: Path, *, transport_factory=FastbootTransport,
              transport=None, validator=None, progress=None) -> dict:
    """Read-only sector triage. Never resends a previous write or reboot."""
    job_dir = Path(job_dir)
    manifest, state = load(job_dir)
    require(not _fragment_mode(manifest),
            "fragment restore uses per-fragment readback; select the saved snapshot for a new restore")
    return _reconcile_loaded(job_dir, manifest, state,
                             transport_factory=transport_factory, transport=transport,
                             validator=validator, progress=progress)


def _reconcile_loaded(job_dir: Path, manifest: dict, state: dict, *,
                      transport_factory=FastbootTransport, transport=None,
                      validator=None, progress=None) -> dict:
    """Continue read-only triage after this apply already verified all local pins."""
    if state["reboot_may_have_occurred"]:
        return {"status": "NEEDS_USER_ACTION", "reason": "reboot outcome requires camera observation"}
    state.pop("storage_verification", None)
    durable_json(job_dir / "journal.json", state)
    epoch = state["epoch"] + 1
    epoch_dir = job_dir / f"reconcile-{epoch:03d}"
    reuse_unwritten = (epoch_dir.is_dir() and not (epoch_dir / "plan.json").exists() and
                       not state.get("write_may_have_occurred") and
                       not state.get("reboot_may_have_occurred") and
                       not state.get("inflight") and
                       state.get("confirmed_fragments") == 0)
    if not reuse_unwritten:
        while epoch_dir.exists():
            epoch += 1
            epoch_dir = job_dir / f"reconcile-{epoch:03d}"
        epoch_dir.mkdir(mode=0o700)
    own_transport = transport is None
    if own_transport:
        transport = transport_factory()
    try:
        _identity(transport, manifest["identity"], epoch_dir)
        _emit(progress, "identity", "Device content pins verified")
        if validator is not None:
            validator(transport, epoch_dir)
        pending_summary = []
        for region in manifest["regions"]:
            name = region["name"]
            require(transport.region_size(name) == region["bytes"], f"live region size changed: {name}")
            live = epoch_dir / f"{name}.live.bin"
            _emit(progress, "prewrite", f"Reading full {name} preimage")
            read_full_region(transport, name, live, region["bytes"])
            checked = _plan_live_region(region, live, state)
            pending_summary.append(checked)
            _emit(progress, "reconcile", f"{name}: {checked['pending_sectors']} sectors pending")
        result = {"status": "READY" if any(x["pending_sectors"] for x in pending_summary)
                  else "VERIFIED", "epoch": epoch, "regions": pending_summary}
        durable_json(epoch_dir / "plan.json", result)
        state["epoch"] = epoch
        state["last_reconcile"] = str(epoch_dir / "plan.json")
        state["status"] = result["status"]
        durable_json(job_dir / "journal.json", state)
        return result
    except BaseException as exc:
        state["status"] = "NO_WRITE" if not state["write_may_have_occurred"] else "UNKNOWN_AFTER_WRITE"
        state["error"] = str(exc)
        durable_json(job_dir / "journal.json", state)
        raise
    finally:
        if own_transport:
            transport.close()


def apply(job_dir: Path, *, transport_factory=FastbootTransport, validator=None,
          progress=None) -> dict:
    """One attempt. A later explicit call reconciles before any continuation."""
    job_dir = Path(job_dir)
    manifest, state = load(job_dir)
    return _apply_loaded(job_dir, manifest, state, transport_factory=transport_factory,
                         validator=validator, progress=progress)


def _apply_loaded(job_dir: Path, manifest: dict, state: dict, *,
                  transport_factory=FastbootTransport, validator=None,
                  progress=None) -> dict:
    """Run one attempt using pins fully rehashed immediately before mode entry."""
    require(not state["reboot_may_have_occurred"], "reboot may already have occurred")
    if _fragment_mode(manifest):
        return _apply_fragment_mode(job_dir, manifest, state,
                                    transport_factory=transport_factory,
                                    validator=validator, progress=progress)
    with transport_factory() as transport:
        plan = _reconcile_loaded(job_dir, manifest, state, transport=transport,
                                 validator=validator, progress=progress)
        state = json.loads((job_dir / "journal.json").read_text())
        require(state.get("schema") == "write-transaction-state-v1",
                "unsupported transaction state schema")
        if plan["status"] == "VERIFIED":
            if manifest["reboot"]:
                no_write_stock = (manifest["kind"] == "stock_sd_recovery" and
                                  manifest["metadata"].get("preexisting_trigger_verified") is True and
                                  all(not item["scope"] for item in manifest["regions"]))
                require(state["write_may_have_occurred"] or no_write_stock,
                        "storage already at target without this task's write; reboot not automatic")
            require(_full_reconcile_evidence_valid(job_dir, manifest, state),
                    "complete live readback differs from target")
            state.pop("inflight", None)
            state["storage_verification"] = {
                "kind": "full_reconcile", "epoch": state["epoch"],
                "plan_sha256": digest(Path(state["last_reconcile"]))}
            state["status"] = "VERIFIED_STORAGE"
            state["last_verified_utc"] = now()
            durable_json(job_dir / "journal.json", state)
            if manifest["reboot"]:
                return _reboot_once(job_dir, state, transport)
            state["status"] = "VERIFIED"
            durable_json(job_dir / "journal.json", state)
            return state
        epoch_dir = job_dir / f"reconcile-{plan['epoch']:03d}"
        if state.pop("storage_verification", None) is not None:
            durable_json(job_dir / "journal.json", state)
        total_fragments = sum(len(item["fragments"]) for item in plan["regions"])
        done_fragments = 0
        try:
            for spec, region_plan in zip(manifest["regions"], plan["regions"]):
                name = spec["name"]
                for index, fragment in enumerate(region_plan["fragments"]):
                    with Path(spec["target"]).open("rb") as target:
                        target.seek(fragment["start_sector"]*SECTOR)
                        data = target.read(fragment["sectors"]*SECTOR)
                    payload = sparse_fragment(spec["bytes"]//SECTOR, fragment["start_sector"], data)
                    require(hashlib.sha256(payload).hexdigest() == fragment["sha256"] and
                            len(payload) == fragment["bytes"], "fragment changed after reconciliation")
                    state["status"] = "WRITE_INFLIGHT"
                    state["write_may_have_occurred"] = True
                    state["inflight"] = {"region": name, "fragment": index,
                                         "sha256": fragment["sha256"], "epoch": plan["epoch"]}
                    durable_json(job_dir / "journal.json", state)
                    try:
                        _write_response(transport, name, payload, fragment["sectors"])
                    except OSError as exc:
                        if isinstance(exc, usb.core.USBError) or exc.errno in TRANSIENT_CAMERA_ERRNOS:
                            raise CameraWriteIOError(exc) from exc
                        raise
                    state["confirmed_fragments"] += 1
                    done_fragments += 1
                    state.pop("inflight", None)
                    durable_json(job_dir / "journal.json", state)
                    _emit(progress, "write", f"{name} fragment {index + 1} confirmed",
                          fraction=done_fragments / total_fragments)
                readback = epoch_dir / f"{name}.readback.bin"
                while readback.exists() or readback.with_name(readback.name + ".partial").exists():
                    readback = epoch_dir / f"{name}.readback-{uuid4().hex}.bin"
                _emit(progress, "readback", f"Reading full {name} after write")
                transport.read_region(name, readback)
                require(digest(readback) == spec["target_sha256"], f"full readback differs: {name}")
            state["status"] = "VERIFIED_STORAGE"
            state["last_verified_utc"] = now()
            durable_json(job_dir / "journal.json", state)
            if manifest["reboot"]:
                _reboot_once(job_dir, state, transport)
            else:
                state["status"] = "VERIFIED"
                durable_json(job_dir / "journal.json", state)
            return state
        except BaseException as exc:
            state["status"] = ("REBOOT_UNCERTAIN" if state["reboot_may_have_occurred"]
                               else "UNKNOWN_AFTER_WRITE" if state["write_may_have_occurred"]
                               else "NO_WRITE")
            state["error"] = str(exc)
            durable_json(job_dir / "journal.json", state)
            raise


def _apply_fragment_with_reconnect(job_dir: Path, manifest: dict, state: dict, *,
                                   manifest_digest: str, transport_factory,
                                   validator, progress, reconnect, max_retries: int) -> dict:
    """Resume one current-task fragment only after a fresh RAM session and readback."""
    def latest() -> dict:
        require(digest(job_dir / "manifest.json") == manifest_digest,
                "restore manifest changed during reconnect")
        saved = json.loads((job_dir / "journal.json").read_text())
        require(saved.get("schema") == "write-transaction-state-v1" and
                saved.get("reboot_may_have_occurred") is False,
                "restore state changed during reconnect")
        verify_fragment_results(job_dir, manifest, saved)
        if saved.get("inflight") is not None:
            _validate_fragment_inflight(job_dir, manifest, saved)
        return saved

    def reenter(reason: str, *, force_bridge: bool) -> dict:
        require(reconnect is not None, "restore reconnect callback unavailable")
        while True:
            saved = latest()
            safe = (saved.get("inflight") is not None and
                    _fragment_inflight_reentry_boundary(job_dir, manifest, saved)) or (
                    saved.get("inflight") is None and
                    (not saved.get("write_may_have_occurred") or
                     _fragment_boundary(job_dir, manifest, saved)))
            require(safe, "restore reconnect lacks a saved fragment boundary")
            verified = saved.get("confirmed_fragments", 0)
            if verified > saved.get("restore_retry_verified_fragments", 0):
                saved["restore_retry_used"] = 0
                saved["restore_retry_verified_fragments"] = verified
            used = saved.get("restore_retry_used", 0)
            require(type(used) is int and 0 <= used < max_retries,
                    "restore reconnect budget exhausted")
            used += 1
            saved["restore_retry_used"] = used
            saved["reentry_intent"] = {
                "retry": used, "reason": reason, "force_bridge": force_bridge,
                "manifest_sha256": manifest_digest,
                "inflight_position": (saved["inflight"]["position"]
                                      if saved.get("inflight") else None),
                "recorded_utc": now(),
            }
            durable_json(job_dir / "journal.json", saved)
            _emit(progress, "restore_retry",
                  f"{reason}: guarded reconnect {used}/{max_retries}",
                  retry=used, max_retries=max_retries,
                  reason=reason, reentry_requested=force_bridge)
            try:
                # This callback owns a new transport. The failed one has left
                # its context before this point; a pending flash forces reset
                # so queued old INFO/OKAY responses cannot be reused.
                reconnect(force_bridge=force_bridge)
                _emit(progress, "restore_reconnected", "Camera Fastboot reconnected; verifying current state")
                return latest()
            except Exception as exc:
                if not _fragment_transport_unavailable(exc):
                    raise
                force_bridge = True
                reason = "uart_marker_absent" if isinstance(exc, RamPayloadNotReady) else "camera_unavailable_after_reentry"

    state = latest()
    if state.get("inflight") is not None and reconnect is not None:
        state = reenter("saved_flash_outcome_unknown", force_bridge=True)
    while True:
        try:
            return _apply_loaded(job_dir, manifest, state,
                                 transport_factory=transport_factory,
                                 validator=validator, progress=progress)
        except Exception as exc:
            if not _fragment_transport_unavailable(exc):
                raise
            state = latest()
            state = reenter("flash_outcome_unknown" if state.get("inflight") else
                            "camera_usb_interrupted" if _retryable_fragment_usb(exc) else
                            "fastboot_unavailable",
                            force_bridge=(state.get("inflight") is not None or
                                          _retryable_fragment_usb(exc)))


def apply_with_reconnect(job_dir: Path, *, transport_factory=FastbootTransport,
                         validator=None, progress=None, reconnect=None,
                         max_retries: int = MAX_RESTORE_RECONNECTS,
                         host_load=None) -> dict:
    """Bounded restore continuation with a fresh full reconcile before every write.

    Only classified camera USB I/O interruptions qualify. `apply` durably
    records an uncertain flash before raising; its next invocation reads the
    entire live preimage and accepts only original/target/reviewed drift.
    Reboot-uncertain transactions and local output failures never auto-retry.
    """
    require(0 <= max_retries <= MAX_RESTORE_RECONNECTS,
            "restore reconnect limit outside reviewed range")
    job_dir = Path(job_dir)
    manifest, state = (host_load() if host_load is not None else load(job_dir, progress=progress))
    manifest_digest = digest(job_dir / "manifest.json")
    def refreshed_after_host_load(loaded_state: dict) -> dict:
        # The USB owner may renew a lost RAM session during any host hash,
        # including the hash after an interrupted possible write. Only the
        # latest durable retry count is authoritative.
        live_state = json.loads((job_dir / "journal.json").read_text())
        require(live_state.get("schema") == "write-transaction-state-v1" and
                digest(job_dir / "manifest.json") == manifest_digest,
                "restore transaction changed during local verification")
        require(all(live_state.get(key) == loaded_state.get(key) for key in
                    ("epoch", "write_may_have_occurred", "reboot_may_have_occurred",
                     "inflight", "confirmed_fragments", "last_reconcile")),
                "restore transaction safety state changed during local verification")
        return live_state
    if host_load is not None:
        state = refreshed_after_host_load(state)
    retries = state.get("restore_retry_used", 0)
    require(type(retries) is int and 0 <= retries <= max_retries,
            "saved restore reconnect count invalid")
    if _fragment_mode(manifest):
        return _apply_fragment_with_reconnect(
            job_dir, manifest, state, manifest_digest=manifest_digest,
            transport_factory=transport_factory, validator=validator,
            progress=progress, reconnect=reconnect, max_retries=max_retries)
    while True:
        try:
            return _apply_loaded(job_dir, manifest, state,
                                 transport_factory=transport_factory,
                                 validator=validator, progress=progress)
        except Exception as exc:
            interrupted = _retryable_fragment_usb(exc)
            unavailable = (isinstance(exc, (RamPayloadNotReady, FastbootEnumerationUnavailable)) or
                           (isinstance(exc, FastbootError) and
                            "unavailable" in str(exc).lower()))
            transient = interrupted or unavailable
            if not transient:
                raise
            # The first attempt already rehashed every local pin. Re-read the
            # durable state after a USB failure; only a possible write requires
            # another full host hash before a later device action.
            state = json.loads((job_dir / "journal.json").read_text())
            require(state.get("schema") == "write-transaction-state-v1",
                    "unsupported transaction state schema")
            require(digest(job_dir / "manifest.json") == manifest_digest,
                    "saved restore manifest changed during this attempt")
            if state.get("write_may_have_occurred"):
                manifest, state = (host_load() if host_load is not None else load(job_dir, progress=progress))
                if host_load is not None:
                    state = refreshed_after_host_load(state)
                    retries = state.get("restore_retry_used", 0)
                    require(type(retries) is int and 0 <= retries <= max_retries,
                            "saved restore reconnect count invalid")
            if state.get("reboot_may_have_occurred"):
                raise
            # A missing device after an unacknowledged flash cannot be reset
            # blindly. Direct Fastboot may still reappear and allow read-only
            # reconciliation; bridge reentry must wait for user intervention.
            safe_boundary = (_fragment_boundary(job_dir, manifest, state)
                             if _fragment_mode(manifest) else
                             _verified_region_boundary(job_dir, manifest, state))
            if unavailable and (state.get("inflight") or
                                (state.get("write_may_have_occurred") and not safe_boundary) or
                                reconnect is None):
                raise
            if safe_boundary and state["confirmed_fragments"] > state.get("restore_retry_verified_fragments", 0):
                retries = 0
                state["restore_retry_verified_fragments"] = state["confirmed_fragments"]
                state["restore_retry_used"] = 0
                durable_json(job_dir / "journal.json", state)
            if retries >= max_retries:
                raise
            retries += 1
            state["restore_retry_used"] = retries
            durable_json(Path(job_dir) / "journal.json", state)
            if progress is not None:
                try:
                    reason = ("uart_marker_absent" if isinstance(exc, RamPayloadNotReady) else
                              "serial_descriptor_unreadable" if isinstance(exc, FastbootEnumerationUnavailable) else
                              "fastboot_unavailable" if unavailable else
                              "camera_usb_read" if isinstance(exc, USBReadError) else
                              "flash_response_uncertain")
                    progress({"stage": "restore_retry",
                              "message": f"{reason}: reconcile (retry {retries}/{max_retries})",
                              "retry": retries, "max_retries": max_retries,
                              "reason": reason,
                              "reentry_requested": unavailable})
                except Exception:
                    pass
            if unavailable:
                while True:
                    try:
                        reconnect()
                        break
                    except RamPayloadNotReady:
                        state = json.loads((Path(job_dir) / "journal.json").read_text())
                        require(state.get("schema") == "write-transaction-state-v1",
                                "unsupported transaction state schema")
                        safe = (state.get("status") in {"READY", "NO_WRITE", "UNKNOWN_AFTER_WRITE",
                                                        "FRAGMENT_VERIFIED", "FRAGMENT_FAILED"} and
                                (not state.get("write_may_have_occurred") or
                                 (_fragment_boundary(job_dir, manifest, state) if _fragment_mode(manifest)
                                  else _verified_region_boundary(job_dir, manifest, state))) and
                                not state.get("reboot_may_have_occurred") and
                                not state.get("inflight"))
                        if not safe or retries >= max_retries:
                            raise
                        retries += 1
                        state["restore_retry_used"] = retries
                        durable_json(Path(job_dir) / "journal.json", state)
                        if progress is not None:
                            progress({"stage": "restore_reentry_retry",
                                      "message": f"UART ready marker absent; guarded bridge attempt {retries}/{max_retries}",
                                      "retry": retries, "max_retries": max_retries,
                                      "reason": "uart_marker_absent"})
            state = json.loads((job_dir / "journal.json").read_text())
            require(state.get("schema") == "write-transaction-state-v1",
                    "unsupported transaction state schema")


def _reboot_once(job_dir: Path, state: dict, transport: FastbootTransport) -> dict:
    require(not state["reboot_may_have_occurred"], "reboot may already have occurred")
    state["status"] = "REBOOT_INFLIGHT"
    state["reboot_may_have_occurred"] = True
    durable_json(job_dir / "journal.json", state)
    try:
        transport._send("reboot")
        transport._reply()
    except BaseException as exc:
        state["status"] = "REBOOT_UNCERTAIN"
        state["error"] = str(exc)
        durable_json(job_dir / "journal.json", state)
        raise
    state["status"] = "REBOOT_ACKNOWLEDGED"
    state["reboot_acknowledged_utc"] = now()
    durable_json(job_dir / "journal.json", state)
    return state
