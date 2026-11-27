"""Terminal client for the same task worker used by the macOS app."""

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys

from .decrypt_snapshot import verify_snapshot
from .snapshot import REGIONS
from .task_backend import _restore_snapshot_folder


def worker_request(action, **fields):
    command = ([sys.executable] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "recovery_tool.task_worker"])
    terminal = None
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          encoding="utf-8", start_new_session=True) as child:
        child.stdin.write(json.dumps({"action": action, **fields}) + "\n")
        child.stdin.close()
        for line in child.stdout:
            message = json.loads(line)
            if message["type"] == "progress":
                event = message["event"]
                if event.get("message"):
                    print(event["message"], file=sys.stderr, flush=True)
            elif message["type"] in {"result", "error"}:
                terminal = message
        exit_code = child.wait()
        if exit_code != 0 or terminal is None:
            raise RuntimeError(f"Task worker ended without a result (exit {exit_code}). Check the task directory.")
    return terminal.get("result", terminal)


def parser():
    root = argparse.ArgumentParser(prog="camera-recovery",
                                   description="Sony A7 IV backup and recovery for macOS.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check the adapter and camera connection")
    backup = commands.add_parser("backup", help="Create or explicitly resume a backup")
    target = backup.add_mutually_exclusive_group(required=True)
    target.add_argument("--output", type=Path, help="New backup task directory")
    target.add_argument("--resume", type=Path, help="Existing full backup task directory")
    recapture = commands.add_parser("backup-recapture", help="Read a new snapshot after saved content changed")
    recapture.add_argument("prior_snapshot", type=Path, help="Previous incomplete snapshot directory")
    recapture.add_argument("--output", type=Path, required=True, help="New capture directory")
    recapture.add_argument("--region", choices=tuple(REGIONS),
                           help="Read only this region and return a partial backup")
    recapture.add_argument("--same-camera", required=True, action="store_true",
                           help="Confirm the original physical camera is still connected and in Fastboot")
    archive = commands.add_parser("backup-archive", help="Verify and archive raw regions across capture sessions")
    archive.add_argument("prior_snapshot", type=Path, help="Original backup snapshot directory")
    archive.add_argument("--emmc", type=Path, required=True, help="Verified regional nflasha recapture")
    archive.add_argument("--supplement", type=Path, action="append", default=[],
                         help="Verified capture of a missing region; repeat as needed")
    archive.add_argument("--output", type=Path, required=True, help="New archive directory")
    archive.add_argument("--same-camera", required=True, action="store_true",
                         help="Confirm every input was captured from the same physical camera")
    archive_verify = commands.add_parser("backup-archive-verify", help="Verify all raw pairs in an offline archive")
    archive_verify.add_argument("archive", type=Path)
    verify = commands.add_parser("verify", help="Verify a backup locally")
    verify.add_argument("snapshot", type=Path, help="Snapshot or its containing backup task directory")
    restore = commands.add_parser("restore", help="Restore a same-camera snapshot")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("--output", type=Path, required=True, help="New restore task directory")
    restore.add_argument("--yes", action="store_true", required=True, help="Confirm camera writes")
    sd = commands.add_parser("sd-plan", help="Prepare and show a stock SD recovery plan")
    sd.add_argument("--output", type=Path, required=True, help="New SD recovery task directory")
    sd.add_argument("--dat", type=Path, required=True, help="Local official BODYDATA.DAT")
    sd.add_argument("--sd-ready", action="store_true", required=True,
                    help="Confirm the matching BODYDATA.DAT is already in camera slot 1")
    apply = commands.add_parser("sd-apply", help="Apply a reviewed stock SD recovery plan")
    apply.add_argument("task", type=Path)
    apply.add_argument("--confirm-plan", required=True, help="Exact plan digest returned by sd-plan")
    confirm = commands.add_parser("sd-confirm", help="Record that the camera displayed update complete")
    confirm.add_argument("task", type=Path)
    confirm.add_argument("--complete", required=True, action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    interrupted = False

    def interrupt(_signum, _frame):
        nonlocal interrupted
        if not interrupted:
            print("Waiting for the current worker action to finish before exiting.",
                  file=sys.stderr, flush=True)
        interrupted = True

    def request(action, **fields):
        result = worker_request(action, **fields)
        return result, not interrupted

    def prepare(kind, output, **inputs):
        return request("prepare", kind=kind, inputs=inputs, output_dir=str(output))

    def backup():
        job = args.resume or args.output
        if args.resume:
            task = json.loads((job / "task.json").read_text())
            if task.get("kind") != "full_backup":
                raise ValueError("Select a full backup task directory to resume.")
        else:
            prepared, keep_going = prepare("full_backup", job)
            if not keep_going or prepared.get("status") != "READY":
                return prepared
        for action, ready in (("reconcile", "READY"), ("apply", "VERIFIED"), ("verify", "VERIFIED")):
            result, keep_going = request(action, job_dir=str(job))
            if not keep_going or result.get("status") != ready:
                break
        return result

    old = signal.signal(signal.SIGINT, interrupt)
    try:
        if args.command == "doctor":
            result, _ = request("connection_probe")
        elif args.command == "backup":
            result = backup()
        elif args.command == "backup-recapture":
            result, _ = request("backup_recapture", prior_snapshot=str(args.prior_snapshot),
                                output_dir=str(args.output), same_camera_confirmed=args.same_camera, region=args.region)
        elif args.command == "backup-archive":
            result, _ = request("backup_archive", prior_snapshot=str(args.prior_snapshot),
                                emmc_capture=str(args.emmc), supplements=[str(path) for path in args.supplement],
                                output_dir=str(args.output), same_camera_confirmed=args.same_camera)
        elif args.command == "backup-archive-verify":
            result, _ = request("backup_archive_verify", archive_dir=str(args.archive))
        elif args.command == "verify":
            path = _restore_snapshot_folder(args.snapshot.resolve())
            result = {"snapshot": str(path), **verify_snapshot(path)}
        elif args.command == "restore":
            result, keep_going = prepare("scoped_restore", args.output, target_snapshot=str(args.snapshot))
            if keep_going and result.get("status") == "READY":
                result, _ = request("restore_run", job_dir=str(args.output))
        elif args.command == "sd-plan":
            result, keep_going = prepare("stock_sd_recovery", args.output, dat=str(args.dat), sd_ready=True)
            if keep_going and result.get("status") == "READY":
                result, _ = request("stock_plan", job_dir=str(args.output))
        elif args.command == "sd-apply":
            result, _ = request("stock_run", job_dir=str(args.task), confirmed_plan_digest=args.confirm_plan)
        else:
            result, _ = request("confirm_stock_update_complete", job_dir=str(args.task), confirmed=True)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        result = {"status": "INCOMPLETE", "error": str(exc)}
    finally:
        signal.signal(signal.SIGINT, old)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if interrupted:
        return 130
    if result.get("type") == "error":
        return 1
    if result.get("status") in {"VERIFIED", "READY", "ALREADY_VERIFIED"} and not result.get("success_with_skips"):
        return 0
    if result.get("status") in {"NEEDS_USER_ACTION", "PARTIAL"} or result.get("success_with_skips"):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
