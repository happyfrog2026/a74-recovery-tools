"""Matched ILCE-7M4 updater environments and bundled Sony NOR resources."""
import argparse
import json
import hashlib
import struct
from pathlib import Path
import os
import sys

from . import nor_qualification as q
from .stock_fat import Fat
from .stock_common import StockRecoveryError
from .stock_profile_data import CATALOG

ASSET_DIR = Path(__file__).resolve().parent / 'nor_assets'


# IDs come from the stock updater catalog. Assets shared with 6.02 live in sony602.
UPDATERS = {
    '94dbf6d871c1d995': {'cp/udtrcp.bin': 'd47a852e935204f135e0df8c4c2de3dff6f9d35a76ceacf078fee900f9ed4399'},
    '5034ae00f3ac8d15': {'cp/udtrcp.bin': '3dfc22ebb8976fb50c4f43d35364b44ba1741a7ecb3da535995424688eee337d'},
    '3d57de854a449cb2': {},
}
DEFAULT_UPDATER = '3d57de854a449cb2'
PROFILE = 'sony602-native-nor-rebuild-v1'
COMPONENTS = ('DGU.bin', 'MIS.bin', 'loader_spi.bin', 'pcie_phy_fw.bin',
              'cpapp-b.bin', 'nor_ldr.img')


def select_updater(f1):
    boot = {name: f1.read(name) for name in q.P1_PINS}
    hashes = {name: q.sha(data) for name, data in boot.items()}
    for key in UPDATERS:
        stock = CATALOG['profiles'][key]['files']
        if all(hashes[name] == stock[name]['sha256'] for name in hashes):
            checksum = f1.read('p1chksum').splitlines()
            for name in ('boot/initrd.img', 'boot/vmlinux.bin'):
                line = hashlib.md5(boot[name]).hexdigest().encode() + b'  ./' + name.encode()
                q.need(checksum.count(line) == 1, 'updater checksum mismatch: ' + name)
            return key
    raise ValueError('unrecognized ILCE-7M4 NOR updater environment')


def cp_components(f8):
    files = {name: q.read_root_file(f8, name) for name in COMPONENTS}
    limits = {'DGU.bin': 32768, 'MIS.bin': 131072, 'loader_spi.bin': 65536,
              'pcie_phy_fw.bin': 16384, 'cpapp-b.bin': 24 * 1024 * 1024}
    for name, limit in limits.items():
        q.need(0 < len(files[name]) <= limit, 'unsupported NOR component allocation: ' + name)
    q.need(len(files['pcie_phy_fw.bin']) == limits['pcie_phy_fw.bin'],
           'unexpected PCIe PHY size for the fixed erase geometry')
    mis = files['MIS.bin']
    q.need(mis.startswith(b'MIS_HEAD') and len(mis) >= 2048 and
           struct.unpack_from('<II', mis, 320) == (512, 1536),
           'unsupported MIS format or training layout')
    companion = files['nor_ldr.img']
    q.need(len(companion) == 417792 and companion[:4] == b'EXBL', 'unsupported companion allocation')
    return files


def inspect_profile(p1_path, p8_path):
    """Identify the boot environment and validate the restored CP data shape."""
    try:
        updater = select_updater(Fat(Path(p1_path).read_bytes()))
        cpapp = cp_components(Fat(Path(p8_path).read_bytes()))['cpapp-b.bin']
    except (ValueError, KeyError, IndexError, OSError, StockRecoveryError) as exc:
        return {'supported': False, 'profile': None, 'reason': str(exc)}
    return {'supported': True, 'profile': PROFILE, 'updater_profile': updater,
            'official_versions': list(CATALOG['profiles'][updater]['versions']),
            'cpapp_sha256': q.sha(cpapp), 'cpapp_bytes': len(cpapp)}

def snapshot_profile(snapshot):
    """Inspect already-verified target files without opening the camera."""
    folder = Path(snapshot) / 'decrypted'
    try:
        manifest = json.loads((folder / 'manifest.json').read_text())
        rows = {row['slot']: row for row in manifest['partitions']}
        paths = []
        for slot in (1, 8):
            q.need(slot in rows, f"snapshot has no decrypted partition for slot {slot}")
            row = rows[slot]
            path = (folder / row['file']).resolve()
            q.need(path.is_relative_to(folder.resolve()), 'target input escapes decrypted folder')
            pin = q.file_pin(path)
            q.need(pin['sha256'] == row['sha256'] and pin['bytes'] == row['bytes'], f'snapshot size or SHA-256 mismatch for slot {slot}')
            paths.append(path)
        return inspect_profile(*paths)
    except (OSError, ValueError, KeyError, TypeError, StockRecoveryError) as exc:
        return {'supported': False, 'profile': None, 'reason': str(exc)}


def tool_inputs(root, updater):
    overrides = UPDATERS[updater]
    for name, original in q.SONY_PINS.items():
        if name in overrides:
            yield name, ASSET_DIR / updater / name, overrides[name]
        else:
            yield name, root / name, original


def resolve(updater=DEFAULT_UPDATER):
    """Verify packaged inputs; never fall back to a research disk or Homebrew."""
    root = ASSET_DIR / 'sony602'
    pins = []
    for name, path, expected in tool_inputs(root, updater):
        pin = q.file_pin(path)
        q.need(pin['sha256'] == expected, 'bundled Sony tool pin mismatch: ' + name)
        pins.append(pin)
    loader = root / 'loader_nor.tar'
    pin = q.file_pin(loader)
    q.need(pin['sha256'] == q.LOADER_SHA, 'bundled matched loader pin mismatch')
    pins.append(pin)
    frozen = getattr(sys, '_MEIPASS', None)
    tool = 'mksquashfs.exe' if sys.platform == 'win32' else 'mksquashfs'
    squash = (Path(frozen) / 'nor_tools' / tool if frozen else ASSET_DIR / 'bin' / tool)
    q.need(squash.is_file() and os.access(squash, os.X_OK), 'bundled mksquashfs is missing or not executable')
    # Fail before entry mutation if the bundled Python parser is unavailable.
    from PySquashfsImage import SquashFsImage  # noqa: F401
    return {'sony_body': root, 'loader_tar': loader, 'mksquashfs': squash,
            'pins': pins + [q.file_pin(squash)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check the NOR profile of a local snapshot without connecting to a camera.")
    parser.add_argument("snapshot", type=Path, help="snapshot folder or its enclosing backup task")
    args = parser.parse_args(argv)
    folder = args.snapshot
    if not (folder / 'decrypted').is_dir() and (folder / 'snapshot').is_dir():
        folder = folder / 'snapshot'
    report = snapshot_profile(folder)
    print(json.dumps({'check': 'snapshot_component_match', 'snapshot': str(folder.resolve()),
                      **report}, indent=2))
    return 0 if report['supported'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
