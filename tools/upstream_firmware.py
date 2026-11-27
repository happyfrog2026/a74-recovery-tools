"""Bounded offline upstream UFU decoding and TAR indexing.

Adapted from upstream's MIT-licensed firmware.py and
offline.py. See third_party/licenses/host-toolkit-LICENSE.
Only reads a user-provided package and writes an explicitly selected local file.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import tarfile
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Public container-format constants, not upstream signing keys.
KEY_HEADER = bytes.fromhex("e3b0c44298fc1c149afbf4c8996fb924")
KEY_K8 = bytes.fromhex("434589432d319010f1132727353286ce4d7ede8b9c4f7217a0e0226e0f0edc69")


def require(value, code, message):
    if not value:
        raise ValueError(f"{code}: {message}")


def ufu_layout(path):
    """Bounded UFU chunk parser; supports a final RSA-2048 FDAT."""
    total = Path(path).stat().st_size
    chunks = []
    with Path(path).open("rb") as stream:
        require(stream.read(8) == b"\x89UFU\r\n\x1a\n", "UFU", "Invalid UFU magic")
        pos = 8
        while pos < total:
            stream.seek(pos)
            header = stream.read(8)
            require(len(header) == 8, "UFU", "Truncated chunk header")
            size, tag = struct.unpack(">I4s", header)
            require(pos + 8 + size <= total, "UFU", "Chunk exceeds file")
            chunks.append((tag, pos + 8, size))
            pos += 8 + size
    require(len(chunks) >= 2 and chunks[-1][0] == b"DEND" and chunks[-1][2] == 4,
            "UFU", "Expected a final DEND CRC")
    fdat = [row for row in chunks if row[0] == b"FDAT"]
    require(len(fdat) == 1 and chunks[-2] == fdat[0] and fdat[0][2] >= 512 + 16 + 256,
            "UFU", "Expected exactly one FDAT immediately before DEND")
    require([r[0] for r in chunks].count(b"DEND") == 1, "UFU", "Duplicate DEND")
    return fdat[0][1], fdat[0][1] + fdat[0][2] - 256, total - 12

def prefix_crc(stream, end):
    stream.seek(0)
    crc = 0
    for start in range(0, end, 4 << 20):
        data = stream.read(min(4 << 20, end - start))
        require(data, "UFU", "Truncated input")
        crc = zlib.crc32(data, crc)
    return crc & 0xffffffff

def exact(stream, count):
    data = stream.read(count)
    require(len(data) == count, "TRUNCATED", "Unexpected end of file")
    return data

def contexts(iv, encrypt=False):
    contexts = [Cipher(algorithms.AES(KEY_HEADER), modes.ECB()), Cipher(algorithms.AES(KEY_K8), modes.CBC(iv))]
    return [c.encryptor() if encrypt else c.decryptor() for c in contexts]

def frame_batches(source):
    """Validate DEND and every UFU block; CBC spans all but the first 512 B."""
    import numpy as np
    start, signature, dend = ufu_layout(source)
    encrypted_size = signature - start - 16
    require(encrypted_size > 0 and encrypted_size % 1024 == 0, "UFU", "Invalid FDAT block length")
    count = encrypted_size // 1024
    with Path(source).open("rb") as stream:
        crc = prefix_crc(stream, dend)
        stream.seek(dend + 8)
        require(struct.unpack(">I", exact(stream, 4))[0] == crc, "UFU", "DEND CRC mismatch")
        stream.seek(signature - 16)
        iv = exact(stream, 16)
        ecb, cbc = contexts(iv)
        stream.seek(start)
        for base in range(0, count, 4096):
            number = min(4096, count - base)
            ct = exact(stream, number * 1024)
            plain = ecb.update(ct[:512]) + cbc.update(ct[512:]) if base == 0 else cbc.update(ct)
            array = np.frombuffer(plain, dtype=np.uint8).reshape(number, 1024)
            words = array.view("<u2").reshape(number, 512)
            flags = words[:, 1]
            sizes = flags & 0x7fff
            full = number - 1 if base + number == count else number
            require(bool(np.all(sizes[:full] == 1020) and np.all(flags[:full] < 0x8000)), "UFU", "Nonfinal block layout mismatch")
            if full != number:
                require(int(flags[-1]) & 0x8000 and 0 < int(sizes[-1]) <= 1020, "UFU", "Invalid final block")
            require(bool(np.all((words[:, 1:].sum(axis=1, dtype=np.uint64) & 0xffff) == words[:, 0])), "UFU", "Block checksum mismatch")
            payload = array[:full, 4:].tobytes()
            if full != number:
                payload += array[-1, 4:4 + int(sizes[-1])].tobytes()
            yield base, count, array, payload, iv
        require(ecb.finalize() == b"" and cbc.finalize() == b"", "UFU", "Unexpected cipher tail")

def verify_plain_header(header, size):
    require(len(header) == 512 and header[:8] == b"UDTRFIRM", "HEADER", "Invalid UDTRFIRM header")
    require(struct.unpack_from("<I", header, 8)[0] == zlib.crc32(header[12:]), "HEADER", "Header CRC mismatch")
    require(all((v & 15) <= 9 and (v >> 4) <= 9 for v in header[0x20:0x22]), "HEADER", "Invalid BCD version")
    offset, length, number = struct.unpack_from("<III", header, 48)
    require(512 <= offset <= offset + length <= size and number <= 28, "HEADER", "Invalid firmware bounds")
    for index in range(number):
        _, off, count, _ = struct.unpack_from("<4sIII", header, 64 + 16 * index)
        require(off + count <= size, "HEADER", "Filesystem outside plaintext")
    return offset, length

def decode(source, *, output=None, compare=None):
    require(output is None or not Path(output).exists(), "EXISTS", "Plaintext output exists")
    target = Path(output).open("xb") if output else None
    reference = Path(compare).open("rb") if compare else None
    hasher, total, header, blocks = hashlib.sha256(), 0, bytearray(), 0
    try:
        for base, count, array, payload, _ in frame_batches(source):
            if reference:
                require(exact(reference, len(payload)) == payload, "COMPARE", "Decoded plaintext differs")
            if target:
                target.write(payload)
            if len(header) < 512:
                header.extend(payload[:512 - len(header)])
            hasher.update(payload)
            total += len(payload)
            blocks = count
        if reference:
            require(not reference.read(1), "COMPARE", "Reference has extra data")
        offset, length = verify_plain_header(header, total)
        return {"plaintext_sha256": hasher.hexdigest(), "plaintext_size": total, "blocks_verified": blocks,
                "firmware_offset": offset, "firmware_size": length, "compared_byte_for_byte": bool(compare),
                "camera_io": False, "rsa_verified": False}
    finally:
        if target:
            target.close()
        if reference:
            reference.close()

def tar_index(path):
    with Path(path).open("rb") as stream:
        offset, length = verify_plain_header(exact(stream, 512), Path(path).stat().st_size)
        stream.seek(offset)
        result = {}
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                require(name not in result and not name.startswith("/") and ".." not in Path(name).parts, "TAR", "Invalid or duplicate member")
                if member.isfile():
                    require(member.offset_data + member.size <= length, "TAR", "Member outside firmware region")
                    result[name] = (offset + member.offset_data, member.size)
        return result
