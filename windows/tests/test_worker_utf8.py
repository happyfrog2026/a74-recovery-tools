"""Exercise Chinese JSON paths with pipe streams initially configured as GBK."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
for stream in (sys.stdin, sys.stdout, sys.stderr):
    stream.reconfigure(encoding="gbk")
from windows.worker_entry import configure_worker_stdio
if sys.argv[2] == "fixed":
    configure_worker_stdio()
from recovery_tool import task_worker
task_worker.APP_ROOT = Path(sys.argv[3])
if sys.argv[4] == "denied":
    from unittest.mock import patch
    with patch.object(task_worker.Backend, "prepare",
                      side_effect=PermissionError(13, "拒绝访问", sys.argv[5])):
        task_worker.main()
else:
    task_worker.main()
"""


class WorkerUtf8Tests(unittest.TestCase):
    def run_worker(self, folder, mode="fixed", action="prepare"):
        output = folder / "用户" / "备份任务"
        request = {"action": "prepare", "kind": "board_install",
                   "inputs": {}, "output_dir": str(output)}
        result = subprocess.run(
            [sys.executable, "-I", "-c", PROGRAM, str(ROOT), mode,
             str(folder / "worker-root"), action, str(output)],
            input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
            capture_output=True, cwd=ROOT, check=True)
        events = [json.loads(line) for line in result.stdout.decode("utf-8", errors="replace").splitlines()]
        return output, events

    def test_chinese_task_path_survives_gbk_pipe_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            output, events = self.run_worker(Path(temporary))
            self.assertEqual(events[-1]["result"]["status"], "READY", events)
            self.assertTrue((output / "task.json").is_file())
            self.assertNotIn("\ufffd", json.dumps(events, ensure_ascii=False))

    def test_unconfigured_gbk_input_targets_a_different_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            output, _ = self.run_worker(Path(temporary), mode="old")
            self.assertFalse((output / "task.json").exists())

    def test_permission_error_reports_exact_chinese_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            output, events = self.run_worker(Path(temporary), action="denied")
            error = events[-1]
            self.assertEqual(error["type"], "error")
            self.assertEqual(error["error_type"], "PermissionError")
            self.assertEqual(error["errno"], 13)
            self.assertEqual(error["filename"], str(output))
            self.assertIn("拒绝访问", error["error"])
            self.assertIn("用户", error["error"])


if __name__ == "__main__":
    unittest.main()
