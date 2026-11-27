"""Decrypt all valid SDM2 slots from a verified local raw eMMC image."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
from uuid import uuid4

from .snapshot import captured_regions, save, sha256

ROOT = Path(__file__).resolve().parents[1]
VALID_SLOTS = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 18, 23)
SECTOR = 512


def derive_key() -> bytes:
    brom = (ROOT / "ILCE-7M4-RE/bootrom/CXD90057_Mira_0xFFFF0000.bin").read_bytes()
    loader = (ROOT / "ILCE-7M4-RE/emmc_boot/esram_0xfe040000.bin").read_bytes()
    offsets = [struct.unpack_from("<I", loader, 0x239B0 + i)[0] >> 16 for i in range(0, 0x100, 4)]
    pad = brom[0xB800:0xBC00]
    key = bytes(pad[i] for i in offsets)
    if len(key) != 64:
        raise RuntimeError("invalid derived AES-XTS key length")
    return key


def table(raw: Path) -> list[dict]:
    with raw.open("rb") as stream:
        header = stream.read(4096)
    if header[:8] != b"82462.00":
        raise RuntimeError("SDM2 magic/version mismatch")
    if len(header) < 32:
        raise RuntimeError("short SDM2 header")
    count = struct.unpack_from("<I", header, 8)[0]
    if count > (len(header) - 32) // 16:
        raise RuntimeError("SDM2 entry count outside header")
    entries = []
    for i in range(count):
        start, sectors, kind, flags = struct.unpack_from("<4I", header, 32 + i * 16)
        if flags & 1:
            entries.append({"slot": i + 1, "start_sector": start, "sectors": sectors,
                            "partition_type": kind, "bytes": sectors * SECTOR})
    if not entries:
        raise RuntimeError("SDM2 contains no valid partitions")
    by_start = sorted(entries, key=lambda x: x["start_sector"])
    cursor = 128
    for entry in by_start:
        if entry["start_sector"] < cursor or entry["sectors"] <= 0:
            raise RuntimeError("SDM2 partitions overlap or begin inside header")
        cursor = entry["start_sector"] + entry["sectors"]
        if cursor * SECTOR > raw.stat().st_size:
            raise RuntimeError("SDM2 partition extends outside raw image")
    return entries


def decrypt_one(raw: Path, out: Path, entry: dict, key: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pending = out.with_name(out.name + ".partial")
    if pending.exists() or out.exists():
        raise FileExistsError(f"decrypted output already exists: {out}")
    start, count = entry["start_sector"], entry["sectors"]
    with raw.open("rb") as source, pending.open("xb") as dest:
        source.seek(start * SECTOR)
        for i in range(count):
            block = source.read(SECTOR)
            if len(block) != SECTOR:
                raise RuntimeError("short raw sector")
            tweak = (start + i).to_bytes(16, "little")
            transform = Cipher(algorithms.AES(key), modes.XTS(tweak)).decryptor()
            dest.write(transform.update(block) + transform.finalize())
        dest.flush()
        os.fsync(dest.fileno())
    pending.chmod(0o600)
    pending.replace(out)


def _preserve_uncommitted(path: Path, manifest: dict, outdir: Path) -> None:
    """Keep crash evidence, but never adopt a file without its committed row."""
    if not path.exists():
        return
    archived = path.with_name(f"{path.name}.interrupted-{uuid4().hex}")
    path.replace(archived)
    manifest.setdefault("interrupted_files", []).append({
        "file": str(archived.relative_to(outdir)), "bytes": archived.stat().st_size,
        "preserved_utc": datetime.now(timezone.utc).isoformat(),
    })


def _validate_recorded_coverage(verified: dict, receipt: dict) -> None:
    if not verified:
        raise RuntimeError("snapshot has no captured raw evidence")
    regions = captured_regions(receipt)
    if "capture_regions" in receipt and any(
            verified.get(region, {}).get("bytes") != size for region, size in regions.items()):
        raise RuntimeError("raw manifest sizes differ from recorded capture coverage")


def decrypt(folder: Path):
    folder = Path(folder)
    raw = folder / "nflasha.raw.bin"
    verified = json.loads((folder / "verified.json").read_text())
    receipt = json.loads((folder / "capture-receipt.json").read_text())
    _validate_recorded_coverage(verified, receipt)
    if (receipt.get("status") != "VERIFIED" or
            set(verified) != set(captured_regions(receipt)) or
            set(receipt.get("reads", {})) != set(verified)):
        raise RuntimeError("double-read capture is incomplete for its recorded profile")
    for region, spec in verified.items():
        reads = receipt.get("reads", {}).get(region, {})
        for attempt, file in (("first", f"{region}.raw.bin"),
                              ("second", f"{region}.repeat.bin")):
            item = reads.get(attempt, {})
            path = folder / file
            if item.get("file") != file or item.get("bytes") != spec["bytes"] or \
               item.get("sha256") != spec["sha256"] or \
               path.stat().st_size != spec["bytes"] or sha256(path) != spec["sha256"]:
                raise RuntimeError(f"double-read evidence mismatch: {region} {attempt}")
    if "nflasha" in verified and (raw.stat().st_size != verified["nflasha"]["bytes"] or \
       sha256(raw) != verified["nflasha"]["sha256"]):
        raise RuntimeError("raw eMMC image differs from verified double-read manifest")
    outdir = folder / "decrypted"
    outdir.mkdir(mode=0o700, exist_ok=True)
    outdir.chmod(0o700)
    manifest_path = outdir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "source_snapshot": verified, "partitions": [],
        "started_utc": datetime.now(timezone.utc).isoformat()}
    if manifest.get("source_snapshot") != verified:
        raise RuntimeError("decrypted manifest belongs to another raw snapshot")
    try:
        if "nflasha" not in verified:
            raise RuntimeError("raw user-area region unavailable")
        entries = table(raw)
    except RuntimeError as exc:
        if manifest.get("partitions"):
            raise RuntimeError("previously decrypted layout is now unsupported") from exc
        manifest.update(status="VERIFIED", decryption_status="UNSUPPORTED",
                        decryption_error=str(exc), partitions=[],
                        finished_utc=datetime.now(timezone.utc).isoformat())
        save(manifest_path, manifest)
        return manifest
    manifest["decryption_status"] = "SUPPORTED"
    complete = {x["slot"]: x for x in manifest["partitions"]}
    if len(complete) != len(manifest["partitions"]):
        raise RuntimeError("duplicate decrypted slot manifest rows")
    _preserve_uncommitted(manifest_path.with_suffix(".tmp"), manifest, outdir)
    key = derive_key()
    for entry in entries:
        slot = entry["slot"]
        path = outdir / f"nflasha{slot:02d}.decrypted.bin"
        pending = path.with_name(path.name + ".partial")
        if slot in complete:
            row = complete[slot]
            if (row.get("file") != path.name or
                    any(row.get(field) != entry[field] for field in
                        ("start_sector", "sectors", "partition_type", "bytes")) or
                    path.stat().st_size != entry["bytes"] or
                    sha256(path) != row["sha256"]):
                raise RuntimeError(f"existing decrypted slot changed: {slot}")
            _preserve_uncommitted(pending, manifest, outdir)
            continue
        _preserve_uncommitted(pending, manifest, outdir)
        _preserve_uncommitted(path, manifest, outdir)
        print(f"decrypt slot {slot}: {entry['bytes']:,} bytes", flush=True)
        decrypt_one(raw, path, entry, key)
        item = dict(entry, file=path.name, sha256=sha256(path))
        manifest["partitions"].append(item)
        save(manifest_path, manifest)
        print(f"slot {slot}: {item['sha256']}", flush=True)
    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["status"] = "VERIFIED"
    save(manifest_path, manifest)
    return manifest


def verify_snapshot(folder: Path, *, progress=None) -> dict:
    """Rehash a completed double-read and its 15 derived images without writes."""
    folder = Path(folder)
    verified = json.loads((folder / "verified.json").read_text())
    receipt = json.loads((folder / "capture-receipt.json").read_text())
    _validate_recorded_coverage(verified, receipt)
    manifest = json.loads((folder / "decrypted/manifest.json").read_text())
    if receipt.get("status") != "VERIFIED" or manifest.get("status") != "VERIFIED" or \
       set(verified) != set(captured_regions(receipt)) or \
       set(receipt.get("reads", {})) != set(verified) or \
       manifest.get("source_snapshot") != verified:
        raise RuntimeError("snapshot receipts are incomplete or mismatched")
    total = 2 * sum(row['bytes'] for row in verified.values()) + sum(
        row['bytes'] for row in manifest.get('partitions', []))
    done = 0
    def report(phase):
        if progress:
            progress({'stage': 'restore_local_verify', 'phase': phase, 'bytes_done': done,
                      'bytes_total': total, 'message': f'Verifying backup snapshot ({done}/{total} bytes)'})
    def verify_hash(path):
        nonlocal done
        next_report = done + 64 * 1024 * 1024
        def advance(size):
            nonlocal done, next_report
            done += size
            if done >= next_report:
                report('progress')
                next_report = done + 64 * 1024 * 1024
        digest = sha256(path, on_bytes=advance)
        report('file_done')
        return digest
    report('start')
    for region, record in verified.items():
        for attempt, suffix in (("first", "raw"), ("second", "repeat")):
            name = f"{region}.{suffix}.bin"
            item = receipt.get("reads", {}).get(region, {}).get(attempt, {})
            path = folder / name
            if item.get("file") != name or item.get("bytes") != record["bytes"] or \
               item.get("sha256") != record["sha256"] or \
               path.stat().st_size != record["bytes"] or verify_hash(path) != record["sha256"]:
                raise RuntimeError(f"snapshot double-read mismatch: {region} {attempt}")
    if manifest.get("decryption_status") == "UNSUPPORTED":
        if manifest.get("partitions") or not manifest.get("decryption_error"):
            raise RuntimeError("invalid raw-only decryption receipt")
        report("done")
        return {"status": "VERIFIED", "raw_regions": len(verified),
                "decrypted_slots": 0, "decryption_status": "UNSUPPORTED",
                "decryption_error": manifest["decryption_error"]}
    entries = {row["slot"]: row for row in table(folder / "nflasha.raw.bin")}
    rows = manifest.get("partitions", [])
    if len(rows) != len(entries) or {row["slot"] for row in rows} != set(entries):
        raise RuntimeError("decrypted slot manifest is incomplete")
    for row in rows:
        expected = entries[row["slot"]]
        name = f"nflasha{row['slot']:02d}.decrypted.bin"
        path = folder / "decrypted" / name
        if row.get("file") != name or any(row.get(key) != expected[key]
                                              for key in ("start_sector", "sectors",
                                                          "partition_type", "bytes")) or \
           path.stat().st_size != expected["bytes"] or verify_hash(path) != row["sha256"]:
            raise RuntimeError(f"decrypted slot mismatch: {row['slot']}")
    report("done")
    return {"status": "VERIFIED", "raw_regions": len(verified),
            "decrypted_slots": len(entries), "decryption_status": "SUPPORTED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline decrypt verified snapshot SDM2 slots")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    decrypt(args.folder)


if __name__ == "__main__":
    main()
