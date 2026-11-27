"""JSON Lines subprocess adapter for desktop frontends and the separate Python runtime.

One request per line on stdin; zero or more progress lines and one final
result/error line on stdout. The process may handle multiple requests, but a
GUI may also start a fresh worker for each task action.
"""

from __future__ import annotations

import json
import fcntl
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys

from .task_backend import Backend, _stock_failure_evidence
from .sd_card_prepare import prepare as prepare_sd_card
from .card_connection import (APP_ROOT, MODE_JOURNAL, TASK_ROOT,
                              confirm_stock_update_complete,
                              confirm_stock_update_failed,
                              prepare_card_connection, register_task, return_normal)
from .transport import observe_normal_camera_usb

backup_fastboot_state = None


def _stock_plan_mode_exit(job_dir: Path, backend: Backend, progress) -> dict:
    """Report planner exit state without rebooting into a possibly failed update."""
    evidence = _stock_failure_evidence(job_dir)
    if (not evidence["journal_valid"] or
            evidence["write_may_have_occurred"] is not False or
            evidence["reboot_may_have_occurred"] is not False or
            evidence["inflight"] is not None):
        return {"normal_mode_status": "NOT_EXITED", "current_mode": "FASTBOOT_OR_UNKNOWN",
                "reboot_sent": False, "failed_check": "current_stock_task_not_safe_for_mode_exit"}
    return {"normal_mode_status": "NOT_EXITED", "current_mode": "FASTBOOT_OR_UNKNOWN",
            "reboot_sent": False, "next_action": "Review the stock plan result before leaving RAM mode"}


def send(value: dict, stream=None):
    print(json.dumps(value, ensure_ascii=False), file=stream or sys.stdout, flush=True)


def handle(request: dict, backend: Backend | None = None):
    supplied_backend = backend is not None
    if backend is None:
        backend = (Backend(backup_fastboot_state=backup_fastboot_state)
                   if backup_fastboot_state is not None else Backend())
    action = request.get("action")
    model_options = ({"camera_model": request["camera_model"]}
                     if request.get("camera_model") else {})
    protocol = sys.stdout
    progress = lambda event: send({"type": "progress", "event": event}, protocol)
    # Legacy capture helpers print human progress; keep the worker stdout a
    # strict JSON Lines channel for the GUI.
    with redirect_stdout(sys.stderr):
        if action == "prepare":
            result = backend.prepare(request["kind"], request.get("inputs", {}),
                                     Path(request["output_dir"]), progress)
            if not supplied_backend:
                register_task(Path(request["output_dir"]))
        elif action == "connection_probe":
            result = backend.connection_probe(progress, **model_options)
        elif action == "connection_enter_ram":
            result = backend.connection_enter_ram(confirmed=request.get("confirmed") is True,
                                                  progress=progress, **model_options)
        elif action == "stock_plan":
            result = backend.stock_plan(Path(request["job_dir"]), progress)
        elif action == "stock_run":
            result = backend.stock_run(Path(request["job_dir"]), progress,
                                       request.get("confirmed_plan_digest"))
        elif action == "sd_card_prepare":
            result = prepare_sd_card(request["source_dat"], request["volume"],
                                     camera_model=request.get("camera_model"),
                                     confirm_replace=request.get("confirm_replace") is True,
                                     expected_existing_sha256=request.get("expected_existing_sha256"),
                                     expected_volume_device_identifier=request.get("expected_volume_device_identifier"),
                                     receipt_dir=APP_ROOT / "SD Card Preparations")
        elif action == "prepare_card_connection":
            result = prepare_card_connection(route=request["route"], progress=progress, **model_options)
        elif action == "confirm_stock_update_complete":
            result = confirm_stock_update_complete(Path(request["job_dir"]),
                                                   confirmed=request.get("confirmed") is True)
        elif action == "confirm_stock_update_failed":
            result = confirm_stock_update_failed(Path(request["job_dir"]),
                                                 confirmed=request.get("confirmed") is True,
                                                 reason=request.get("reason"))
        elif action == "nor_offline_smoke":
            from .nor_rebuild_flow import offline_smoke
            result = offline_smoke(Path(request["capture_receipt"]), Path(request["output_dir"]))
        elif action == "backup_resume_status":
            result = backend.backup_resume_status(Path(request["job_dir"]))
        elif action == "backup_archive":
            from .backup_archive import assemble
            result = assemble(Path(request["prior_snapshot"]), Path(request["emmc_capture"]),
                              [Path(path) for path in request.get("supplements", [])],
                              Path(request["output_dir"]),
                              same_camera_confirmed=request.get("same_camera_confirmed") is True,
                              progress=progress)
        elif action == "backup_archive_verify":
            from .backup_archive import verify
            result = verify(Path(request["archive_dir"]), progress=progress)
        elif action == "backup_recapture":
            result = backend.backup_recapture(
                Path(request["prior_snapshot"]), Path(request["output_dir"]),
                same_camera_confirmed=request.get("same_camera_confirmed") is True,
                region=request.get("region"), progress=progress)
        elif action == "restore_run":
            result = backend.restore_run(Path(request["job_dir"]), progress)
        elif action in {"reconcile", "apply", "verify", "speed_probe"}:
            result = getattr(backend, action)(Path(request["job_dir"]), progress)
        else:
            raise ValueError(f"unknown backend action: {action}")
        # The verified task result describes what was read or restored. Current
        # camera USB mode is reported separately after one durable exit intent.
        if type(backend) is Backend and not result.get("normal_return_handled") and (
            (action == "connection_enter_ram" and
             result.get("data", {}).get("status") == "READY") or
            (action == "verify" and result.get("kind") == "full_backup" and
             result.get("status") == "VERIFIED") or
            (action == "restore_run" and
             result.get("status") in {"VERIFIED", "PARTIAL", "NO_WRITE"}) or
            (action == "stock_plan" and result.get("status") == "NO_WRITE") or
            (action == "apply" and result.get("kind") == "full_backup" and
             result.get("status") == "INCOMPLETE")):
            restore = action == "restore_run"
            no_write_session = (restore and
                                (result.get("status") in {"NO_WRITE", "PARTIAL"} or
                                 (result.get("status") == "VERIFIED" and
                                  not (Path(request["job_dir"]) / "scoped-stage/manifest.json").is_file())))
            failed_backup_read = (action == "apply" and result.get("kind") == "full_backup" and
                                  result.get("status") == "INCOMPLETE")
            if no_write_session or failed_backup_read:
                # Failed or partial restore is not a successful normal-mode
                # exit. Observe only; keep the current RAM session available.
                try:
                    normal = observe_normal_camera_usb()
                except Exception:
                    normal = None
                result.update({"normal_mode_status": "READY" if normal else "NEEDS_USER_ACTION",
                               "current_mode": normal["status"] if normal else "UNKNOWN",
                               "reboot_sent": False})
            elif action == "stock_plan":
                result.update(_stock_plan_mode_exit(Path(request["job_dir"]), backend, progress))
            else:
                verified_backup = (action == "verify" and result.get("kind") == "full_backup" and
                                   result.get("status") == "VERIFIED")
                journal = (Path(request["job_dir"]) / "post-backup-normal.json" if verified_backup else
                           Path(request["job_dir"]) / "post-restore-normal.json"
                           if restore and result.get("status") == "VERIFIED" else MODE_JOURNAL)
                result.update(return_normal(journal_path=journal,
                                            transport_factory=backend.transport_factory,
                                            ignore_historical_tasks=bool(verified_backup or
                                                                         (restore and result.get("status") == "VERIFIED")),
                                            once_only=bool(verified_backup or
                                                           (restore and result.get("status") == "VERIFIED")),
                                            progress=progress, **model_options))
    send({"type": "result", "result": result}, protocol)


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        request = None
        mode_after_error = {}
        try:
            request = json.loads(line)
            APP_ROOT.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(APP_ROOT / "camera-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    handle(request)
                except BaseException:
                    if request.get("action") == "stock_plan" and request.get("job_dir"):
                        # A planner exception preserves RAM mode. Do not turn a
                        # read-only planning failure into a camera reboot.
                        try:
                            mode_after_error = _stock_plan_mode_exit(
                                Path(request["job_dir"]), Backend(),
                                lambda event: send({"type": "progress", "event": event}))
                        except BaseException as mode_exc:
                            mode_after_error = {"normal_mode_status": "INCOMPLETE",
                                                "normal_mode_error": str(mode_exc)}
                    raise
            finally:
                os.close(lock_fd)
        except BaseException as exc:
            job_dir = request.get("job_dir") if isinstance(request, dict) else None
            status = "INCOMPLETE"
            details = {}
            if job_dir:
                journal = Path(job_dir) / "journal.json"
                if journal.exists():
                    try:
                        status = json.loads(journal.read_text()).get("status", status)
                    except Exception:
                        pass
            if job_dir and request.get("action") in {"stock_plan", "stock_run"}:
                evidence = _stock_failure_evidence(Path(job_dir))
                details = {**evidence, "phase": ("plan" if request["action"] == "stock_plan"
                                                else "unknown"),
                           "error_code": getattr(exc, "code", type(exc).__name__),
                           "failed_check": getattr(exc, "code", None),
                           "failure_cause": str(exc),
                           "errno": getattr(exc, "errno", None),
                           "next_action": ("Inspect this task's saved write and reboot receipts"
                                           if not evidence["journal_valid"] or
                                           evidence["write_may_have_occurred"] or
                                           evidence["reboot_may_have_occurred"] else
                                           "Check the current camera connection and pinned stock plan")}
                status = ("REBOOT_UNCERTAIN" if evidence["reboot_may_have_occurred"] is True
                          else "UNKNOWN_AFTER_WRITE" if
                          evidence["write_may_have_occurred"] is True or
                          not evidence["journal_valid"] else "NO_WRITE")
            send({"type": "error", "status": status, "job_dir": job_dir,
                  "error": str(exc), "error_type": type(exc).__name__,
                  "errno": getattr(exc, "errno", None),
                  "winerror": getattr(exc, "winerror", None),
                  "filename": os.fsdecode(exc.filename) if isinstance(exc, OSError) and exc.filename is not None else None,
                  "filename2": os.fsdecode(exc.filename2) if isinstance(exc, OSError) and exc.filename2 is not None else None,
                  **details, **mode_after_error})


if __name__ == "__main__":
    main()
