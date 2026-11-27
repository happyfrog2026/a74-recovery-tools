"""Serialize camera USB while cancellable host-only restore work runs.

The calling thread owns every USB operation.  A worker thread may read and
hash local files, but it never receives a transport or bridge handle.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
import queue
import threading
import time


class HostWorkCancelled(RuntimeError):
    pass


class IdleUSBSession:
    def __init__(self, *, probe, reenter, can_reenter, progress=None,
                 interval_seconds: float = 10.0):
        self.probe = probe
        self.reenter = reenter
        self.can_reenter = can_reenter
        self.progress = progress
        self.interval_seconds = interval_seconds

    def run_host(self, work):
        """Run ``work(cancel_event, progress)`` with idle-only USB observation.

        A failing probe must never leave a file-writing host thread behind.
        The caller's thread alone invokes ``probe`` and ``reenter``.
        """
        cancelled = threading.Event()
        events = queue.Queue()
        def host_progress(event):
            if cancelled.is_set():
                raise HostWorkCancelled("local restore verification cancelled")
            events.put(event)
        def drain():
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    return
                if self.progress is not None:
                    self.progress(event)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="restore-host") as executor:
            future = executor.submit(work, cancelled, host_progress)
            next_probe = time.monotonic()
            try:
                while True:
                    drain()
                    if future.done():
                        result = future.result()
                        drain()
                        return result
                    now = time.monotonic()
                    if now >= next_probe:
                        if not self.probe():
                            # A single interrupted USB close/read is not
                            # evidence that a bridge reset is needed.
                            time.sleep(0.25)
                            if self.probe():
                                next_probe = time.monotonic() + self.interval_seconds
                                continue
                            if not self.can_reenter():
                                raise RuntimeError("RAM session lost outside a safe reentry boundary")
                            self.reenter()
                            if not self.probe():
                                raise RuntimeError("RAM session unavailable after guarded reentry")
                        next_probe = time.monotonic() + self.interval_seconds
                    try:
                        future.result(timeout=min(0.25, max(0.01, next_probe - time.monotonic())))
                    except TimeoutError:
                        pass
            except BaseException:
                cancelled.set()
                future.cancel()
                try:
                    future.result()
                except BaseException:
                    pass
                drain()
                raise
