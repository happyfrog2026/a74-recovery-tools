"""Assemble and verify an offline archive of independent raw double reads."""

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from . import snapshot


FORMAT = "cross-session-raw-archive-v1"
PASSES = (("first", "raw"), ("second", "repeat"))


def _load(folder: Path, name: str) -> tuple[dict, str]:
    path = folder / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing regular file: {path}")
    data = path.read_bytes()
    expected = json.loads(data)
    if not isinstance(expected, dict):
        raise ValueError(f"invalid JSON object: {path}")
    return expected, hashlib.sha256(data).hexdigest()


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing capture timestamp")
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("capture timestamp lacks a UTC offset")
    return instant


def _pair(region: str, extent: dict, reads: dict) -> dict:
    size = extent.get("bytes")
    digest = extent.get("sha256")
    if (type(size) is not int or size <= 0 or size % 512 or
            not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)):
        raise ValueError(f"invalid extent for {region}")
    if region != "nflasha" and size != snapshot.REGIONS[region]:
        raise ValueError(f"fixed-size region is incomplete: {region}")
    evidence = {}
    for attempt, suffix in PASSES:
        item = reads.get(attempt, {})
        filename = f"{region}.{suffix}.bin"
        if item.get("file") != filename or item.get("bytes") != size or item.get("sha256") != digest:
            raise ValueError(f"double-read manifest mismatch: {region} {attempt}")
        _timestamp(item.get("checked_utc"))
        evidence[attempt] = {"file": filename, "bytes": size, "sha256": digest,
                             "checked_utc": item["checked_utc"]}
    if _timestamp(evidence["first"]["checked_utc"]) > _timestamp(evidence["second"]["checked_utc"]):
        raise ValueError(f"capture timestamps are out of order: {region}")
    return evidence


def _source_pair(folder: Path, region: str, previous: Path, *, is_regional: bool) -> dict:
    receipt, receipt_hash = _load(folder, "capture-receipt.json")
    manifest_name = "verified-region.json" if is_regional else "verified.json"
    manifest, manifest_hash = _load(folder, manifest_name)
    sizes = snapshot.captured_regions(receipt)
    provenance = None
    provenance_hash = None
    if is_regional:
        provenance, provenance_hash = _load(folder, "recapture.json")
        expected_profile = (snapshot.EMMC_RECAPTURE_PROFILE if region == "nflasha"
                            else snapshot.REGIONAL_RECAPTURE_PROFILE)
        if (receipt.get("status") != "VERIFIED" or receipt.get("capture_profile") != expected_profile or
                set(sizes) != {region} or set(manifest) != {region} or
                provenance.get("status") != "PARTIAL" or provenance.get("requested_region") != region or
                provenance.get("same_camera_confirmed_by_operator") is not True or
                provenance.get("previous_snapshot") != str(previous)):
            raise ValueError(f"unverified or unrelated regional capture: {folder}")
    elif receipt.get("capture_profile") is not None or receipt.get("status") not in {"INCOMPLETE", "VERIFIED"}:
        raise ValueError(f"invalid original backup receipt: {folder}")
    extent = manifest.get(region, {})
    if (receipt.get("region_coverage", {}).get(region, {}).get("status") != "VERIFIED" or
            sizes.get(region) != extent.get("bytes")):
        raise ValueError(f"source region was not fully verified: {region}")
    reads = _pair(region, extent, receipt.get("reads", {}).get(region, {}))
    started = _timestamp(receipt.get("started_utc"))
    first = _timestamp(reads["first"]["checked_utc"])
    second = _timestamp(reads["second"]["checked_utc"])
    if first < started or ("finished_utc" in receipt and _timestamp(receipt["finished_utc"]) < second):
        raise ValueError(f"read times are outside their source capture: {region}")
    if provenance is not None and (
            provenance.get("verified_regions") != {region: receipt["reads"][region]} or
            provenance.get("capture_started_utc") != receipt.get("started_utc") or
            provenance.get("finished_utc") != receipt.get("finished_utc")):
        raise ValueError(f"regional capture provenance mismatch: {region}")
    return {"source": str(folder), "source_capture_started_utc": receipt["started_utc"],
            "source_receipt_sha256": receipt_hash, "source_manifest_sha256": manifest_hash,
            "source_provenance_sha256": provenance_hash, "bytes": extent["bytes"],
            "sha256": extent["sha256"], "reads": reads}


def _source_metadata(pair: dict) -> dict:
    archived = {
        "capture-receipt.json": pair["source_receipt_sha256"],
        "verified-region.json" if pair["source_provenance_sha256"] else "verified.json":
            pair["source_manifest_sha256"],
    }
    if pair["source_provenance_sha256"]:
        archived["recapture.json"] = pair["source_provenance_sha256"]
    return archived


def _check_source_metadata(folder: Path, pair: dict, destination: Path | None = None) -> None:
    if destination is not None:
        destination.mkdir(mode=0o700, parents=True)
    for filename, expected in _source_metadata(pair).items():
        path = folder / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing source metadata: {path}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"source metadata hash mismatch: {path}")
        if destination is not None:
            with (destination / filename).open("xb") as sink:
                sink.write(data)
                sink.flush()
                os.fsync(sink.fileno())



def _read_checked(folder: Path, evidence: dict, destination: Path | None = None) -> None:
    path = folder / evidence["file"]
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing regular raw read: {path}")
    if destination is not None:
        pending = destination.with_suffix(".partial")
        sink = pending.open("xb")
    else:
        pending = None
        sink = nullcontext()
    digest = hashlib.sha256()
    size = 0
    with sink as out, path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            size += len(block)
            if size > evidence["bytes"]:
                raise ValueError(f"raw read exceeds recorded length: {path}")
            digest.update(block)
            if out is not None:
                out.write(block)
        if size != evidence["bytes"] or digest.hexdigest() != evidence["sha256"]:
            raise ValueError(f"raw read does not match recorded bytes and SHA-256: {path}")
        if out is not None:
            out.flush()
            os.fsync(out.fileno())
    if pending is not None:
        pending.chmod(0o600)
        pending.replace(destination)


def assemble(previous: Path, emmc: Path, supplements: list[Path], output: Path, *,
             same_camera_confirmed: bool, progress=None) -> dict:
    """Copy verified first and second reads into a distinct non-restorable archive."""
    if same_camera_confirmed is not True:
        raise ValueError("confirm all captures came from the same physical camera")
    previous, emmc, output = (Path(p).expanduser().resolve() for p in (previous, emmc, output))
    supplements = [Path(p).expanduser().resolve() for p in supplements]
    sources = [previous, emmc, *supplements]
    if len(set(sources)) != len(sources) or any(output.is_relative_to(path) for path in sources):
        raise ValueError("use distinct sources and a separate new output directory")
    original_manifest, _ = _load(previous, "verified.json")
    pairs = {"nflasha": _source_pair(emmc, "nflasha", previous, is_regional=True)}
    supplementary = {}
    for folder in supplements:
        record, _ = _load(folder, "recapture.json")
        region = record.get("requested_region")
        if region not in snapshot.REGIONS or region == "nflasha" or region in supplementary:
            raise ValueError("select at most one separate capture per missing region")
        supplementary[region] = folder
    for region in snapshot.REGIONS:
        if region == "nflasha":
            continue
        if region in supplementary:
            if region in original_manifest:
                raise ValueError(f"original backup already records region: {region}")
            pairs[region] = _source_pair(supplementary[region], region, previous, is_regional=True)
        elif region in original_manifest:
            pairs[region] = _source_pair(previous, region, previous, is_regional=False)
    missing = sorted(set(snapshot.REGIONS) - set(pairs))
    if missing:
        return {"status": "INCOMPLETE", "missing_regions": missing,
                "complete_region_set": False, "restorable": False}
    original_receipt, _ = _load(previous, "capture-receipt.json")
    if (_timestamp(pairs["nflasha"]["source_capture_started_utc"]) <
            _timestamp(original_receipt.get("started_utc"))):
        raise ValueError("selected eMMC recapture predates the original backup")

    output.mkdir(mode=0o700)
    path = output / "archive.json"
    record = {"format": FORMAT, "status": "IN_PROGRESS", "archive": str(output),
              "same_camera_confirmed_by_operator": True, "atomic_camera_snapshot": False,
              "restorable": False, "complete_region_set": False, "regions": {}}
    snapshot.save(path, record)
    try:
        for region, pair in pairs.items():
            if progress:
                progress({"stage": "backup_archive", "message": f"Verifying and copying both reads of {region}"})
            source = Path(pair["source"])
            _check_source_metadata(source, pair, output / "source-receipts" / region)
            for evidence in pair["reads"].values():
                _read_checked(source, evidence, output / evidence["file"])
            record["regions"][region] = pair
            snapshot.save(path, record)
    except Exception as exc:
        record.update(status="INCOMPLETE", error=str(exc))
        snapshot.save(path, record)
        return record
    record.update(status="VERIFIED", complete_region_set=True,
                  finished_utc=datetime.now(timezone.utc).isoformat())
    snapshot.save(path, record)
    return record


def verify(folder: Path, progress=None) -> dict:
    """Rehash both archived reads for every region without using the old folders."""
    folder = Path(folder).expanduser().resolve()
    record, _ = _load(folder, "archive.json")
    pairs = record.get("regions", {})
    if (record.get("format") != FORMAT or record.get("status") != "VERIFIED" or
            record.get("complete_region_set") is not True or record.get("restorable") is not False or
            record.get("atomic_camera_snapshot") is not False or
            record.get("same_camera_confirmed_by_operator") is not True or
            set(pairs) != set(snapshot.REGIONS)):
        raise ValueError("archive has no complete verified region set")
    for region, pair in pairs.items():
        if progress:
            progress({"stage": "backup_archive_verify", "message": f"Checking both archived reads of {region}"})
        _check_source_metadata(folder / "source-receipts" / region, pair)
        for evidence in _pair(region, pair, pair.get("reads", {})).values():
            _read_checked(folder, evidence)
    return {**record, "archive": str(folder)}
