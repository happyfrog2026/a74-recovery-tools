"""Checkpointed, read-only full-region capture for one transaction epoch.

The caller owns camera identity checks, epoch selection, and write admission.
This helper never reconnects, resets, or writes to the device.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from .durable_file import durable_replace


DEFAULT_CHUNK_BYTES = 64 * 1024 * 1024
SCHEMA = "resumable-region-read-v1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256(stream.read()).hexdigest() if path.stat().st_size <= 1024 * 1024 else _hash_stream(stream)


def _hash_stream(stream) -> str:
    hasher = sha256()
    while chunk := stream.read(1024 * 1024):
        hasher.update(chunk)
    return hasher.hexdigest()


def _hash_segment(path: Path, whole) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
            whole.update(chunk)
    return hasher.hexdigest()


def _durable_json(path: Path, value: dict) -> None:
    temporary = path.with_name(".j-" + uuid4().hex[:12])
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _paths(output_path: Path) -> tuple[Path, Path, Path]:
    selected = Path(output_path)
    if not selected.is_absolute() or ".." in selected.parts or not selected.parent.is_dir():
        raise ValueError("full-region output must be under an existing absolute directory")
    if selected.is_symlink():
        raise ValueError("full-region output may not be a symlink")
    # macOS /tmp and /var are system symlinks. Canonicalize the containing
    # directory, then keep all generated checkpoint names under that directory.
    output = selected.parent.resolve(strict=True) / selected.name
    segments = output.with_name(output.name + ".segments")
    receipt = output.with_name(output.name + ".segments.json")
    if segments.is_symlink() or receipt.is_symlink():
        raise ValueError("full-region checkpoint may not be a symlink")
    return output, segments, receipt


def _segment_name(offset: int) -> str:
    return f"{offset:016x}.bin"


def _expected_size(offset: int, length: int, chunk_bytes: int) -> int:
    return min(chunk_bytes, length - offset)


def _checked_receipt(receipt_path: Path, *, region: str, length: int,
                     chunk_bytes: int) -> dict:
    if not receipt_path.exists():
        return {"schema": SCHEMA, "region": region, "length": length,
                "chunk_bytes": chunk_bytes, "segments": []}
    row = json.loads(receipt_path.read_text())
    if (row.get("schema") != SCHEMA or row.get("region") != region or
            row.get("length") != length or row.get("chunk_bytes") != chunk_bytes or
            not isinstance(row.get("segments"), list)):
        raise ValueError("full-region checkpoint parameters differ")
    for index, item in enumerate(row["segments"]):
        offset = index * chunk_bytes
        if (not isinstance(item, dict) or item.get("offset") != offset or
                item.get("bytes") != _expected_size(offset, length, chunk_bytes) or
                not isinstance(item.get("sha256"), str) or
                _HEX.fullmatch(item["sha256"]) is None or
                item.get("file") != _segment_name(offset)):
            raise ValueError("full-region checkpoint has invalid segment offset or path")
    if len(row["segments"]) > (length + chunk_bytes - 1) // chunk_bytes:
        raise ValueError("full-region checkpoint has extra segments")
    complete_hash = row.get("sha256")
    if complete_hash is not None and (not isinstance(complete_hash, str) or
                                      _HEX.fullmatch(complete_hash) is None):
        raise ValueError("full-region checkpoint has invalid final hash")
    if complete_hash is not None and len(row["segments"]) != (length + chunk_bytes - 1) // chunk_bytes:
        raise ValueError("incomplete full-region checkpoint has a final hash")
    return row


def read_full_region(transport, region: str, output_path: Path, length: int, *,
                     chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> dict:
    """Read missing chunks and return their durable receipt plus the full hash.

    Every saved chunk is rehashed before reuse. A missing or changed recorded
    chunk aborts. The caller must recheck live device identity on each session.
    """
    if (not isinstance(region, str) or re.fullmatch(r"[A-Za-z0-9-]+", region) is None or
            type(length) is not int or length <= 0 or length % 512 or type(chunk_bytes) is not int or
            chunk_bytes <= 0 or chunk_bytes % 512):
        raise ValueError("invalid region read parameters")
    output, segments_dir, receipt_path = _paths(output_path)
    if segments_dir.exists() and not segments_dir.is_dir():
        raise ValueError("full-region segment checkpoint is not a directory")
    segments_dir.mkdir(mode=0o700, exist_ok=True)
    receipt = _checked_receipt(receipt_path, region=region, length=length,
                               chunk_bytes=chunk_bytes)
    recorded_names = {item["file"] for item in receipt["segments"]}
    for child in segments_dir.iterdir():
        if child.name.endswith(".bin") and child.name not in recorded_names:
            raise ValueError("unrecorded full-region segment exists")
    whole = sha256()
    for item in receipt["segments"]:
        path = segments_dir / item["file"]
        if (path.is_symlink() or not path.is_file() or
                path.stat().st_size != item["bytes"] or
                _hash_segment(path, whole) != item["sha256"]):
            raise ValueError("saved full-region segment differs from checkpoint")
    if transport.region_size(region) != length:
        raise ValueError("live full-region size changed")
    for offset in range(len(receipt["segments"]) * chunk_bytes, length, chunk_bytes):
        size = _expected_size(offset, length, chunk_bytes)
        path = segments_dir / _segment_name(offset)
        temporary = path.with_name(".r-" + uuid4().hex[:12])
        try:
            count = transport.read_region(region, temporary, offset=offset, length=size)
            if (count is not None and count != size) or temporary.stat().st_size != size:
                raise ValueError("short full-region segment read")
            # Windows requires a writable handle for fsync.
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            segment_hash = _hash_segment(temporary, whole)
            durable_replace(temporary, path)
            receipt["segments"].append({"offset": offset, "bytes": size,
                                         "sha256": segment_hash, "file": path.name})
            _durable_json(receipt_path, receipt)
        except BaseException:
            # FastbootTransport writes `output + .partial` before its final
            # rename. Preserve both possible incomplete forms without ever
            # adopting their bytes as a verified checkpoint.
            for incomplete in (temporary, temporary.with_name(temporary.name + ".partial")):
                if incomplete.exists():
                    incomplete.rename(incomplete.with_name(incomplete.name + ".interrupted"))
            raise
    if len(receipt["segments"]) != (length + chunk_bytes - 1) // chunk_bytes:
        raise ValueError("full-region checkpoint coverage incomplete")
    expected_hash = whole.hexdigest()
    if receipt.get("sha256") not in (None, expected_hash):
        raise ValueError("full-region checkpoint final hash differs from segments")
    if output.exists():
        if output.is_symlink() or output.stat().st_size != length:
            raise ValueError("existing full-region output differs")
        full_hash = _hash_file(output)
        if full_hash != expected_hash:
            raise ValueError("existing full-region output differs from saved segments")
    else:
        temporary = output.with_name(".r-" + uuid4().hex[:12])
        hasher = sha256()
        try:
            with temporary.open("xb") as destination:
                for item in receipt["segments"]:
                    with (segments_dir / item["file"]).open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            destination.write(chunk)
                            hasher.update(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if temporary.stat().st_size != length:
                raise ValueError("assembled full-region image has wrong length")
            full_hash = hasher.hexdigest()
            if full_hash != expected_hash:
                raise ValueError("assembled full-region image differs from saved segments")
            durable_replace(temporary, output)
        finally:
            if temporary.exists():
                temporary.rename(temporary.with_name(temporary.name + ".interrupted"))
    if receipt.get("sha256") not in (None, full_hash):
        raise ValueError("assembled full-region image differs from checkpoint")
    receipt["sha256"] = full_hash
    _durable_json(receipt_path, receipt)
    return {"path": str(output), "bytes": length, "sha256": full_hash,
            "receipt": str(receipt_path), "segments": len(receipt["segments"])}
