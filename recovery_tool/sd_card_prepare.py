"""Safely copy a selected DAT to a user-selected removable SD volume."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
import plistlib
import shutil
import subprocess
from uuid import uuid4

from .stock_profile_data import CATALOG
from .sd_package_catalog import SD_CAMERA_MODELS, SD_PACKAGES
from .stock_sd import file_sha256
from .write_transaction import durable_json


VOLUME_ROOT = Path("/Volumes")


# SHA-256 and byte counts of Sony's original ILCE-7RM5 SD downloads.
# Keep these separate from the ILCE-7M4 guided recovery profiles.
RM5_PACKAGES = {
    "6c261f49a713bc7908a0e1eae60411f6cd4821bd39304c82c9ee9078b8392965": ("2.00", 954530216),
    "db51f55a34c730884b161ff755a790768943e18f70f1e275c1bea37c5372ba6a": ("2.01", 953576872),
    "2de11d9eea27f09f9ac39cffe90c628070b93e8b08128f52d2af6f1230b41fbb": ("2.02", 954185128),
    "265e396a3a2a452778f6caa91a21a2d5822322465ec7467dbd8443fd59ef19e3": ("3.00", 973894056),
    "5fd37318b29351a425364939106d7c0129447fa71e9eb72464e1c7f380c77a08": ("3.01", 975094184),
    "51e1176a255a9a4b4782dfbcfa6c5f332b71173bdfe9ed2bcf4fca6bcbc6c2e2": ("4.00", 754525608),
}


def package_metadata(digest: str, size: int) -> dict:
    m4 = CATALOG["packages"].get(digest)
    if m4 and m4["bytes"] == size:
        return {"status": "catalog_match", "model": "ILCE-7M4", "version": m4["version"]}
    rm5 = RM5_PACKAGES.get(digest)
    if rm5 and rm5[1] == size:
        return {"status": "catalog_match", "model": "ILCE-7RM5", "version": rm5[0]}
    package = SD_PACKAGES.get(digest)
    if package and package["bytes"] == size:
        return {"status": "catalog_match", "model": package["model"], "version": package["version"]}
    return {"status": "not_parsed", "model": None, "version": None}


def _inspect_volume(volume: Path) -> str:
    """Return a stable identifier for a writable mounted removable card volume."""
    root = VOLUME_ROOT.resolve(strict=True)
    if volume.parent != root or volume.is_symlink() or not volume.is_dir():
        raise ValueError("Select the root of one mounted SD or USB card volume")
    completed = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(volume)],
                               check=True, capture_output=True)
    info = plistlib.loads(completed.stdout)
    # A built-in SD reader reports Internal=true even though its card is
    # removable. Disk images can report removable too, so require a physical
    # bus and a writable, ejectable mounted volume.
    if (Path(info.get("MountPoint", "")).resolve() != volume or
            info.get("BusProtocol") not in {"Secure Digital", "USB"} or
            info.get("RemovableMediaOrExternalDevice") is not True or
            info.get("Ejectable") is not True or
            info.get("WritableVolume") is not True):
        raise ValueError("Selected path is not a writable mounted SD or USB volume")
    identifier = info.get("DeviceIdentifier")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("External volume identity is unavailable")
    return identifier


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    with source.open("rb") as origin, destination.open("xb") as output:
        shutil.copyfileobj(origin, output, length=4 << 20)
        output.flush()
        os.fsync(output.fileno())
    if file_sha256(destination) != expected_sha256:
        raise ValueError("Copied file differs from its verified source")


def prepare(source_dat: str | Path, volume: str | Path, *,
            camera_model: str | None = None,
            confirm_replace: bool = False,
            expected_existing_sha256: str | None = None,
            expected_volume_device_identifier: str | None = None,
            volume_inspector=_inspect_volume,
            receipt_dir: Path | None = None) -> dict:
    source = Path(source_dat).expanduser().resolve(strict=True)
    card = Path(volume).expanduser().resolve(strict=True)
    identifier = volume_inspector(card)
    if source.suffix.lower() != ".dat" or not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Select a nonempty DAT file")
    source_size = source.stat().st_size
    source_hash = file_sha256(source)
    if source.stat().st_size != source_size:
        raise ValueError("Selected DAT size changed during source hashing")
    metadata = package_metadata(source_hash, source_size)
    if camera_model is not None and camera_model not in SD_CAMERA_MODELS:
        raise ValueError("Unknown SD update camera model")
    destination = card / "BODYDATA.DAT"
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError("Card BODYDATA.DAT is not a regular file")
    existing_hash = file_sha256(destination) if destination.exists() else None
    base = {"source_dat_sha256": source_hash, "card_dat_sha256": existing_hash,
            "existing_sha256": existing_hash, "volume_device_identifier": identifier,
            "volume": str(card), "sd_slot_1_presence_verified": False,
            "requires_confirmation": False,
            "source_path": str(source), "source_sha256": source_hash,
            "source_bytes": source_size, "target_path": str(destination),
            "target_sha256": existing_hash,
            "target_bytes": destination.stat().st_size if destination.exists() else None,
            "volume_identity": {"device_identifier": identifier, "mount_point": str(card)},
            "dat_metadata": metadata, "selected_camera_model": camera_model,
            "readback_verified": False, "readback_utc": None, "copy_replaced": False}
    receipt_path = None
    if receipt_dir is not None:
        receipt_dir = Path(receipt_dir)
        receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt_path = receipt_dir / f"sd-card-prepare-{uuid4().hex}.json"
        durable_json(receipt_path, {"schema": "sd-card-prepare-v1", "status": "PREPARED",
                                    "started_utc": datetime.now(timezone.utc).isoformat(), **base})

    def done(fields: dict) -> dict:
        result = {**base, **fields}
        if receipt_path is not None:
            durable_json(receipt_path, {"schema": "sd-card-prepare-v1", **result,
                                        "finished_utc": datetime.now(timezone.utc).isoformat()})
            result["receipt_path"] = str(receipt_path)
        return result
    if camera_model and metadata["model"] and metadata["model"] != camera_model:
        return done({"status": "MODEL_MISMATCH",
                     "summary": f"Selected {camera_model}, DAT is {metadata['model']} {metadata['version']}"})
    selected_model = camera_model or metadata["model"]
    card_slot = "B" if selected_model == "ILME-FX6" else "1"
    update_instruction = f"Safely eject the card and insert it in camera slot {card_slot}"
    if existing_hash == source_hash:
        return done({"status": "ALREADY_PRESENT", "readback_verified": True,
                     "readback_utc": datetime.now(timezone.utc).isoformat(),
                     "summary": "Selected DAT bytes are already at the SD root",
                     "next_action": update_instruction})
    if existing_hash is not None:
        if not confirm_replace:
            return done({"status": "NEEDS_CONFIRMATION", "requires_confirmation": True,
                         "summary": "A different BODYDATA.DAT is present on the selected card",
                         "next_action": "Confirm replacing the existing card file"})
        if (expected_existing_sha256 != existing_hash or
                expected_volume_device_identifier != identifier):
            raise ValueError("Card file or volume changed since confirmation; inspect again")
    elif confirm_replace and expected_volume_device_identifier != identifier:
        raise ValueError("Volume changed since confirmation; inspect again")
    if shutil.disk_usage(card).free < source.stat().st_size:
        raise ValueError("Selected SD volume lacks space for a staged DAT copy")

    temporary = card / f".BODYDATA.DAT.tmp-{uuid4().hex}"
    replaced = False
    try:
        _copy_verified(source, temporary, source_hash)
        if temporary.stat().st_size != source_size:
            raise ValueError("Staged DAT size differs from selected source")
        # Recheck the card and existing file immediately before the only replace.
        if volume_inspector(card) != identifier or \
           (file_sha256(destination) if destination.exists() else None) != existing_hash:
            raise ValueError("Card or existing DAT changed during copy; no replacement made")
        os.replace(temporary, destination)
        replaced = True
        if destination.stat().st_size != source_size or file_sha256(destination) != source_hash:
            raise ValueError("Card DAT readback differs from the selected source")
    except BaseException as exc:
        if receipt_path is not None:
            failed = dict(base)
            if replaced:
                failed["target_sha256"] = None
                failed["target_bytes"] = None
                failed["card_dat_sha256"] = None
            durable_json(receipt_path, {"schema": "sd-card-prepare-v1", **failed,
                                        "status": "FAILED", "failure_cause": str(exc),
                                        "copy_replaced": replaced,
                                        "finished_utc": datetime.now(timezone.utc).isoformat()})
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return done({"status": "VERIFIED", "card_dat_sha256": source_hash,
                 "target_sha256": source_hash, "target_bytes": destination.stat().st_size,
                 "readback_verified": True, "readback_utc": datetime.now(timezone.utc).isoformat(),
                 "copy_replaced": True,
                 "summary": "Selected DAT copied and read back byte for byte at the SD root",
                 "next_action": update_instruction})
