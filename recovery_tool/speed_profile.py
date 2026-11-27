"""Optional local output-volume speed estimate; never touches a camera."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

PROFILE_NAME = "recovery-speed-profile.json"
SAMPLE_BYTES = 8 * 1024 * 1024
RECHECK_BYTES = 1 * 1024 * 1024
MAX_AGE_SECONDS = 24 * 3600


class SpeedProfileError(RuntimeError):
    pass


def _measure(directory: Path, size: int) -> dict:
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise SpeedProfileError("Speed test folder does not exist")
    payload = os.urandom(min(size, 1024 * 1024))
    digest = hashlib.sha256()
    path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".recovery-speed-", dir=directory,
                                         delete=False) as stream:
            path = Path(stream.name)
            started = time.monotonic()
            remaining = size
            while remaining:
                chunk = payload[:min(remaining, len(payload))]
                stream.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            write_seconds = max(time.monotonic() - started, 1e-6)
        started = time.monotonic()
        verified = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                verified.update(chunk)
        read_seconds = max(time.monotonic() - started, 1e-6)
        if verified.digest() != digest.digest():
            raise SpeedProfileError("Speed test file readback checksum differs")
        return {"bytes": size, "write_mib_s": round(size / 1048576 / write_seconds, 2),
                "read_mib_s": round(size / 1048576 / read_seconds, 2)}
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def create_profile(directory: Path, size: int = SAMPLE_BYTES) -> dict:
    directory = Path(directory).expanduser().resolve()
    if not 1024 * 1024 <= size <= 64 * 1024 * 1024:
        raise SpeedProfileError("Speed test size must be between 1 and 64 MiB")
    measurement = _measure(directory, size)
    profile = {"schema": "local-speed-profile-v1", "directory": str(directory),
               "volume_device": directory.stat().st_dev,
               "created_utc": datetime.now(timezone.utc).isoformat(), **measurement,
               "camera_speed_measured": False}
    path = directory / PROFILE_NAME
    pending = path.with_suffix(".tmp")
    with pending.open("w", encoding="utf-8") as stream:
        json.dump(profile, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.chmod(0o600)
    pending.replace(path)
    return profile


def revalidate(directory: Path, *, tolerance: float = 0.25) -> dict:
    directory = Path(directory).expanduser().resolve()
    path = directory / PROFILE_NAME
    if not path.is_file():
        raise SpeedProfileError("No saved speed profile exists in this folder")
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("schema") != "local-speed-profile-v1" or \
       profile.get("directory") != str(directory) or \
       profile.get("volume_device") != directory.stat().st_dev:
        raise SpeedProfileError("Speed profile belongs to another folder or volume")
    created = datetime.fromisoformat(profile["created_utc"])
    if (datetime.now(timezone.utc) - created).total_seconds() > MAX_AGE_SECONDS:
        raise SpeedProfileError("Speed profile has expired")
    current = _measure(directory, RECHECK_BYTES)
    if current["write_mib_s"] < profile["write_mib_s"] * tolerance or \
       current["read_mib_s"] < profile["read_mib_s"] * tolerance:
        raise SpeedProfileError("Current folder speed is far below the saved profile")
    return {"profile": profile, "recheck": current, "valid": True,
            "camera_speed_measured": False}
