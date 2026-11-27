"""Explicit, evidence-producing installation of the pinned David RP2040 bridge."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
from datetime import datetime, timezone


UF2_RELATIVE = Path("ILCE-7M4-RE/tools/uart_boot/build/uart_boot.ino.uf2")
UF2_SHA256 = "f486230d8e527b5791cef127b823640f8cf011986918c92f23bd56575dd86bfe"
UF2_FAMILY_RP2040 = 0xE48BFF56
KNOWN_PINGS = (b"uart_boot 1", b"uart_boot 2")


class BoardInstallError(RuntimeError):
    pass


def default_uf2() -> Path:
    return Path(__file__).resolve().parent.parent / UF2_RELATIVE


def validate_uf2(path: Path) -> dict:
    """Validate the entire pinned binary before any RP2040 write."""
    path = path.expanduser().resolve()
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != UF2_SHA256:
        raise BoardInstallError(f"UF2 SHA-256 differs from the reviewed David build: {digest}")
    if not data or len(data) % 512:
        raise BoardInstallError("UF2 size is invalid")
    count = len(data) // 512
    addresses = set()
    for number in range(count):
        block = data[number * 512:(number + 1) * 512]
        magic0, magic1, flags, address, size, index, total, family = struct.unpack_from("<8I", block)
        end_magic = struct.unpack_from("<I", block, 508)[0]
        if (magic0, magic1, end_magic) != (0x0A324655, 0x9E5D5157, 0x0AB16F30):
            raise BoardInstallError(f"UF2 block {number} has invalid magic")
        if flags != 0x2000 or family != UF2_FAMILY_RP2040 or index != number or total != count:
            raise BoardInstallError(f"UF2 block {number} has invalid RP2040 metadata")
        if size != 256 or address < 0x10000000 or address + size > 0x11000000 or address % 256:
            raise BoardInstallError(f"UF2 block {number} has invalid flash range")
        if address in addresses:
            raise BoardInstallError(f"UF2 block {number} repeats a flash address")
        addresses.add(address)
    return {"path": str(path), "sha256": digest, "bytes": len(data), "blocks": count}


def _mount_candidates() -> list[Path]:
    candidates: set[Path] = set()
    if sys.platform == "darwin":
        candidates.update(Path("/Volumes").glob("RPI-RP2*"))
    elif os.name == "nt":
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            candidates.add(Path(f"{letter}:/"))
    else:
        for table in (Path("/proc/self/mounts"), Path("/etc/mtab")):
            try:
                lines = table.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) >= 2:
                    mountpoint = fields[1].replace("\\040", " ")
                    if Path(mountpoint).name.upper().startswith("RPI-RP2"):
                        candidates.add(Path(mountpoint))
            break
        user = os.environ.get("USER", "")
        for base in (Path("/media") / user, Path("/run/media") / user):
            candidates.update(base.glob("RPI-RP2*"))
    return sorted(candidates)


def confirm_bootsel(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise BoardInstallError(f"BOOTSEL volume does not exist: {path}")
    info = path / "INFO_UF2.TXT"
    try:
        with info.open("rb") as stream:
            raw = stream.read(4097)
    except OSError as exc:
        raise BoardInstallError(f"Cannot read BOOTSEL identity at {info}: {exc}") from exc
    if len(raw) > 4096:
        raise BoardInstallError("BOOTSEL identity file is unexpectedly large")
    text = raw.decode("utf-8", "replace")
    if not re.search(r"(?m)^Board-ID:\s*RPI-RP2\s*$", text):
        raise BoardInstallError(f"Volume is not confirmed as RP2040 BOOTSEL: {path}")
    if not re.search(r"(?m)^Model:\s*(?:Raspberry Pi RP2|.*RP2040.*)\s*$", text):
        raise BoardInstallError("BOOTSEL identity lacks RP2040/RP2 model")
    return {"volume": str(path), "board_id": "RPI-RP2", "info": text.strip()}


def bootsel_volumes(explicit: Path | None = None) -> list[dict]:
    candidates = [explicit] if explicit else _mount_candidates()
    found = []
    for path in candidates:
        try:
            found.append(confirm_bootsel(path))
        except BoardInstallError:
            if explicit:
                raise
    return found


def _ports() -> list[str]:
    from .bridge import list_ports
    return list_ports()


def _ping(port: str) -> bytes:
    from .bridge import Bridge
    with Bridge(port) as bridge:
        return bridge.ping()


def inspect(*, volume: Path | None = None, port: str | None = None) -> dict:
    """Discover without sending commands or writing the board."""
    result = {"uf2": validate_uf2(default_uf2()), "bootsel": bootsel_volumes(volume)}
    try:
        result["ports"] = [port] if port else _ports()
    except (ImportError, RuntimeError) as exc:
        result["ports"] = []
        result["port_discovery_error"] = str(exc)
    return result


def _receipt(evidence_dir: Path, evidence: dict) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = evidence_dir / f"board-install-{stamp}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return path


def install(*, uf2: Path | None = None, volume: Path | None = None,
            port: str | None = None, evidence_dir: Path | None = None,
            wait_seconds: float = 30) -> dict:
    """Explicitly install only on a confirmed BOOTSEL volume; PING proves success."""
    evidence = {"started_utc": datetime.now(timezone.utc).isoformat(),
                "uf2": validate_uf2(uf2 or default_uf2()), "events": []}
    evidence_dir = evidence_dir or Path.cwd() / "tasks" / "board-install-evidence"

    def finish(status: str, error: str | None = None) -> dict:
        evidence["status"] = status
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        if error:
            evidence["error"] = error
        evidence["receipt"] = str(_receipt(evidence_dir, evidence))
        return evidence

    try:
        ports = [port] if port else _ports()
        evidence["initial_ports"] = ports
        if len(ports) > 1:
            raise BoardInstallError("Multiple RP2040 CDC ports; specify --port after identifying the board")
        if ports:
            try:
                version = _ping(ports[0])
            except Exception as exc:
                evidence["events"].append({"ping_port": ports[0], "result": "failed", "detail": str(exc)})
            else:
                evidence["events"].append({"ping_port": ports[0], "response": version.decode("ascii", "replace")})
                if version in KNOWN_PINGS:
                    return finish("already_installed")
        volumes = bootsel_volumes(volume)
        evidence["bootsel"] = volumes
        if len(volumes) != 1:
            raise BoardInstallError(f"Expected one confirmed RP2040 BOOTSEL volume; found {len(volumes)}")
        destination = Path(volumes[0]["volume"]) / "A7M4.UF2"
        source = Path(evidence["uf2"]["path"])
        # A single raw write avoids macOS cp copying extended attributes to USB MSC.
        # Read the same bytes validated above immediately before writing.
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != UF2_SHA256:
            raise BoardInstallError("UF2 changed after validation")
        evidence["events"].append({"write_started_utc": datetime.now(timezone.utc).isoformat(),
                                   "destination": str(destination), "bytes": len(data)})
        try:
            with destination.open("wb") as stream:
                written = stream.write(data)
                if written != len(data):
                    raise BoardInstallError(f"Short UF2 write: {written}/{len(data)}")
                stream.flush()
                os.fsync(stream.fileno())
            evidence["events"].append({"write_result": "completed"})
        except OSError as exc:
            evidence["events"].append({"write_result": "OS error", "detail": str(exc)})
            # A disconnect during fsync may mean successful transfer. Only PING can decide.
        deadline = time.monotonic() + wait_seconds
        last_error = "No RP2040 CDC port appeared"
        while time.monotonic() < deadline:
            current = [port] if port else _ports()
            if len(current) > 1:
                raise BoardInstallError("Multiple RP2040 CDC ports appeared; cannot attribute PING to the installed board")
            if current:
                try:
                    version = _ping(current[0])
                except Exception as exc:
                    last_error = str(exc)
                else:
                    evidence["events"].append({"ping_port": current[0], "response": version.decode("ascii", "replace")})
                    if version in KNOWN_PINGS:
                        return finish("installed_and_verified")
                    last_error = f"Unexpected PING response: {version!r}"
            time.sleep(0.25)
        raise BoardInstallError(f"UF2 write outcome unverified: {last_error}")
    except (Exception, KeyboardInterrupt) as exc:
        finish("failed", str(exc))
        raise BoardInstallError(f"{exc} (receipt: {evidence['receipt']})") from exc
