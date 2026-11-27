"""Scoped, journaled nflasha1 restoration. Never runs a write without `apply`."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct

from .stock_fat import Fat
from .transport import FastbootTransport, FastbootError

REGION = "nflasha1"
SIZE = 25_100_288
SECTOR = 512
MAX_DOWNLOAD = 512 * 1024
CURRENT_SHA256 = "591a446c5a796625790b77b71149907a34c5d013396f0ccef056360f6bc55735"
# Stock updater after Sony's successful 6.02 reinstall, extracted/decrypted
# from this camera's fully verified post-recovery raw eMMC snapshot.
OEM_SHA256 = "7388d13ddeba89678cfc76b9f01a3112128df490bc99a702d31011e1eed1f870"
SDM2_SECTOR0_SHA256 = "a377bf79619d392474213f3c9229e090b047c60338826b38e85523481ccfc282"
SETTINGS_SHA256 = "ac9d091099ef02254d545b1e71a1c288cd6d1311a48326e6b2699f5d06277d63"
SETTINGS_SIZE = 20_971_520
SPARSE_MAGIC = 0xED26FF3A
RAW = 0xCAC1
SKIP = 0xCAC3
MAX_RAW_SECTORS = (MAX_DOWNLOAD - 28 - 3 * 12) // SECTOR


class RestoreError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_images(current: bytes, target: bytes, expected_current: str, expected_target: str):
    if len(current) != SIZE or sha256(current) != expected_current:
        raise RestoreError("current image is not the reviewed same-camera preflight")
    if len(target) != SIZE or sha256(target) != expected_target:
        raise RestoreError("target image is not the reviewed same-camera image")
    if current[:3] != b"\xeb\x3c\x90" or current[510:512] != b"\x55\xaa":
        raise RestoreError("current image lacks expected FAT boot signature")
    if target[:3] != b"\xeb\x3c\x90" or target[510:512] != b"\x55\xaa":
        raise RestoreError("target image lacks FAT boot signature")


def _chunk(kind: int, sectors: int, payload: bytes = b"") -> bytes:
    return struct.pack("<HHII", kind, 0, sectors, 12 + len(payload)) + payload


@dataclass(frozen=True)
class Fragment:
    start_sector: int
    sectors: int
    data: bytes


def fragments(current: bytes, target: bytes, expected_current: str | None = None,
              expected_target: str | None = None) -> list[Fragment]:
    """Build minimal RAW ranges plus DONT_CARE gaps; never writes unchanged sectors."""
    expected_current = CURRENT_SHA256 if expected_current is None else expected_current
    expected_target = OEM_SHA256 if expected_target is None else expected_target
    _check_images(current, target, expected_current, expected_target)
    indices = [i for i in range(SIZE // SECTOR)
               if current[i * SECTOR:(i + 1) * SECTOR] != target[i * SECTOR:(i + 1) * SECTOR]]
    result = []
    at = 0
    while at < len(indices):
        first, last = indices[at], indices[at]
        at += 1
        while at < len(indices) and indices[at] == last + 1 and last - first + 1 < MAX_RAW_SECTORS:
            last = indices[at]
            at += 1
        count = last - first + 1
        chunks = []
        if first:
            chunks.append(_chunk(SKIP, first))
        chunks.append(_chunk(RAW, count, target[first * SECTOR:(last + 1) * SECTOR]))
        tail = SIZE // SECTOR - first - count
        if tail:
            chunks.append(_chunk(SKIP, tail))
        header = struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12, SECTOR,
                             SIZE // SECTOR, len(chunks), 0)
        data = header + b"".join(chunks)
        if len(data) > MAX_DOWNLOAD:
            raise AssertionError("sparse fragment exceeds payload download limit")
        result.append(Fragment(first, count, data))
    return result


def replay(current: bytes, parts: list[Fragment]) -> bytes:
    """Independent bounded replay of generated sparse fragments for offline QA."""
    output = bytearray(current)
    total_sectors = len(current) // SECTOR
    for part in parts:
        img = part.data
        magic, major, minor, hdr, chunk_hdr, block, total, count, _ = struct.unpack_from("<IHHHHIIII", img)
        if (magic, major, hdr, chunk_hdr, block, total) != (SPARSE_MAGIC, 1, 28, 12, SECTOR, total_sectors):
            raise RestoreError("bad sparse header")
        pos, offset = hdr, 0
        for _ in range(count):
            kind, _, n, size = struct.unpack_from("<HHII", img, pos)
            if size < 12 or pos + size > len(img) or offset + n > total:
                raise RestoreError("bad sparse chunk")
            if kind == RAW:
                data = img[pos + 12:pos + size]
                if len(data) != n * SECTOR:
                    raise RestoreError("bad RAW length")
                output[offset * SECTOR:(offset + n) * SECTOR] = data
            elif kind != SKIP or size != 12:
                raise RestoreError("unexpected sparse chunk")
            offset += n
            pos += size
        if pos != len(img) or offset != total:
            raise RestoreError("incomplete sparse fragment")
    return bytes(output)


def build_plan(current: bytes, target: bytes, *, reverse: bool = False) -> tuple[list[Fragment], dict]:
    current_hash, target_hash = (OEM_SHA256, CURRENT_SHA256) if reverse else (CURRENT_SHA256, OEM_SHA256)
    parts = fragments(current, target, current_hash, target_hash)
    if replay(current, parts) != target:
        raise RestoreError("offline sparse replay differs from target")
    manifest = {
        "region": REGION, "partition_bytes": SIZE,
        "current_sha256": current_hash, "target_sha256": target_hash,
        "direction": "reapply-reviewed-current" if reverse else "restore-post-recovery-OEM",
        "changed_sectors": sum(p.sectors for p in parts),
        "fragment_count": len(parts),
        "fragments": [{"start_sector": p.start_sector, "sectors": p.sectors,
                       "bytes": len(p.data), "sha256": sha256(p.data)} for p in parts],
        "replay_equals_target": True,
        "write_authorized_by_this_plan": False,
    }
    return parts, manifest


def _download_flash(transport: FastbootTransport, part: Fragment, region: str = REGION):
    if region not in ("nflasha1", "nflasha2"):
        raise RestoreError("unsupported scoped write region")
    data = part.data
    if len(data) > MAX_DOWNLOAD:
        raise RestoreError("fragment exceeds 512 KiB")
    transport._send(f"download:{len(data):08x}")
    _, length = transport._reply((b"DATA",))
    if length != f"{len(data):08x}".encode():
        raise RestoreError("camera requested a different download length")
    for offset in range(0, len(data), 65536):
        chunk = data[offset:offset + 65536]
        if transport.backend.write(chunk, 3500) != len(chunk):
            raise RestoreError("short USB download write")
    transport._reply()
    transport._send(f"flash:{region}")
    expected = f"wrote {part.sectors} sectors to {region}".encode()
    wrote_seen = False
    for _ in range(128):
        response = transport.backend.read(64, 3500)
        if len(response) < 4 or len(response) > 64:
            raise RestoreError("invalid flash response length")
        tag, value = response[:4], response[4:]
        if tag == b"INFO":
            wrote_seen |= value == expected
            continue
        if tag == b"FAIL":
            raise RestoreError("camera rejected flash: " + value.decode("utf-8", "replace"))
        if tag != b"OKAY" or not wrote_seen:
            raise RestoreError("camera did not confirm expected sector write")
        break
    else:
        raise RestoreError("flash response lacked completion")


def _journal(path: Path, receipt: dict):
    pending = path.with_suffix(".tmp")
    with pending.open("w") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.chmod(0o600)
    pending.replace(path)


def _verify_sdm2_sector(sector0: bytes):
    if len(sector0) != SECTOR or sha256(sector0) != SDM2_SECTOR0_SHA256:
        raise RestoreError("live SDM2 sector0 differs from reviewed same-camera mapping")
    if sector0[:4] != b"8246":
        raise RestoreError("live SDM2 magic differs")
    start, count, kind, flags = struct.unpack_from("<4I", sector0, 32)
    if (start, count, kind, flags & 1) != (128, SIZE // SECTOR, 5, 1):
        raise RestoreError("live nflasha1 SDM2 mapping differs")


def _settings_files(fs: Fat) -> dict[str, bytes]:
    found = {}

    def walk(first=None, prefix=""):
        for name, (_, cluster, _, attr) in fs.entries(first).items():
            if name in (".", ".."):
                continue
            path = prefix + name
            if attr & 16:
                walk(cluster, path + "/")
            else:
                found[path] = fs.read(path)

    walk()
    return found


def validate_settings(baseline: bytes, live: bytes, *,
                      expected_sha: str | None = None,
                      expected_versions: tuple[str, ...] | None = ("6.04",)) -> dict:
    """Accept only mutable Backup.bin/.bak data, never updater flags or FAT changes."""
    expected_sha = SETTINGS_SHA256 if expected_sha is None else expected_sha
    if len(baseline) != SETTINGS_SIZE or sha256(baseline) != expected_sha:
        raise RestoreError("settings baseline differs from pinned preflight")
    if len(live) != SETTINGS_SIZE:
        raise RestoreError("live settings partition size changed")
    old, new = Fat(baseline), Fat(live)
    if baseline[:old.data_start] != live[:new.data_start]:
        raise RestoreError("settings FAT or directory metadata changed")
    old_files, new_files = _settings_files(old), _settings_files(new)
    if old_files.keys() != new_files.keys():
        raise RestoreError("settings file list changed")
    version_raw = new.read("updater/dat4")
    from .stock_sd import decode_version, version_text
    version = version_text(decode_version(version_raw))
    if expected_versions is not None and version not in expected_versions:
        raise RestoreError("settings recorded version is outside reviewed profile")
    updater = new.entries(new.entries()["UPDATER"][1])
    if any(name in updater for name in ("MODE", "MODE1", "MODE2", "MODE3", "MODE4",
                                        "MODE5", "MODE6", "MODE7", "UINCOMP")):
        raise RestoreError("settings contain a pending update mode")
    allowed_paths = {"BACKUP.BIN", "BACKUP.BAK"} & old_files.keys()
    if not allowed_paths:
        raise RestoreError("settings backup files missing")
    if any(old_files[path] != new_files[path] for path in old_files.keys() - allowed_paths):
        raise RestoreError("settings changed outside backup files")
    allowed_clusters = set()
    for name in allowed_paths:
        _, first, size, _ = old.locate(name)
        _, new_first, new_size, _ = new.locate(name)
        if first != new_first or size != new_size:
            raise RestoreError("settings backup allocation changed")
        allowed_clusters.update(old.chain(first))
    changed = [i for i, (a, b) in enumerate(zip(baseline, live)) if a != b]
    if any(i < old.data_start or
           2 + (i - old.data_start) // old.cluster_size not in allowed_clusters for i in changed):
        raise RestoreError("settings changed outside backup data clusters")
    return {"baseline_sha256": expected_sha, "live_sha256": sha256(live),
            "changed_bytes": len(changed), "changed_sectors": len({i // SECTOR for i in changed}),
            "allowed_mutable_files": sorted(allowed_paths), "version": version,
            "no_update_mode": True}


def apply(source: Path, expected_current: Path, settings_baseline: Path, evidence_dir: Path,
          transport_factory=FastbootTransport, *, reverse: bool = False) -> dict:
    """Explicit physical restore; caller must separately approve exact target compatibility."""
    source, expected_current = Path(source), Path(expected_current)
    current, target = expected_current.read_bytes(), source.read_bytes()
    parts, plan = build_plan(current, target, reverse=reverse)
    baseline = Path(settings_baseline).read_bytes()
    if len(baseline) != SETTINGS_SIZE or sha256(baseline) != SETTINGS_SHA256:
        raise RestoreError("settings baseline is not the reviewed 6.04 preflight")
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    receipt_path = evidence_dir / "restore-receipt.json"
    receipt = {"started_utc": datetime.now(timezone.utc).isoformat(), "status": "PREWRITE_READ",
               "plan": plan, "fragments_confirmed": 0, "camera_write_may_have_occurred": False}
    _journal(receipt_path, receipt)
    try:
        with transport_factory() as transport:
            sector0 = evidence_dir / "sector0.prewrite.bin"
            transport.read_region("nflasha", sector0, length=SECTOR)
            sector0.chmod(0o600)
            _verify_sdm2_sector(sector0.read_bytes())
            if transport.region_size("nflasha2") != SETTINGS_SIZE:
                raise RestoreError("live settings partition size changed")
            settings = evidence_dir / "settings.prewrite.bin"
            transport.read_region("nflasha2", settings)
            settings.chmod(0o600)
            receipt["settings_semantics"] = validate_settings(baseline, settings.read_bytes())
            _journal(receipt_path, receipt)
            if transport.region_size(REGION) != SIZE:
                raise RestoreError("live nflasha1 size changed")
            prewrite = evidence_dir / "updater.prewrite.bin"
            transport.read_region(REGION, prewrite)
            prewrite.chmod(0o600)
            if sha256(prewrite.read_bytes()) != plan["current_sha256"]:
                raise RestoreError("live updater differs from reviewed current backup")
            receipt["status"] = "WRITE_STARTED"
            for index, part in enumerate(parts):
                receipt["inflight_fragment"] = index
                receipt["camera_write_may_have_occurred"] = True
                _journal(receipt_path, receipt)
                _download_flash(transport, part)
                receipt["fragments_confirmed"] = index + 1
                del receipt["inflight_fragment"]
                _journal(receipt_path, receipt)
            receipt["status"] = "READBACK_STARTED"
            _journal(receipt_path, receipt)
            readback = evidence_dir / "updater.readback.bin"
            transport.read_region(REGION, readback)
            readback.chmod(0o600)
            if sha256(readback.read_bytes()) != plan["target_sha256"]:
                raise RestoreError("full readback differs from target; state unknown")
        receipt["status"] = "VERIFIED"
        receipt["readback_sha256"] = plan["target_sha256"]
    except BaseException as exc:
        receipt["status"] = "UNKNOWN_AFTER_WRITE" if receipt["camera_write_may_have_occurred"] else "NO_WRITE"
        receipt["error"] = str(exc)
        _journal(receipt_path, receipt)
        raise
    receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
    _journal(receipt_path, receipt)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scoped nflasha1 OEM restore; explicit apply only")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply"):
        cmd = sub.add_parser(action)
        cmd.add_argument("source", type=Path, help="reviewed post-recovery nflasha01.decrypted.bin")
        cmd.add_argument("current", type=Path, help="fresh full current updater backup")
        cmd.add_argument("--reverse", action="store_true", help="reapply the reviewed modified updater after OEM restore")
        if action == "apply":
            cmd.add_argument("settings_baseline", type=Path, help="reviewed 6.04 settings preflight")
            cmd.add_argument("evidence_dir", type=Path, help="new private directory for prewrite/readback/journal")
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            _, result = build_plan(args.current.read_bytes(), args.source.read_bytes(), reverse=args.reverse)
        else:
            result = apply(args.source, args.current, args.settings_baseline, args.evidence_dir,
                           reverse=args.reverse)
        print(json.dumps(result, indent=2))
        return 0
    except (RestoreError, FastbootError, OSError, ValueError) as exc:
        parser.exit(1, f"restore: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
