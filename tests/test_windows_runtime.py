"""Offline contracts for the Windows adapter and durable file replacement."""
import ctypes
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recovery_tool import durable_file


class WindowsDurabilityTests(unittest.TestCase):
    def test_windows_uses_native_write_through_replace(self):
        move = Mock(return_value=1)
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp)/'source', Path(tmp)/'target'
            source.write_text('durable')
            with patch.object(durable_file, 'os', SimpleNamespace(name='nt')), \
                 patch.object(ctypes, 'WinDLL', return_value=SimpleNamespace(MoveFileExW=move), create=True):
                durable_file.durable_replace(source, target)
            move.assert_called_once_with(str(source.resolve()), str(target.resolve()), 9)

    def test_posix_replacement_persists_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp)/'source', Path(tmp)/'target'
            source.write_bytes(b'new'); target.write_bytes(b'old')
            durable_file.durable_replace(source, target)
            self.assertEqual(target.read_bytes(), b'new')
            self.assertFalse(source.exists())


class WindowsWorkerTests(unittest.TestCase):
    def _entry(self):
        spec = importlib.util.spec_from_file_location('windows_entry_test', Path(__file__).parents[1]/'windows/worker_entry.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        return entry

    def test_backup_fastboot_state_requires_unambiguous_pnp_result(self):
        entry = self._entry()
        with patch.object(entry.subprocess, 'CREATE_NO_WINDOW', 0, create=True), \
             patch.object(entry.subprocess, 'run') as run:
            for response, expected in (("present", "present"), ("departed", "departed"),
                                       ("unknown", "unknown"), ("", "unknown"), ("present\ndeparted", "unknown")):
                with self.subTest(response=response):
                    run.return_value = SimpleNamespace(stdout=response + "\n")
                    self.assertEqual(entry.backup_fastboot_state(), expected)
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            self.assertIn("default { 'unknown' }", run.call_args.args[0][-1])
            run.side_effect = subprocess.TimeoutExpired("powershell.exe", 5)
            self.assertEqual(entry.backup_fastboot_state(), "unknown")


    def test_adapter_forwards_normal_return_safety_flags(self):
        # Run install with host-only services mocked; never enumerate USB.
        entry = self._entry()
        from recovery_tool import card_connection, task_worker
        names = ('APP_ROOT','TASK_ROOT','MODE_JOURNAL','TASK_REGISTRY','mounted_usb_volumes')
        original = {name:getattr(card_connection,name) for name in names}
        worker_names = ('APP_ROOT','TASK_ROOT','MODE_JOURNAL','prepare_card_connection','return_normal','prepare_sd_card','backup_fastboot_state')
        worker_original = {name:getattr(task_worker,name) for name in worker_names}
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(entry, 'os', SimpleNamespace(name='nt', environ={'LOCALAPPDATA': tmp})), \
                 patch.object(entry, '_install_fcntl_lock'), patch.object(entry, '_install_libusb'), \
                 patch.object(card_connection, 'return_normal', return_value={'ready':True}) as normal:
                entry.install()
                self.assertIs(task_worker.backup_fastboot_state, entry.backup_fastboot_state)
                self.assertEqual(task_worker.APP_ROOT, Path(tmp)/'Camera Recovery')
                task_worker.return_normal(journal_path=Path(tmp)/'journal.json',
                                          ignore_historical_tasks=True, once_only=True)
                self.assertTrue(normal.call_args.kwargs['ignore_historical_tasks'])
                self.assertTrue(normal.call_args.kwargs['once_only'])
                self.assertEqual(normal.call_args.kwargs['task_root'], Path(tmp)/'Camera Recovery/Tasks')
        finally:
            for name,value in original.items():setattr(card_connection,name,value)
            for name,value in worker_original.items():setattr(task_worker,name,value)


if __name__ == '__main__':
    unittest.main()
