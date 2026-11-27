"""Offline stock SD MODE5 trigger for a structurally reviewed A7M4 layout.

FAT and MODE5 candidate logic adapted from MIT-licensed a7m4.sd_recovery.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from .stock_common import StockRecoveryError, require
from .stock_fat import Fat
from .stock_profiles import CATALOG, inspect_environment, inspect_package

SETTINGS_SIZE = 20 * 1024 * 1024
# Exact Sony 6.02 artifacts previously verified on ILCE-7M4. Only digests are
# published; device images and per-camera settings remain outside this module.
STOCK_DAT_SHA256 = "ad6df47a0a93a46336b9920a0d8891d0c766f8c96fa68ebda8685f6c21621188"
STOCK_UPDATER_INITRD_SHA256 = "2aea454ee676c2019ea44026cb15421756ef0631ef45ce5311d168c0f8b318e5"
PROFILE = CATALOG

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def decode_version(raw):
    require(len(raw) == 2 and all((b >> 4) <= 9 and (b & 15) <= 9 for b in raw),
            'BCD', 'DAT4 is not a valid two-byte BCD version')
    minor, major = ((b >> 4) * 10 + (b & 15) for b in raw)
    return major * 100 + minor


def encode_version(value):
    require(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9999,
            'BCD', 'Version is outside the supported BCD range')
    major, minor = divmod(value, 100)
    return bytes(((minor // 10) * 16 + minor % 10, (major // 10) * 16 + major % 10))


def version_text(value):
    return f'{value // 100}.{value % 100:02d}'


def all_files(fs):
    files = {}
    def walk(first=None, prefix=''):
        for name, (_, cluster, _, attr) in fs.entries(first).items():
            if name in ('.', '..'):
                continue
            path = prefix + name
            if attr & 16:
                walk(cluster, path + '/')
            else:
                files[path] = fs.read(path)
    walk()
    return files


def make_trigger(original):
    """Only DAT4 data and at most two unused 32-byte directory slots may change."""
    require(len(original) == SETTINGS_SIZE, 'SETTINGS_SIZE', 'Settings size differs from the verified A7M4 layout')
    fs = Fat(original)
    current = decode_version(fs.read('updater/dat4'))
    # DAT4 is a recorded value, not a filesystem or updater layout identifier.
    # Its BCD syntax was checked above; the admission checks below validate the
    # actual FAT structure and every byte that the MODE5 trigger may change.
    root = fs.entries().get('UPDATER')
    require(root and root[3] & 16, 'UPDATER_DIR', 'The stock UPDATER directory is missing')
    entries = fs.entries(root[1])
    conflicting = {'MODE1', 'MODE2', 'MODE3', 'MODE4', 'MODE6', 'MODE7', 'UINCOMP'} & entries.keys()
    require(not conflicting, 'OTHER_UPDATE_MODE', 'Another update mode is present: ' + ','.join(sorted(conflicting)))
    missing = []
    for name in ('MODE', 'MODE5'):
        if name in entries:
            entry = entries[name]
            require(not entry[3] & 16 and entry[1] == 0 and entry[2] == 0,
                    'BAD_MODE_FLAG', name + ' is not an empty, clusterless flag file')
        else:
            missing.append(name)
    before_files = all_files(fs)
    dat4_entry, cluster, size, _ = fs.locate('updater/dat4')
    require(size == 2, 'DAT4_SIZE', 'DAT4 has an unexpected length')
    value_offset = fs.offset(cluster)
    # The SD file stays inside slot 1. Its target version is unknown to the host.
    # 0.00 permits same-version and downgrade rescue through the native BCD gate.
    gate = 0
    fs.replace('updater/dat4', encode_version(gate))
    allowed = [(value_offset, 2)]
    created = []
    if missing:
        slots = [off for c in fs.chain(root[1])
                 for off in range(fs.offset(c), fs.offset(c) + fs.cluster_size, 32)]
        first_zero = next((i for i, off in enumerate(slots) if fs.data[off] == 0), None)
        require(first_zero is not None and first_zero + len(missing) < len(slots),
                'NO_DIRECTORY_ROOM', 'UPDATER has insufficient existing empty slots; no filesystem expansion is permitted')
        use = slots[first_zero:first_zero + len(missing) + 1]
        require(all(fs.data[off:off + 32] == bytes(32) for off in use),
                'DIR_TAIL_NOT_EMPTY', 'UPDATER directory tail is not empty as reviewed')
        for name, off in zip(missing, use):
            row = bytearray(32)
            row[:11] = name.encode('ascii').ljust(8, b' ') + b'   '
            row[11], row[12] = 0x20, 8  # empty archive file, lowercase display
            row[14:26] = original[dat4_entry + 14:dat4_entry + 26]
            require(row[20:22] == b'\0\0', 'FAT_HIGH_CLUSTER', 'Unexpected FAT12/16 high-cluster field')
            fs.data[off:off + 32] = row
            allowed.append((off, 32))
            created.append(name)
    candidate = fs.bytes()
    after = Fat(candidate)
    require(after.read('updater/mode') == after.read('updater/mode5') == b'', 'FLAG_VERIFY', 'Update flags failed readback')
    require(after.read('updater/dat4') == b'\0\0', 'GATE', 'Temporary version gate failed readback')
    files_after = all_files(after)
    require(set(files_after) - set(before_files) == {'UPDATER/' + n for n in created} and
            not set(before_files) - set(files_after), 'FILES_CHANGED', 'Unexpected file set change')
    for path, data in before_files.items():
        expected = encode_version(gate) if path == 'UPDATER/DAT4' else data
        require(files_after[path] == expected, 'FILE_CHANGED', 'Original file changed: ' + path)
    require(candidate[fs.fat_start:fs.data_start] == original[fs.fat_start:fs.data_start],
            'FAT_CHANGED', 'FAT or root directory changed')
    changes = [i for i, (a, b) in enumerate(zip(original, candidate)) if a != b]
    require(all(any(lo <= i < lo + n for lo, n in allowed) for i in changes),
            'UNEXPECTED_DIFF', 'Candidate changed bytes outside the reviewed scope')
    require(after.read('Backup.bin') == fs.read('Backup.bin'), 'CALIBRATION_CHANGED', 'Camera-specific backup data changed')
    return candidate, dict(current=version_text(current), target='slot1:/BODYDATA.DAT',
                           admission_basis='validated FAT settings structure and pinned updater environment',
                           target_version_known_to_host=False,
                           temporary_gate=version_text(gate), created_flags=created,
                           changed_bytes=len(changes), changed_offsets=changes,
                           allowed_ranges=allowed, calibration_preserved=True,
                           other_files_preserved=True, FAT_unchanged=True)


def inspect_sd_environment(image, config=PROFILE, *, camera_model="ILCE-7M4"):
    return inspect_environment(image, config, camera_model=camera_model)


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def dry_run(settings_path: Path, updater_path: Path, sd_dat_path: Path) -> dict:
    """Plan from fresh camera reads and the actual card file; never access a device.

    The caller must collect settings and updater via a verified Fastboot session.
    A matching host file does not prove that slot 1 will contain that card later.
    """
    settings_path, updater_path, sd_dat_path = map(Path, (settings_path, updater_path, sd_dat_path))
    package = inspect_package(sd_dat_path)
    dat_hash = package["sha256"]
    updater = inspect_sd_environment(updater_path.read_bytes(), PROFILE)
    original = settings_path.read_bytes()
    candidate, plan = make_trigger(original)
    return {
        "status": "OFFLINE_PLAN_ONLY",
        "product": "ILCE-7M4",
        "source_settings_sha256": digest(original),
        "source_updater_sha256": file_sha256(updater_path),
        "candidate_sha256": digest(candidate),
        "candidate_bytes": len(candidate),
        "stock_dat_sha256": dat_hash,
        "target_package_version": package["version"],
        "updater": updater,
        "plan": plan,
        "camera_write": False,
        "camera_reboot": False,
        "sd_slot_1_presence_verified": False,
        "camera_update_completed": False,
    }


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Offline official Sony stock SD recovery plan; no device I/O")
    parser.add_argument("settings", type=Path, help="fresh nflasha2/settings read")
    parser.add_argument("updater", type=Path, help="fresh nflasha1/updater read")
    parser.add_argument("sd_dat", type=Path, help="BODYDATA.DAT on the SD card mounted by this host")
    args = parser.parse_args(argv)
    try:
        result = dry_run(args.settings, args.updater, args.sd_dat)
    except (StockRecoveryError, OSError) as exc:
        print(json.dumps({"error": str(exc), "code": getattr(exc, "code", type(exc).__name__)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
