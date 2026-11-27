"""Durable same-volume file replacement for transaction evidence."""
from __future__ import annotations
import os
from pathlib import Path


def durable_replace(source: Path, destination: Path) -> None:
    """Replace an already-flushed file and persist its directory entry.

    Windows CRT cannot open directories for fsync. MoveFileExW with
    WRITE_THROUGH supplies the native durable rename operation instead.
    """
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(Path(source).resolve()), str(Path(destination).resolve()), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, destination)
    directory = os.open(Path(destination).parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
