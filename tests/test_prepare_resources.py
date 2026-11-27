"""Local resource preparation never writes outside its selected project root."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_resources", ROOT / "tools/prepare_resources.py")
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


class ResourceInstallTests(unittest.TestCase):
    def test_conflicting_resource_does_not_partially_install_new_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".local") as folder:
            base = Path(folder)
            (base / "conflict").write_bytes(b"existing")
            with patch.object(resources, "ROOT", base):
                with self.assertRaisesRegex(ValueError, "Existing resource differs"):
                    resources.install_resources({"new-resource": b"new", "conflict": b"different"})
            self.assertFalse((base / "new-resource").exists())
            self.assertEqual((base / "conflict").read_bytes(), b"existing")

    def test_symlink_parent_cannot_redirect_resource_write(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".local") as folder:
            base = Path(folder)
            project, outside = base / "project", base / "outside"
            project.mkdir()
            outside.mkdir()
            try:
                (project / "resources").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks requires local permission")
            with patch.object(resources, "ROOT", project):
                with self.assertRaisesRegex(ValueError, "escapes the project"):
                    resources.install_resources({"resources/file.bin": b"new"})
            self.assertFalse((outside / "file.bin").exists())

    def test_matching_partial_extraction_is_resumable(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".local") as folder:
            base = Path(folder)
            (base / "existing").write_bytes(b"same")
            with patch.object(resources, "ROOT", base):
                resources.install_resources({"existing": b"same", "nested/new": b"new"})
            self.assertEqual((base / "existing").read_bytes(), b"same")
            self.assertEqual((base / "nested/new").read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
