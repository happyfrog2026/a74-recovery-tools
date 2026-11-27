"""Windows-only entry for the existing headless Camera Recovery JSONL worker.

The adapter installs host services before importing task_worker. Camera USB
commands remain in recovery_tool; this module never opens a device itself.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import types

_dll_directory_handle = None


def configure_worker_stdio() -> None:
    """The Electron JSONL pipe is UTF-8, including isolated embedded Python."""
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def app_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("LOCALAPPDATA is required for Windows task receipts")
    return Path(base) / "Camera Recovery"


def removable_drive_id(volume: str | Path) -> str:
    """Validate an exact writable removable drive root and pin volume serial."""
    if os.name != "nt":
        raise RuntimeError("Windows removable-drive inspection requires Windows")
    path = Path(volume).resolve(strict=True)
    if path != Path(path.anchor) or not path.is_dir():
        raise ValueError("Select the root of one mounted removable SD or USB drive")
    root = str(path)
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    if drive_type not in (2, 3):  # Removable or USB reader reported fixed.
        raise ValueError("Selected path is not a mounted disk volume")
    letter = path.drive[0].upper()
    script = (f"Get-Partition -DriveLetter '{letter}' -ErrorAction Stop | Get-Disk "
              "| Select-Object Number,UniqueId,IsOffline,IsReadOnly,"
              "@{Name='BusType';Expression={$_.BusType.ToString()}} "
              "| ConvertTo-Json -Compress")
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                "-Command", script], capture_output=True, text=True,
                               check=True, timeout=15)
    disk = json.loads(completed.stdout)
    if (not isinstance(disk, dict) or disk.get("BusType") not in {"USB", "SD", "MMC"}
            or disk.get("IsOffline") is not False or disk.get("IsReadOnly") is not False):
        raise ValueError("Selected drive is not a writable physical USB or SD card")
    serial = ctypes.c_uint32()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial), None, None, None, 0)
    if not ok:
        raise OSError("Windows cannot read removable volume identity")
    if not os.access(path, os.W_OK):
        raise ValueError("Selected removable volume is not writable")
    return f"{path.drive.upper()}:{disk['Number']}:{disk.get('UniqueId') or 'NO-UNIQUE-ID'}:{serial.value:08X}"


def mounted_removable_volumes() -> list[dict]:
    if os.name != "nt":
        return []
    drives = ctypes.windll.kernel32.GetLogicalDrives()
    found = []
    for number in range(26):
        if not drives & (1 << number):
            continue
        root = Path(f"{chr(65 + number)}:\\")
        try:
            found.append({"path": str(root), "device_identifier": removable_drive_id(root)})
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return found


def _install_fcntl_lock() -> None:
    """Provide only the flock operation used by task_worker using msvcrt."""
    import msvcrt
    module = types.ModuleType("fcntl")
    module.LOCK_EX = 2
    module.LOCK_NB = 4

    def flock(fd: int, operation: int) -> None:
        if operation != module.LOCK_EX | module.LOCK_NB:
            raise ValueError("Only the worker's nonblocking exclusive lock is supported")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    module.flock = flock
    sys.modules["fcntl"] = module


def _install_libusb() -> None:
    global _dll_directory_handle
    folder = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    library = folder / "libusb-1.0.dll"
    if library.is_file():
        _dll_directory_handle = os.add_dll_directory(str(folder))
        os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


def backup_fastboot_state() -> str:
    """Inspect present Windows PnP instances without opening a camera handle."""
    script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ids = @(Get-PnpDevice -PresentOnly | Select-Object -ExpandProperty InstanceId)
if ($ids.Count -eq 0 -or $ids -contains $null) { 'unknown'; return }
$fastboot = @($ids -match '^USB\\VID_18D1&PID_4EE0(?:&[^\\]+)?\\')
switch ($fastboot.Count) {
    0 { 'departed' }
    1 { 'present' }
    default { 'unknown' }
}
"""
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                 "-Command", script], capture_output=True, text=True, check=True,
                                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return "unknown"
    state = result.stdout.strip()
    return state if state in {"present", "departed"} else "unknown"


def install() -> None:
    if os.name != "nt":
        raise RuntimeError("This worker entry is Windows-only")
    # Embedded CPython and PyInstaller share the same resource lookup contract.
    if not getattr(sys, "frozen", False) and Path(__file__).with_name("python311._pth").is_file():
        sys._MEIPASS = str(Path(__file__).resolve().parent)
    _install_fcntl_lock()
    _install_libusb()
    from recovery_tool import card_connection, sd_card_prepare, task_worker
    root = app_root()
    card_connection.APP_ROOT = task_worker.APP_ROOT = root
    card_connection.TASK_ROOT = task_worker.TASK_ROOT = root / "Tasks"
    card_connection.MODE_JOURNAL = task_worker.MODE_JOURNAL = root / "card-mode-transition.json"
    card_connection.TASK_REGISTRY = root / "task-registry.json"
    card_connection.mounted_usb_volumes = mounted_removable_volumes

    def prepare_card_connection(*, route, progress=None, camera_model="ILCE-7M4"):
        return card_connection.prepare_card_connection(
            route=route, volume_probe=mounted_removable_volumes, camera_model=camera_model,
            task_root=card_connection.TASK_ROOT,
            journal_path=card_connection.MODE_JOURNAL, progress=progress)

    def return_normal(*, journal_path, transport_factory=None, progress=None,
                      ignore_historical_tasks=False, once_only=False, camera_model="ILCE-7M4"):
        from recovery_tool.transport import FastbootTransport
        return card_connection.return_normal(
            journal_path=journal_path, transport_factory=transport_factory or FastbootTransport,
            task_root=card_connection.TASK_ROOT,
            ignore_historical_tasks=ignore_historical_tasks, once_only=once_only, progress=progress,
            camera_model=camera_model)

    def prepare_sd_card(source_dat, volume, **kwargs):
        return sd_card_prepare.prepare(source_dat, volume,
                                       volume_inspector=removable_drive_id, **kwargs)

    task_worker.backup_fastboot_state = backup_fastboot_state
    task_worker.prepare_card_connection = prepare_card_connection
    task_worker.return_normal = return_normal
    task_worker.prepare_sd_card = prepare_sd_card


def _check_squashfs(tool: Path) -> None:
    """Exercise the NOR tar/metadata flags without any camera or source data."""
    import io
    import tarfile
    import tempfile
    from PySquashfsImage import SquashFsImage
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.PAX_FORMAT) as archive:
        entry = tarfile.TarInfo("runtime-check")
        entry.mode, entry.uid, entry.gid, entry.mtime, entry.size = 0o755, 123, 456, 1000000000, 2
        archive.addfile(entry, io.BytesIO(b"ok"))
    with tempfile.TemporaryDirectory(prefix="camera-runtime-check-") as tmp:
        output = Path(tmp) / "check.squashfs"
        subprocess.run([str(tool), "-", str(output), "-tar", "-noappend", "-comp", "gzip",
                        "-no-xattrs", "-numeric-owner", "-processors", "1", "-no-progress",
                        "-quiet", "-mkfs-time", "1000000000", "-root-time", "1000000000",
                        "-root-mode", "755", "-root-uid", "0", "-root-gid", "0", "-no-exports"],
                       input=payload.getvalue(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with SquashFsImage.from_bytes(output.read_bytes()) as image:
            item = image.select("/runtime-check")
            if (item.read_bytes(), item.mode & 0o7777, item.uid, item.gid) != (b"ok", 0o755, 123, 456):
                raise RuntimeError("Bundled SquashFS metadata check failed")


def main() -> None:
    configure_worker_stdio()
    install()
    if sys.argv[1:] == ["--check-runtime"]:
        from recovery_tool.nor_resources import UPDATERS, resolve
        import cryptography, serial, usb.core
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        Cipher(algorithms.AES(bytes(32)), modes.ECB()).encryptor().update(bytes(16))
        for updater in UPDATERS:
            resources = resolve(updater)
        _check_squashfs(resources["mksquashfs"])
        import usb.backend.libusb1
        library = Path(sys._MEIPASS) / "libusb-1.0.dll"
        if usb.backend.libusb1.get_backend(find_library=lambda _: str(library)) is None:
            raise RuntimeError("Bundled libusb could not load")
        print(json.dumps({"runtime": "ready", "hardware_accessed": False}), flush=True)
        return
    from recovery_tool.task_worker import main as worker_main
    print(json.dumps({"type": "progress", "event": {
        "stage": "worker_runtime",
        "message": f"Windows worker stdin={sys.stdin.encoding}; stdout={sys.stdout.encoding}; "
                   f"stderr={sys.stderr.encoding}; app_root={app_root()}",
    }}, ensure_ascii=False), flush=True)
    worker_main()


if __name__ == "__main__":
    main()
