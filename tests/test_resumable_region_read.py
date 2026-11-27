"""Offline-only tests for checkpointed transaction preimage reads."""

import json
from pathlib import Path
import tempfile
import unittest

from recovery_tool.resumable_region_read import read_full_region


class FakeTransport:
    def __init__(self, image: bytes, *, fail_at=None):
        self.image = image
        self.fail_at = fail_at
        self.read_offsets = []

    def region_size(self, region):
        assert region == "nflasha2"
        return len(self.image)

    def read_region(self, region, output, *, offset, length):
        assert region == "nflasha2"
        self.read_offsets.append(offset)
        if offset == self.fail_at:
            raise OSError(5, "offline injected read interruption")
        Path(output).write_bytes(self.image[offset:offset + length])
        return length


class TestResumableRegionRead(unittest.TestCase):
    image = b"A" * 1024 + b"B" * 1024 + b"C" * 1024

    def test_interrupted_read_resumes_only_missing_chunks_and_assembles(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nflasha2.live.bin"
            first = FakeTransport(self.image, fail_at=1024)
            with self.assertRaises(OSError):
                read_full_region(first, "nflasha2", output, len(self.image), chunk_bytes=1024)
            self.assertEqual(first.read_offsets, [0, 1024])
            self.assertFalse(output.exists())
            second = FakeTransport(self.image)
            result = read_full_region(second, "nflasha2", output, len(self.image), chunk_bytes=1024)
            self.assertEqual(second.read_offsets, [1024, 2048])
            self.assertEqual(output.read_bytes(), self.image)
            self.assertEqual(result["segments"], 3)
            third = FakeTransport(self.image)
            self.assertEqual(read_full_region(third, "nflasha2", output, len(self.image), chunk_bytes=1024)["sha256"],
                             result["sha256"])
            self.assertEqual(third.read_offsets, [])

    def test_temporary_leaf_stays_under_windows_limit_at_observed_depth(self):
        class WindowsLimitTransport(FakeTransport):
            def read_region(self, region, output, *, offset, length):
                # Reproduce the failed 266-character path with the former 69-character leaf.
                windows_length = 196 + 1 + len(Path(output).name + ".partial")
                self_test.assertLess(windows_length, 260)
                return super().read_region(region, output, offset=offset, length=length)

        self_test = self
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nflasha2.live.bin"
            read_full_region(WindowsLimitTransport(self.image), "nflasha2", output,
                             len(self.image), chunk_bytes=1024)
            self.assertEqual(output.read_bytes(), self.image)

    def test_tampered_or_missing_saved_chunk_refuses_reuse(self):
        for mutation in ("tamper", "missing"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "nflasha2.live.bin"
                read_full_region(FakeTransport(self.image), "nflasha2", output,
                                 len(self.image), chunk_bytes=1024)
                chunk = Path(str(output) + ".segments") / "0000000000000000.bin"
                if mutation == "tamper":
                    chunk.write_bytes(b"X" * 1024)
                else:
                    chunk.unlink()
                with self.assertRaisesRegex(ValueError, "saved full-region segment differs"):
                    read_full_region(FakeTransport(self.image), "nflasha2", output,
                                     len(self.image), chunk_bytes=1024)

    def test_invalid_offset_path_and_existing_output_refuse(self):
        for mutation in ("offset", "path", "output"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "nflasha2.live.bin"
                read_full_region(FakeTransport(self.image), "nflasha2", output,
                                 len(self.image), chunk_bytes=1024)
                receipt_path = Path(str(output) + ".segments.json")
                receipt = json.loads(receipt_path.read_text())
                if mutation == "offset":
                    receipt["segments"][1]["offset"] = 999
                elif mutation == "path":
                    receipt["segments"][0]["file"] = "../../other.bin"
                else:
                    output.write_bytes(b"Z" * len(self.image))
                    receipt.pop("sha256")
                receipt_path.write_text(json.dumps(receipt))
                with self.assertRaises(ValueError):
                    read_full_region(FakeTransport(self.image), "nflasha2", output,
                                     len(self.image), chunk_bytes=1024)

    def test_transport_partial_is_preserved_but_never_reused(self):
        class PartialTransport(FakeTransport):
            def read_region(self, region, output, *, offset, length):
                self.read_offsets.append(offset)
                Path(str(output) + ".partial").write_bytes(b"X" * 100)
                raise OSError(5, "offline injected USB interruption")

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nflasha2.live.bin"
            with self.assertRaises(OSError):
                read_full_region(PartialTransport(self.image), "nflasha2", output,
                                 len(self.image), chunk_bytes=1024)
            segment_dir = Path(str(output) + ".segments")
            self.assertTrue(any(path.name.endswith(".partial.interrupted")
                                for path in segment_dir.iterdir()))
            self.assertFalse(any(path.name.endswith(".bin") for path in segment_dir.iterdir()))
            retry = FakeTransport(self.image)
            read_full_region(retry, "nflasha2", output, len(self.image), chunk_bytes=1024)
            self.assertEqual(retry.read_offsets, [0, 1024, 2048])
            self.assertEqual(output.read_bytes(), self.image)

    def test_file_only_fake_transport_still_requires_exact_segment_size(self):
        class FileOnlyTransport(FakeTransport):
            def read_region(self, region, output, *, offset, length):
                self.read_offsets.append(offset)
                Path(output).write_bytes(self.image[offset:offset + length])

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nflasha2.live.bin"
            read_full_region(FileOnlyTransport(self.image), "nflasha2", output,
                             len(self.image), chunk_bytes=1024)
            self.assertEqual(output.read_bytes(), self.image)

        class ShortFileTransport(FileOnlyTransport):
            def read_region(self, region, output, *, offset, length):
                Path(output).write_bytes(b"X" * (length - 1))

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nflasha2.live.bin"
            with self.assertRaisesRegex(ValueError, "short full-region"):
                read_full_region(ShortFileTransport(self.image), "nflasha2", output,
                                 len(self.image), chunk_bytes=1024)


if __name__ == "__main__":
    unittest.main()
