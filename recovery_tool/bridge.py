"""RP2040 uart_boot bridge protocol. No camera action occurs on import/discovery."""

from __future__ import annotations

import argparse
import struct
import time
import zlib
from pathlib import Path

VERSIONS = {b"uart_boot 1", b"uart_boot 2"}
LOAD_ADDR = 0xFE020000
MAX_RECORD_PAYLOAD = 512
RECORD_GAP = 0.0144
TRANSMIT_START = 0.5


class BridgeError(RuntimeError):
    pass


class RamPayloadNotReady(BridgeError):
    """Bridge load finished, but the expected camera UART marker was absent."""
    pass


def list_ports() -> list[str]:
    """List CDC serial ports; this does not probe or reset a device."""
    try:
        from serial.tools import list_ports as serial_ports
    except ImportError as exc:
        raise BridgeError("pyserial is required: python3 -m pip install pyserial") from exc
    return sorted(port.device for port in serial_ports.comports() if port.vid == 0x2E8A)


def record(kind: int, value: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<IHI", 10 + len(payload), kind, value) + payload
    return body + struct.pack("<I", zlib.crc32(body))


def boot_records(payload: bytes, addr: int = LOAD_ADDR, entry: int | None = None,
                 unlock_code: int | None = None):
    """Yield David's ROM records, including the camera specific unlock bypass."""
    if not payload:
        raise ValueError("empty payload")
    if addr < 0 or addr + len(payload) > 0x100000000:
        raise ValueError("payload address exceeds 32-bit range")
    yield record(0x0123, 0)
    for off in range(0, len(payload), MAX_RECORD_PAYLOAD):
        yield record(1, addr + off, payload[off:off + MAX_RECORD_PAYLOAD])
    if unlock_code is None:
        unlock_code = 0xDEADBEEF
        val = unlock_code.to_bytes(4, "little")
        val += (zlib.crc32(val) ^ 0xFFFFFFFF).to_bytes(4, "little")
        yield record(1, 0xFE02F398, val)
    yield record(0xFFFF, addr if entry is None else entry)
    yield record(0, unlock_code)
    yield record(0x0123, 0)


class Bridge:
    def __init__(self, port: str, timeout: float = 5.0, serial_factory=None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if serial_factory is None:
            try:
                import serial
            except ImportError as exc:
                raise BridgeError("pyserial is required: python3 -m pip install pyserial") from exc
            serial_factory = serial.Serial
        self.serial = serial_factory(port, 115200, timeout=min(timeout, 0.25))
        self.timeout = timeout

    def close(self):
        self.serial.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, kind: int, data: bytes = b""):
        if len(data) > 0xFFFF:
            raise ValueError("bridge packet too large")
        packet = bytes((kind,)) + struct.pack("<H", len(data)) + data
        if self.serial.write(packet) != len(packet):
            raise BridgeError("short serial write")

    def _exact(self, count: int, deadline: float) -> bytes:
        out = bytearray()
        while len(out) < count:
            if time.monotonic() >= deadline:
                raise BridgeError("bridge response timed out")
            chunk = self.serial.read(count - len(out))
            if chunk:
                out.extend(chunk)
        return bytes(out)

    def recv(self, deadline: float | None = None) -> tuple[int, bytes]:
        deadline = time.monotonic() + self.timeout if deadline is None else deadline
        header = self._exact(3, deadline)
        length = int.from_bytes(header[1:], "little")
        if length > 4100:
            raise BridgeError(f"invalid bridge response length {length}")
        return header[0], self._exact(length, deadline)

    def ack(self, on_uart=None) -> bytes:
        deadline = time.monotonic() + self.timeout
        while True:
            kind, data = self.recv(deadline)
            if kind == 0x81:
                return data
            if kind == 0x82:
                raise BridgeError("bridge error: " + data.decode("utf-8", "replace"))
            if kind == 0x83:
                if on_uart is not None:
                    on_uart(data)
                continue
            raise BridgeError(f"unexpected bridge event 0x{kind:02x}")

    def ping(self, on_uart=None) -> bytes:
        self.send(1)
        version = self.ack(on_uart=on_uart)
        if version not in VERSIONS:
            raise BridgeError(f"unexpected bridge firmware version: {version!r}")
        return version

    def reset(self, on_uart=None):
        self.send(2)
        self.ack(on_uart=on_uart)

    def carrier(self, enabled: bool, on_uart=None):
        self.send(3, bytes((bool(enabled),)))
        self.ack(on_uart=on_uart)

    def blob(self, data: bytes, on_uart=None):
        if len(data) > 4100:
            raise ValueError("ROM record exceeds bridge limit")
        self.send(4, data)
        self.ack(on_uart=on_uart)

    def baud(self, rate: int, on_uart=None):
        self.send(5, struct.pack("<I", rate))
        self.ack(on_uart=on_uart)

    def boot_payload(self, payload: bytes, *, addr: int = LOAD_ADDR,
                     entry: int | None = None, unlock_code: int | None = None,
                     sleep=time.sleep, on_stage=None, on_uart=None):
        """Reset camera and send RAM payload; caller must explicitly request this."""
        version = self.ping(on_uart=on_uart)
        if on_stage is not None:
            on_stage("ping_ack", version)
        self.reset(on_uart=on_uart)
        if on_stage is not None:
            on_stage("reset_ack", b"")
        self.carrier(True, on_uart=on_uart)
        if on_stage is not None:
            on_stage("carrier_on_ack", b"")
        try:
            sleep(TRANSMIT_START)
            records = list(boot_records(payload, addr, entry, unlock_code))
            for index, item in enumerate(records):
                self.blob(item, on_uart=on_uart)
                if on_stage is not None:
                    on_stage("record_ack", b"")
                if index + 1 < len(records):
                    sleep(RECORD_GAP)
        finally:
            self.carrier(False, on_uart=on_uart)
            if on_stage is not None:
                on_stage("carrier_off_ack", b"")

    def wait_uart(self, needle: bytes, timeout: float, on_uart=None) -> bool:
        if not needle or timeout <= 0:
            raise ValueError("needle and timeout must be positive")
        self.baud(115200, on_uart=on_uart)
        deadline = time.monotonic() + timeout
        line = bytearray()
        try:
            while time.monotonic() < deadline:
                try:
                    kind, data = self.recv(min(deadline, time.monotonic() + self.timeout))
                except BridgeError as exc:
                    if "timed out" in str(exc):
                        continue
                    raise
                if kind != 0x83:
                    raise BridgeError(f"unexpected event while reading UART: 0x{kind:02x}")
                if on_uart is not None:
                    on_uart(data)
                line.extend(data)
                end = line.rfind(b"\n")
                if end >= 0:
                    if needle in line[:end]:
                        return True
                    del line[:end + 1]
            return False
        finally:
            self.baud(0, on_uart=on_uart)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or explicitly boot RP2040 uart_boot bridge")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("ports")
    ping = sub.add_parser("ping")
    ping.add_argument("port")
    boot = sub.add_parser("boot")
    boot.add_argument("port")
    boot.add_argument("payload", type=Path)
    boot.add_argument("--expect", default="fastboot ready")
    boot.add_argument("--expect-timeout", type=float, default=20)
    args = parser.parse_args(argv)
    try:
        if args.action == "ports":
            print("\n".join(list_ports()))
            return 0
        with Bridge(args.port) as bridge:
            if args.action == "ping":
                print(bridge.ping().decode("ascii"))
                return 0
            bridge.boot_payload(args.payload.read_bytes())
            if bridge.wait_uart(args.expect.encode(), args.expect_timeout,
                                on_uart=lambda data: print(data.decode("utf-8", "replace"), end="")):
                return 0
            raise BridgeError(f"UART did not report {args.expect!r}")
    except (BridgeError, OSError, ValueError) as exc:
        parser.exit(1, f"bridge: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
