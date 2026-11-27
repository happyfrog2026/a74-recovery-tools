"""Exact, complete Sony SD boot environments; no hardware or external disk I/O."""
from __future__ import annotations
import hashlib
from pathlib import Path

from .stock_common import require
from .stock_fat import Fat
from .stock_profile_data import CATALOG

BOOT_FILES = ("boot/initrd.img", "boot/vmlinux.bin", "boot/cxd90057.dtb",
              "boot/kemco.txt", "boot/loadaddr.txt", "p1chksum")


def package_for_hash(digest):
    return CATALOG['packages'].get(digest)


def inspect_package(path, *, camera_model="ILCE-7M4"):
    path = Path(path)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if camera_model == "ILCE-7RM5":
        from .sd_card_prepare import package_metadata
        metadata = package_metadata(digest, path.stat().st_size)
        require(metadata["model"] == camera_model, "DAT_PROFILE",
                "Selected DAT is not a verified ILCE-7RM5 package")
        return {"sha256": digest, "bytes": path.stat().st_size, **metadata}
    package = package_for_hash(digest)
    require(package is not None and path.stat().st_size == package['bytes'],
            'DAT_PROFILE', 'Sony DAT is not one of the verified official packages')
    return {'sha256': digest, **package}


def inspect_environment(image, catalogue=CATALOG, *, camera_model="ILCE-7M4"):
    """Match one whole tuple; never admit individual files from different releases."""
    fs = Fat(image)
    files = {name: fs.read(name) for name in BOOT_FILES}
    hashes = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
              for name, data in files.items()}
    matches = [(key, value) for key, value in catalogue['profiles'].items() if value['files'] == hashes]
    require(len(matches) == 1 or camera_model == "ILCE-7RM5", 'UPDATER_PROFILE',
            'Current SD updater does not match a complete verified Sony boot profile')
    if camera_model == "ILCE-7RM5" and not matches:
        from .stock_updater_source import ERLLA_INITRD_SHA256
        require(hashes["boot/initrd.img"]["sha256"] != ERLLA_INITRD_SHA256,
                "UPDATER_PROFILE", "Known Erlla updater requires Sony initrd reconstruction")
    profile_id, profile = (matches[0] if matches else
                           ("checksum-verified-live-ILCE-7RM5", {"versions": []}))
    lines = files['p1chksum'].splitlines()
    for name in ('boot/vmlinux.bin', 'boot/initrd.img'):
        expected = hashlib.md5(files[name]).hexdigest().encode() + b'  ./' + name.encode()
        require(lines.count(expected) == 1, 'UPDATER_CHECKSUM',
                f'Stock MD5 does not match {name}')
    return {'profile_id': profile_id, 'camera_model': camera_model, 'official_versions': list(profile['versions']),
            'boot_files': hashes, 'initrd_sha256': hashes['boot/initrd.img']['sha256'],
            'updater_environment_known': True,
            'SD_firmware_validation': 'performed by original camera updater',
            'host_read_SD_card': False,
            'target_update_success_guaranteed': False}


def is_official_initrd(data):
    """A recognized official initrd belongs only to its complete boot tuple."""
    digest = hashlib.sha256(data).hexdigest()
    return any(
        p['files']['boot/initrd.img']['sha256'] == digest for p in CATALOG['profiles'].values())
