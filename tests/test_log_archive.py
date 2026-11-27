"""Focused coverage for persistent, complete, metadata-only log exports."""
import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from recovery_tool.log_archive import LogStore, SUBFOLDER


class LogArchiveTest(unittest.TestCase):
    def test_preference_and_export_cover_sessions_tasks_and_prior_location(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            prefs = root / "prefs.json"
            task = root / "camera-job"
            task.mkdir()
            store = LogStore(preferences=prefs, default_parent=first)
            store.append("Session started")
            store.append("Task prepared", job_dir=task)
            (task / "app-log.jsonl").write_text('{"message":"Legacy task log"}\n')
            (task / "journal.json").write_text('{"status":"INCOMPLETE"}\n')
            (task / "capture-receipt.json").write_text('{"status":"VERIFIED"}\n')
            (task / "nflasha.raw.bin").write_bytes(b"firmware bytes must stay private")
            (task / "verified.json").write_text('{"status":"VERIFIED"}\n')
            self.assertEqual(store.set_directory(second), second.resolve() / SUBFOLDER)
            store.append("Task resumed", job_dir=task)
            reopened = LogStore(preferences=prefs, default_parent=first)
            self.assertEqual(reopened.directory, second.resolve() / SUBFOLDER)
            reopened.append("Another app session")
            archive = root / "all-logs.zip"
            result = reopened.export(archive)
            with zipfile.ZipFile(archive) as source:
                names = source.namelist()
                text = "\n".join(source.read(name).decode("utf-8")
                                 for name in names if name.endswith(".jsonl"))
                manifest = json.loads(source.read("manifest.json"))
            self.assertEqual(result["log_files"], 8)
            self.assertEqual(len(manifest["files"]), 8)
            with zipfile.ZipFile(archive) as source:
                for entry in manifest["files"]:
                    self.assertEqual(entry["sha256"], hashlib.sha256(source.read(entry["name"])).hexdigest())
            self.assertIn("Session started", text)
            self.assertIn("Task prepared", text)
            self.assertIn("Task resumed", text)
            self.assertIn("Another app session", text)
            self.assertIn("Legacy task log", text)
            self.assertTrue(any(name.endswith("journal.json") for name in names))
            self.assertTrue(any(name.endswith("capture-receipt.json") for name in names))
            self.assertNotIn("firmware bytes", text)
            self.assertFalse(any(name.endswith((".bin", "verified.json")) for name in names))

    def test_invalid_export_suffix_and_location_do_not_change_preference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = LogStore(preferences=root / "prefs.json", default_parent=root)
            with self.assertRaises(NotADirectoryError):
                store.set_directory(root / "missing")
            self.assertEqual(store.directory, root.resolve() / SUBFOLDER)
            with self.assertRaises(ValueError):
                store.export(root / "not-a-zip.bin")
