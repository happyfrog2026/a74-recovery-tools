"""Reuse complete official boot profiles or repair the compatible 6.02 lineage.

Two stock components extracted from the user's official Sony DAT are bundled
in this local app. The output retains every live byte outside the verified
initrd allocation, P1CHKSUM content, and initrd directory size. UFU decoding
for asset preparation follows the MIT-licensed a7m4.firmware host
implementation in the sibling source tree; its license is packaged with the
app. Runtime reconstruction does not decrypt the DAT again.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct

from .stock_common import require
from .stock_fat import Fat
from .stock_common import StockRecoveryError
from .stock_profiles import inspect_environment, inspect_package, is_official_initrd


DAT_SHA256 = "ad6df47a0a93a46336b9920a0d8891d0c766f8c96fa68ebda8685f6c21621188"
PLAIN_SHA256 = "4fd810b8d20ea239e0df01aded90687baef1ca2c8df208a7f58c4989f85799c3"
DAT_UPDATER_SHA256 = "696992b67a8a962b9d5f1a5cf17108b1a5fb4e77d1389a359e7e73ac88c6a103"
STOCK_INITRD_SHA256 = "2aea454ee676c2019ea44026cb15421756ef0631ef45ce5311d168c0f8b318e5"
ERLLA_INITRD_SHA256 = "d385a2ba5cdf51a6cf80f2de6ec259aa9198c702b144ae70488579b84e6afec2"
STOCK_P1_SHA256 = "46b171610cc05bffae28eabc4bcdd3f0efb54942fe3c4c7747ea5bc17a0c5fbd"
ERLLA_P1_SHA256 = "670d5b04abc69b152a5b92c1e300d8ddc17687376c7393cb2241398aca02e3e2"
ASSET_DIR = Path(__file__).resolve().parent / "stock_assets"
STOCK_FILES = {
    "boot/initrd.img": ("sony-602-initrd.img", 9_097_216, STOCK_INITRD_SHA256),
    "p1chksum": ("sony-602-p1chksum.bin", 105, STOCK_P1_SHA256),
}
SHARED_FILES = {
    "boot/vmlinux.bin": "838f5ff30cc9d3d3c99d8db3098e16a2dff204b370adfbbee2e58ab78756c106",
    "boot/cxd90057.dtb": "87abb2b72c3d373cf8f0779f8981a6e3f6780b54d08862d61a252248ebcf36ca",
    "boot/kemco.txt": "eb184157afc8eaf2d9a3dc795e7b52057130cb25f7799bc72fd6c85cbb99c94a",
    "boot/loadaddr.txt": "8c7cc8eb65c40311172f4bf1dc591fe3bcbc1efe689ededdd225142139881452",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reconstruct(live_updater: bytes, sony_dat: Path | None = None, *,
                camera_model: str = "ILCE-7M4") -> tuple[bytes, dict]:
    """Return a scoped OEM candidate and proof; never accesses a device."""
    require(len(live_updater) == 25_100_288, "UPDATER_PROFILE", "Unexpected live updater size")
    package = ((inspect_package(sony_dat, camera_model=camera_model)
                if camera_model == "ILCE-7RM5" else inspect_package(sony_dat))
               if sony_dat is not None else None)
    # Official environments need no bundled firmware and no mutation. Match the
    # complete boot tuple, including the kernel and both checksum entries.
    try:
        environment = inspect_environment(live_updater)
    except StockRecoveryError:
        environment = None
    if environment is not None:
        return live_updater, {"source": "verified_live_sony_stock_updater",
                              "dat_sha256": package["sha256"] if package else None,
                              "live_sha256": _sha(live_updater),
                              "candidate_sha256": _sha(live_updater),
                              "environment": environment,
                              "changed_sectors": [], "camera_io": False}
    # Do not turn a mixed official kernel/initrd pair into a repair candidate.
    repair_fs = Fat(live_updater)
    require(not is_official_initrd(repair_fs.read("boot/initrd.img")),
            "UPDATER_PROFILE", "Mixed or damaged official boot tuple; no partial 6.02 repair")
    stock = {}
    for path, (name, size, expected) in STOCK_FILES.items():
        asset = ASSET_DIR / name
        require(asset.is_file(), "STOCK_ASSET", f"Missing bundled Sony 6.02 {name}")
        value = asset.read_bytes()
        require(len(value) == size and _sha(value) == expected,
                "STOCK_ASSET", f"Bundled Sony 6.02 {name} differs from reviewed extraction")
        stock[path] = value
    live = Fat(live_updater)
    require(live.total == 19_377_152 and live.bps == 512 and live.cluster_size == 16_384
            and live.data_start == 49_664, "UPDATER_PROFILE", "Unexpected live FAT layout")
    for path, expected in SHARED_FILES.items():
        require(_sha(live.read(path)) == expected,
                "UPDATER_PROFILE", f"Incompatible shared boot file: {path}")
    initrd_entry = live.locate("boot/initrd.img")
    p1_entry = live.locate("p1chksum")
    require(initrd_entry[0] == 66_144 and live.chain(initrd_entry[1]) == list(range(4, 560))
            and p1_entry[0] == 33_376 and live.chain(p1_entry[1]) == [966],
            "UPDATER_PROFILE", "Live updater file allocations differ from reviewed Sony layout")
    live_initrd = live.read("boot/initrd.img")
    live_p1 = live.read("p1chksum")
    require(len(live_p1) == 105, "UPDATER_PROFILE", "Live updater checksum file size differs from reviewed layout")
    if (_sha(live_initrd) == STOCK_INITRD_SHA256 and
            _sha(live_p1) == STOCK_P1_SHA256):
        return live_updater, {"source": "compatible_live_sony_602_stock_updater",
                              "dat_sha256": package["sha256"] if package else None,
                              "dat_updater_sha256": DAT_UPDATER_SHA256,
                              "live_sha256": _sha(live_updater),
                              "candidate_sha256": _sha(live_updater),
                              "stock_initrd_sha256": STOCK_INITRD_SHA256,
                              "stock_p1chksum_sha256": STOCK_P1_SHA256,
                              "changed_sectors": [], "camera_io": False}
    # The official initrd chain has zero-filled slack after its 9,097,216 B
    # content. Clear the longer Erlla initrd's stale tail inside that chain.
    initrd = stock["boot/initrd.img"].ljust(556 * live.cluster_size, b"\0")
    for index, cluster in enumerate(live.chain(initrd_entry[1])):
        offset = live.offset(cluster)
        live.data[offset:offset + live.cluster_size] = initrd[index * live.cluster_size:(index + 1) * live.cluster_size]
    struct.pack_into("<I", live.data, initrd_entry[0] + 28, len(stock["boot/initrd.img"]))
    live.replace("p1chksum", stock["p1chksum"])
    candidate = live.bytes()
    check = Fat(candidate)
    require(_sha(check.read("boot/initrd.img")) == STOCK_INITRD_SHA256 and
            _sha(check.read("p1chksum")) == STOCK_P1_SHA256,
            "UPDATER_PROFILE", "Rebuilt updater failed stock file verification")
    changed = [index for index in range(0, len(candidate), 512)
               if candidate[index:index + 512] != live_updater[index:index + 512]]
    allowed = set()
    for path in ("boot/initrd.img", "p1chksum"):
        entry = check.locate(path)
        allowed.add(entry[0] // 512)
        for cluster in check.chain(entry[1]):
            offset = check.offset(cluster)
            allowed.update(range(offset // 512, (offset + check.cluster_size) // 512))
    require(all(offset // 512 in allowed for offset in changed),
            "UPDATER_PROFILE", "Candidate changed bytes outside reviewed files")
    return candidate, {"source": "bundled_user_extracted_sony_602_stock_components",
                       "dat_sha256": package["sha256"] if package else None,
                       "dat_updater_sha256": DAT_UPDATER_SHA256,
                       "live_sha256": _sha(live_updater), "candidate_sha256": _sha(candidate),
                       "stock_initrd_sha256": STOCK_INITRD_SHA256,
                       "stock_p1chksum_sha256": STOCK_P1_SHA256,
                       "changed_sectors": [offset // 512 for offset in changed],
                       "camera_io": False}
