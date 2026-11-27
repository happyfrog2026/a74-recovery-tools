"""Prepare local runtime resources from user-supplied official firmware packages."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE_MANIFEST = Path(__file__).with_name("upstream_resources.json")


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_pin(data: bytes, pin: dict, name: str) -> None:
    if len(data) != pin["bytes"] or hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError(f"Resource mismatch: {name}; expected {pin['bytes']} bytes, SHA-256 {pin['sha256']}")


def expected_resources(manifest: dict) -> dict[str, dict]:
    baseline = manifest["baseline"]
    resources = {
        f"recovery_tool/stock_assets/{name}": pin
        for name, pin in baseline["stock_assets"].items()
    }
    resources.update({
        f"recovery_tool/nor_assets/sony602/{name}": pin
        for name, pin in baseline["nor_assets"].items()
    })
    for profile, variant in manifest["variants"].items():
        resources.update({
            f"recovery_tool/nor_assets/{profile}/{name}": pin
            for name, pin in variant["assets"].items()
        })
    return resources


def check_resources(manifest: dict) -> list[str]:
    issues = []
    for name, pin in expected_resources(manifest).items():
        path = ROOT / name
        if not path.is_file():
            issues.append(f"Missing {name}")
        elif path.stat().st_size != pin["bytes"] or file_hash(path) != pin["sha256"]:
            issues.append(f"Mismatch {name}; expected {pin['bytes']} bytes, SHA-256 {pin['sha256']}")
    for directory in ("stock_assets", "nor_assets"):
        name = f"recovery_tool/{directory}/provenance.json"
        if not (ROOT / name).is_file():
            issues.append(f"Missing {name}")
    return issues


def package_profile(source: Path, manifest: dict) -> tuple[str | None, dict]:
    digest = file_hash(source)
    profiles = [(None, manifest["baseline"]), *manifest["variants"].items()]
    for profile, spec in profiles:
        if digest == spec["sha256"] and source.stat().st_size == spec["bytes"]:
            return profile, spec
    raise ValueError("Unrecognized firmware package. Resource preparation requires the exact "
                     "official ILCE-7M4 6.02, 2.00 or 3.00 package identified in tools/upstream_resources.json.")


def read_member(plain: Path, index: dict, name: str, limit: int) -> bytes:
    if name not in index:
        raise ValueError(f"Required package member is missing: {name}")
    offset, size = index[name]
    if not 0 < size <= limit:
        raise ValueError(f"Package member has unexpected size: {name}: {size}")
    with plain.open("rb") as stream:
        stream.seek(offset)
        value = stream.read(size)
    if len(value) != size:
        raise ValueError(f"Truncated package member: {name}")
    return value


def read_updater_filesystem(plain: Path) -> bytes:
    with plain.open("rb") as stream:
        header = stream.read(512)
        count = struct.unpack_from("<I", header, 56)[0]
        regions = [struct.unpack_from("<4sIII", header, 64 + 16 * number)
                   for number in range(count)]
        matches = [(offset, size) for kind, offset, size, _ in regions
                   if kind.rstrip(b"\0") == b"U"]
        if len(matches) != 1:
            raise ValueError("Expected one U updater filesystem in the decoded header")
        offset, size = matches[0]
        if not 0 < size <= 32 * 1024 * 1024:
            raise ValueError(f"Unexpected U updater filesystem size: {size}")
        stream.seek(offset)
        image = stream.read(size)
    if len(image) != size or image[:4] != b"hsqs":
        raise ValueError("The U updater filesystem is not a complete SquashFS image")
    return image


def squashfs_reader():
    try:
        from PySquashfsImage import SquashFsImage
    except ImportError as exc:
        raise RuntimeError("Install the resource preparation dependencies with "
                           "python -m pip install -r requirements-resources.txt") from exc
    return SquashFsImage


def install_resources(resources: dict[str, bytes]) -> None:
    # Verify all existing destinations before changing anything. A partial previous
    # extraction is resumable when each already-written file matches its candidate.
    for name, data in resources.items():
        target = ROOT / name
        if not target.resolve().is_relative_to(ROOT):
            raise ValueError(f"Resource destination escapes the project directory: {name}")
        if target.exists() and (target.is_symlink() or not target.is_file() or target.read_bytes() != data):
            raise ValueError(f"Existing resource differs; move it aside and retry: {name}")
    for name, data in resources.items():
        target = ROOT / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".prepare-", delete=False) as stream:
                pending = Path(stream.name)
                try:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    pending.unlink(missing_ok=True)
                    raise
            try:
                os.replace(pending, target)
            finally:
                pending.unlink(missing_ok=True)
        print(f"Prepared {name} ({len(data)} bytes)")


def prepare(source: Path, manifest: dict) -> None:
    profile, spec = package_profile(source, manifest)
    print(f"Validated official ILCE-7M4 {spec['version']} input")
    from tools.upstream_firmware import decode, tar_index
    from recovery_tool.stock_fat import Fat
    reader = squashfs_reader()
    work = ROOT / ".local" / "resource-work"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        plain = Path(temporary) / "firmware.plain.bin"
        proof = decode(source, output=plain)
        if spec.get("plaintext_sha256") and proof["plaintext_sha256"] != spec["plaintext_sha256"]:
            raise ValueError("Decoded plaintext identity differs from the expected package")
        updater_image = read_updater_filesystem(plain)
        if spec.get("U_sha256") and hashlib.sha256(updater_image).hexdigest() != spec["U_sha256"]:
            raise ValueError("U updater filesystem identity differs from the expected package")
        resources = {}
        if profile is None:
            index = tar_index(plain)
            updater = read_member(plain, index, spec["updater_member"], 32 * 1024 * 1024)
            check_pin(updater, {"bytes": spec["updater_bytes"], "sha256": spec["updater_sha256"]}, spec["updater_member"])
            fat = Fat(updater)
            for name, pin in spec["stock_assets"].items():
                value = fat.read(pin["fat_path"])
                check_pin(value, pin, name)
                resources[f"recovery_tool/stock_assets/{name}"] = value
            loader = read_member(plain, index, spec["loader_member"], 4 * 1024 * 1024)
            check_pin(loader, spec["nor_assets"]["loader_nor.tar"], "loader_nor.tar")
            resources["recovery_tool/nor_assets/sony602/loader_nor.tar"] = loader
            extracted = {name: pin for name, pin in spec["nor_assets"].items() if name != "loader_nor.tar"}
            prefix = "recovery_tool/nor_assets/sony602"
        else:
            extracted = spec["assets"]
            prefix = f"recovery_tool/nor_assets/{profile}"
        with reader.from_bytes(updater_image) as image:
            for name, pin in extracted.items():
                value = image.select("/" + name).read_bytes()
                check_pin(value, pin, name)
                resources[f"{prefix}/{name}"] = value
        if profile is None:
            stock_provenance = {
                "schema": "sony-stock-local-extraction-v1", "profile": "ILCE-7M4 official 6.02",
                "source_sha256": spec["sha256"], "source_bytes": spec["bytes"],
                "decoded_plaintext_sha256": proof["plaintext_sha256"], "assets": spec["stock_assets"],
                "distribution_scope": "local user-supplied resource preparation"
            }
            nor_provenance = {
                "schema": "sony-native-nor-local-extraction-v1", "profile": "ILCE-7M4 official 6.02",
                "source_sha256": spec["sha256"], "source_bytes": spec["bytes"],
                "decoded_plaintext_sha256": proof["plaintext_sha256"], "assets": spec["nor_assets"],
                "loader_source": spec["loader_member"], "updater_variants": manifest["variants"],
                "distribution_scope": "local user-supplied resource preparation"
            }
            resources["recovery_tool/stock_assets/provenance.json"] = (json.dumps(stock_provenance, indent=2) + "\n").encode()
            resources["recovery_tool/nor_assets/provenance.json"] = (json.dumps(nor_provenance, indent=2) + "\n").encode()
        install_resources(resources)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, action="append", default=[],
                        help="User-supplied official ILCE-7M4 DAT; repeat for 6.02, 2.00 and 3.00")
    parser.add_argument("--check", action="store_true", help="Check the complete runtime resource set")
    args = parser.parse_args(argv)
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    try:
        for source in args.firmware:
            prepare(source, manifest)
        issues = check_resources(manifest)
        if issues:
            print("Local firmware resources are incomplete:", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}", file=sys.stderr)
            print("Prepare the listed DAT versions with --firmware. This tool does not prepare "
                  "Boot ROM or ESRAM captures; see docs/resources.md.", file=sys.stderr)
            return 1 if args.check or not args.firmware else 0
        print("All updater and NOR runtime resources match their expected hashes.")
        print("Boot ROM, ESRAM, RAM Fastboot and RP2040 firmware are separate prerequisites.")
        return 0
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Resource preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
