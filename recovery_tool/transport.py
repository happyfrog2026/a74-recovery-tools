"""Read-only USB Fastboot transport for David's ILCE-7M4 RAM payload."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import sys

VID, PID = 0x18D1, 0x4EE0
PRODUCT = "ILCE-7M4"
SERIAL = "ILCE7M4-fastboot"
TIMEOUT_MS = 3500
REGIONS = {"nflashaB0", "nflashaB1", "nflasha", "cp-nor"}
SONY_VID, SONY_NORMAL_PID = 0x054C, 0x0DA5
SD_CAMERA_MODELS = (PRODUCT, "ILCE-7RM5")


def fastboot_products(camera_model: str = PRODUCT) -> tuple[str, ...]:
    """The shared RAM payload can retain its original M4 product identifier."""
    if camera_model not in SD_CAMERA_MODELS:
        raise ValueError(f"Unsupported RAM Fastboot camera model: {camera_model!r}")
    return (PRODUCT,) if camera_model == PRODUCT else (PRODUCT, camera_model)


def _record_usb_cleanup_failure(release_error, dispose_error) -> None:
    """Best-effort private evidence; logging must never mask the primary I/O error."""
    try:
        folder = Path.home() / "Library/Logs/Camera Recovery Logs"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                           "release_error": str(release_error) if release_error else None,
                           "dispose_error": str(dispose_error) if dispose_error else None}) + "\n"
        fd = os.open(folder / "usb-cleanup.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def observe_normal_camera_usb(camera_model: str = PRODUCT) -> dict | None:
    """Read USB descriptors only; do not claim an interface or change modes.

    M4 uses its known ordinary-mode PID. R5 observes a single Sony USB
    device without assuming the M4 PID. Interface class 8 identifies MSC.
    The product field records the caller-selected model.
    """
    fastboot_products(camera_model)
    try:
        import usb.core
        import usb.backend.libusb1
    except ImportError as exc:
        raise FastbootError("PyUSB and libusb are required for USB discovery") from exc
    bundled_libusb = Path(getattr(sys, "_MEIPASS", "")) / "libusb-1.0.0.dylib"
    backend = (usb.backend.libusb1.get_backend(find_library=lambda _: str(bundled_libusb))
               if getattr(sys, "frozen", False) and bundled_libusb.is_file() else None)
    filters = {"idVendor": SONY_VID}
    if camera_model == PRODUCT:
        filters["idProduct"] = SONY_NORMAL_PID
    matches = list(usb.core.find(find_all=True, backend=backend, **filters) or [])
    if not matches:
        return None
    if len(matches) != 1:
        raise FastbootError(f"Expected one {camera_model} normal Sony USB device; found {len(matches)}")
    device = matches[0]
    interfaces = [interface.bInterfaceClass for config in device for interface in config]
    return {"status": "MSC" if 8 in interfaces else "NORMAL_USB",
            "product": camera_model, "vid": SONY_VID, "pid": device.idProduct,
            "interface_classes": interfaces}


def valid_region(region: str) -> bool:
    return region in REGIONS or re.fullmatch(r"nflasha[1-9][0-9]*", region) is not None


class FastbootError(RuntimeError):
    pass


class FastbootEnumerationUnavailable(FastbootError):
    """Fastboot is enumerated but its serial identity cannot yet be read."""


class FastbootIdentityError(FastbootError):
    """A resolved device/descriptor mismatch; bridge reset cannot grant trust."""


class USBReadError(FastbootError):
    """A transport I/O failure while reading camera data, distinct from disk I/O."""
    def __init__(self, message: str, error: OSError, *, stage: str = "usb_read"):
        super().__init__(message)
        self.errno = error.errno
        self.stage = stage
        self.backend_error_type = type(error).__name__
        self.backend_error_code = getattr(error, "backend_error_code", None)


    @property
    def endpoint_missing_during_transfer(self) -> bool:
        # Darwin can report LIBUSB_ERROR_NOT_FOUND when an open endpoint vanishes.
        return (self.stage in {"usb_handshake", "usb_bulk_read", "usb_completion"} and
                self.backend_error_type == "USBError" and self.backend_error_code == -5)


class DestinationIOError(OSError):
    """A local output write/flush/rename failure; never retry as camera USB."""


class USBCloseError(USBReadError):
    """Camera USB cleanup failed after the requested operation completed."""


class UsbBackend:
    """PyUSB adapter; imported only when a live USB operation is requested."""

    def __init__(self, serial: str | tuple[str, ...] = SERIAL, *, backend=None, attach_only: bool = False):
        try:
            import usb.core
            import usb.util
            import usb.backend.libusb1
        except ImportError as exc:
            raise FastbootError("PyUSB and libusb are required: python3 -m pip install pyusb") from exc
        self.util = usb.util
        bundled_libusb = Path(getattr(sys, "_MEIPASS", "")) / "libusb-1.0.0.dylib"
        if backend is None and getattr(sys, "frozen", False) and bundled_libusb.is_file():
            backend = usb.backend.libusb1.get_backend(find_library=lambda _: str(bundled_libusb))
        try:
            matches = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID,
                                         backend=backend) or [])
        except usb.core.USBError as exc:
            raise USBReadError(f"Fastboot USB enumeration interrupted: {exc}", exc,
                               stage="usb_open") from exc
        if not matches:
            raise FastbootEnumerationUnavailable("Camera RAM Fastboot USB device unavailable")
        candidates = matches
        candidate_count = len(matches)
        identified = []
        transient_descriptor_error = False
        fatal_descriptor_error = None
        for device in matches:
            try:
                # Device.langids masks every USBError as an empty tuple, after
                # which get_string raises an ambiguous ValueError. Read the
                # descriptor directly so permission and disconnect differ.
                languages = usb.util.get_langids(device)
                if not languages:
                    fatal_descriptor_error = FastbootIdentityError(
                        "Fastboot serial LANGID absent; cannot verify device identity")
                    continue
                observed = usb.util.get_string(device, device.iSerialNumber, languages[0])
            except usb.core.USBError as exc:
                code = getattr(exc, "backend_error_code", None)
                if exc.errno in {errno.EACCES, errno.EINVAL, errno.ENOTSUP} or code in {-3, -2, -12}:
                    fatal_descriptor_error = FastbootIdentityError(f"Fastboot serial descriptor access failed: {exc}")
                    continue
                if exc.errno not in {errno.EIO, errno.ENODEV, errno.ETIMEDOUT} and code not in {-1, -4, -7, -99}:
                    fatal_descriptor_error = FastbootIdentityError(f"Fastboot serial descriptor failed: {exc}")
                    continue
                transient_descriptor_error = True
                continue
            if observed in ((serial,) if isinstance(serial, str) else serial):
                identified.append(device)
        matches = identified
        # A descriptor lookup may have opened a PyUSB handle even when its
        # serial was unreadable or wrong. Release every candidate that will
        # not become the sole claimed transport before another bridge reset.
        keep = matches[0] if len(matches) == 1 and not (
            candidate_count > 1 and (fatal_descriptor_error or transient_descriptor_error)) else None
        for candidate in candidates:
            if candidate is keep:
                continue
            try:
                usb.util.dispose_resources(candidate)
            except OSError as exc:
                failure = USBReadError(f"Fastboot USB descriptor cleanup failed: {exc}", exc,
                                       stage="usb_open")
                failure.usb_cleanup_dispose_failed = True
                raise failure from exc
        if candidate_count > 1 and (fatal_descriptor_error is not None or transient_descriptor_error):
            raise FastbootIdentityError("ambiguous Fastboot USB serial descriptors; cannot verify unique device")
        if not matches and fatal_descriptor_error is not None:
            raise fatal_descriptor_error
        if not matches and transient_descriptor_error:
            raise FastbootEnumerationUnavailable(
                "RAM Fastboot USB device unavailable: serial descriptor I/O interrupted")
        if len(matches) != 1:
            raise FastbootIdentityError(f"expected one Fastboot USB device with serial {serial!r}; found {len(matches)}")
        self.device = matches[0]
        try:
            if attach_only:
                if self.device.get_active_configuration().bConfigurationValue != 1:
                    raise FastbootError("existing Fastboot USB configuration is unavailable")
            else:
                try:
                    if self.device.get_active_configuration().bConfigurationValue != 1:
                        self.device.set_configuration(1)
                except usb.core.USBError:
                    self.device.set_configuration(1)
            usb.util.claim_interface(self.device, 0)
        except FastbootError:
            usb.util.dispose_resources(self.device)
            raise
        except usb.core.USBError as exc:
            wrapped = USBReadError(f"Fastboot USB open interrupted: {exc}", exc,
                                   stage="usb_open")
            try:
                usb.util.dispose_resources(self.device)
            except OSError as dispose_error:
                wrapped.usb_cleanup_error = str(dispose_error)
                wrapped.usb_cleanup_dispose_failed = True
            raise wrapped from exc

    def write(self, data: bytes, timeout_ms: int = TIMEOUT_MS) -> int:
        return self.device.write(0x01, data, timeout=timeout_ms)

    def read(self, count: int, timeout_ms: int = TIMEOUT_MS) -> bytes:
        return bytes(self.device.read(0x81, count, timeout=timeout_ms))

    def close(self):
        release_error = None
        dispose_error = None
        try:
            self.util.release_interface(self.device, 0)
        except OSError as exc:
            release_error = exc
        try:
            self.util.dispose_resources(self.device)
        except OSError as exc:
            dispose_error = exc
        if release_error is not None or dispose_error is not None:
            _record_usb_cleanup_failure(release_error, dispose_error)
            failure = release_error or dispose_error
            failure.usb_cleanup_dispose_failed = dispose_error is not None
            raise failure


class FastbootTransport:
    def __init__(self, backend=None, *, require_product: bool = True,
                 camera_model: str = PRODUCT):
        self.camera_model = camera_model
        self.expected_products = fastboot_products(camera_model)
        serials = ((SERIAL, "ILCE7RM5-fastboot") if camera_model == "ILCE-7RM5" else SERIAL)
        self.backend = backend if backend is not None else UsbBackend(serial=serials)
        self._verified = False
        if require_product:
            try:
                self.verify_product()
            except BaseException as original:
                if isinstance(original, OSError) and not isinstance(original, USBReadError):
                    wrapped = USBReadError(f"Fastboot product USB handshake interrupted: {original}",
                                           original, stage="usb_product")
                    wrapped.__cause__ = original
                    original = wrapped
                try:
                    self.close()
                except OSError as close_error:
                    original.usb_cleanup_error = str(close_error)
                    original.usb_cleanup_dispose_failed = bool(
                        getattr(close_error, "usb_cleanup_dispose_failed", False))
                raise original

    def close(self):
        self.backend.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except OSError as close_error:
            if exc_type is not None:
                if exc_value is not None:
                    exc_value.usb_cleanup_error = str(close_error)
                    exc_value.usb_cleanup_dispose_failed = bool(
                        getattr(close_error, "usb_cleanup_dispose_failed", False))
                return False  # Preserve the read/write error that caused cleanup.
            failure = USBCloseError(f"camera USB cleanup failed after transfer: {close_error}",
                                    close_error)
            failure.usb_cleanup_dispose_failed = bool(
                getattr(close_error, "usb_cleanup_dispose_failed", False))
            raise failure from close_error

    def _send(self, command: str):
        encoded = command.encode("ascii")
        if not 1 <= len(encoded) <= 64 or not re.fullmatch(rb"[\x20-\x7e]+", encoded):
            raise ValueError("Fastboot command must be 1–64 printable ASCII bytes")
        if self.backend.write(encoded, TIMEOUT_MS) != len(encoded):
            raise FastbootError("short Fastboot command write")

    def _reply(self, allowed=(b"OKAY",)) -> tuple[bytes, bytes]:
        for _ in range(128):
            data = self.backend.read(64, TIMEOUT_MS)
            if len(data) < 4 or len(data) > 64:
                raise FastbootError(f"invalid Fastboot response length {len(data)}")
            tag, value = data[:4], data[4:]
            if tag == b"INFO":
                continue
            if tag == b"FAIL":
                raise FastbootError("Fastboot rejected command: " + value.decode("utf-8", "replace"))
            if tag not in allowed:
                raise FastbootError(f"unexpected Fastboot response {tag!r}")
            return tag, value
        raise FastbootError("too many Fastboot INFO responses")

    def command(self, command: str) -> bytes:
        if not (command.startswith("getvar:") or command.startswith("oem partition dump ")
                or command.startswith("oem cetus dumpnor:") or command == "upload"):
            raise ValueError("read-only transport refuses this Fastboot command")
        self._send(command)
        return self._reply()[1]

    def verify_product(self) -> str:
        name = self.command("getvar:product").decode("ascii", "strict")
        if name not in self.expected_products:
            raise FastbootIdentityError(
                f"unexpected Fastboot product {name!r} for selected {self.camera_model}; "
                f"expected one of {self.expected_products!r}")
        self._verified = True
        return name

    def region_size(self, region: str) -> int:
        if not self._verified:
            self.verify_product()
        if not valid_region(region):
            raise ValueError(f"unsupported region {region!r}")
        if region == "cp-nor":
            return 64 * 1024 * 1024
        value = self.command(f"getvar:partition-size:{region}").decode("ascii", "strict")
        if not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", value):
            raise FastbootError(f"invalid partition size {value!r}")
        size = int(value, 16)
        if not size or size % 512:
            raise FastbootError(f"invalid partition size {size}")
        return size

    def read_region(self, region: str, output: str | Path, *, offset: int = 0,
                    length: int | None = None) -> int:
        """Stage and stream a region; SDM2 nflashaN is decrypted by the payload.

        Preserve an incomplete .partial on failure.
        """
        size = self.region_size(region)
        length = size - offset if length is None else length
        if (offset < 0 or length <= 0 or offset % 512 or length % 512
                or offset > size or length > size - offset or length > 0xFFFFFFFF):
            raise ValueError("read offset/length must be sector aligned and inside region")
        output = Path(output)
        partial = output.with_name(output.name + ".partial")
        if output.exists() or partial.exists():
            raise FileExistsError(f"output or incomplete capture already exists: {output}")
        if region == "cp-nor":
            command = f"oem cetus dumpnor:{offset:x}:{length:x}"
            chunk_size = 4096
        else:
            command = f"oem partition dump {region} {offset // 512:x} {length // 512:x}"
            chunk_size = 65536
        with partial.open("xb") as stream:
            try:
                self.command(command)
                self._send("upload")
                _, value = self._reply((b"DATA",))
            except OSError as exc:
                raise USBReadError(f"camera USB upload handshake failed for {region} at {offset}: {exc}",
                                   exc, stage="usb_handshake") from exc
            if not re.fullmatch(rb"[0-9a-fA-F]{8}", value) or int(value, 16) != length:
                raise FastbootError("upload DATA length differs from requested read")
            remaining = length
            while remaining:
                want = min(remaining, chunk_size)
                try:
                    data = self.backend.read(want, TIMEOUT_MS)
                except OSError as exc:
                    raise USBReadError(f"camera USB bulk read failed for {region} at {offset + length - remaining}: {exc}",
                                       exc, stage="usb_bulk_read") from exc
                if not data or len(data) > want:
                    raise FastbootError(f"invalid data chunk at offset {length - remaining}")
                try:
                    stream.write(data)
                except OSError as exc:
                    raise DestinationIOError(exc.errno, f"backup destination write failed at {partial}: {exc}") from exc
                remaining -= len(data)
            try:
                self._reply()
            except OSError as exc:
                raise USBReadError(f"camera USB upload completion failed for {region}: {exc}",
                                   exc, stage="usb_completion") from exc
            try:
                stream.flush()
                os.fsync(stream.fileno())
            except OSError as exc:
                raise DestinationIOError(exc.errno, f"backup destination flush failed at {partial}: {exc}") from exc
        try:
            partial.replace(output)
        except OSError as exc:
            raise DestinationIOError(exc.errno, f"backup destination rename failed at {output}: {exc}") from exc
        return length


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only ILCE-7M4 RAM Fastboot access")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("identify")
    size = sub.add_parser("size")
    size.add_argument("region", help="nflasha, nflashaB0/B1, nflashaN, or cp-nor")
    read = sub.add_parser("read")
    read.add_argument("region", help="nflasha, nflashaB0/B1, nflashaN, or cp-nor")
    read.add_argument("output", type=Path)
    read.add_argument("--offset", type=lambda s: int(s, 0), default=0)
    read.add_argument("--length", type=lambda s: int(s, 0))
    args = parser.parse_args(argv)
    try:
        with FastbootTransport() as transport:
            if args.action == "identify":
                print(PRODUCT)
            elif args.action == "size":
                print(transport.region_size(args.region))
            else:
                print(transport.read_region(args.region, args.output,
                                            offset=args.offset, length=args.length))
        return 0
    except (FastbootError, OSError, ValueError) as exc:
        parser.exit(1, f"fastboot: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
