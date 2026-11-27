#!/usr/bin/env python3
"""Offline JSONL fixture for exercising the native UI without devices."""

import json
import os
from pathlib import Path
import sys
import time


def main() -> None:
    request = json.loads(sys.stdin.readline())
    record = os.environ.get("CAMERA_RECOVERY_FAKE_LOG")
    if record:
        with Path(record).open("a", encoding="utf-8") as output:
            output.write(json.dumps(request, ensure_ascii=False) + "\n")
    action = request["action"]
    scenario = os.environ.get("CAMERA_RECOVERY_FAKE_SCENARIO", "normal_ready")
    kind = request.get("kind", "")
    job = request.get("job_dir") or request.get("output_dir")
    print(json.dumps({"type": "progress", "event": {"stage": "probe" if action == "smoke" else action,
                                                      "message": "Offline fixture"}}))
    if action == "smoke":
        result = {"status": "READY", "kind": "smoke", "summary": "Offline fixture"}
    elif action == "prepare":
        result = {"status": "READY", "kind": kind, "job_dir": job,
                  "summary": "Offline task prepared", "capability_verified": True}
    elif action == "reconcile" and scenario == "board_needs_boot":
        result = {"status": "NEEDS_USER_ACTION", "summary": "No verified RP2040 runtime or BOOTSEL",
                  "next_action": "Connect the RP2040 in BOOTSEL mode"}
    elif action == "reconcile":
        result = {"status": "READY", "kind": "board_install" if "board_install" in job else "full_backup",
                  "job_dir": job, "summary": "BOOTSEL found", "capability_verified": True}
    elif action == "apply":
        if scenario == "backup_stock_blocked" and "full_backup" in job:
            result = {"status": "INCOMPLETE", "kind": "full_backup", "job_dir": job,
                      "summary": "Automatic RAM reentry blocked by unresolved camera operation",
                      "blocking_tasks": [{"job_dir": "/tmp/fixture-prior-stock", "reason": "REBOOT_ACKNOWLEDGED"}],
                      "backup_retry_used": 2, "backup_retry_total": 2, "backup_retry_limit": 5}
        elif scenario == "backup_read_interrupted" and "full_backup" in job:
            print(json.dumps({"type": "progress", "event": {"stage": "backup_retry", "message": "Offline reconnect",
                           "retry": 5, "max_retries": 5, "total_retries": 7}}))
            result = {"status": "INCOMPLETE", "kind": "full_backup", "job_dir": job,
                      "summary": "Camera bulk read interrupted", "backup_failure_origin": "camera_usb_read",
                      "backup_retry_used": 5, "backup_retry_total": 7, "backup_retry_limit": 5}
        else:
            result = {"status": "VERIFIED", "kind": "board_install" if "board_install" in job else "full_backup",
                      "job_dir": job, "summary": "Offline apply verified", "control_ping_verified": True}
    elif action == "verify":
        result = {"status": "VERIFIED", "job_dir": job, "summary": "Offline verification complete"}
    elif action == "connection_probe":
        result = {"status": "NEEDS_USER_ACTION", "kind": "connection_probe",
                  "summary": "Independent control and DATA checks complete",
                  "control": {"status": "READY", "port": "FAKE", "message": "RP2040 PING verified"},
                  "data": {"status": "UNAVAILABLE", "message": "Camera RAM Fastboot unavailable"},
                  "next_action": "Enter RAM Fastboot through an explicit camera task"}
    elif action == "connection_enter_ram":
        result = {"status": "READY", "control": {"status": "READY", "port": "FAKE"},
                  "data": {"status": "READY", "product": "ILCE-7M4"},
                  "normal_mode_status": "REBOOT_UNCERTAIN" if scenario == "normal_uncertain" else "READY",
                  "current_mode": "UNKNOWN" if scenario == "normal_uncertain" else "MSC"}
    elif action == "prepare_card_connection":
        result = {"status": "READY", "summary": "Offline camera MSC candidate",
                  "volumes": [{"path": "/Volumes/FixtureCard", "device_identifier": "fixture-disk"}],
                  "camera_volume_verified": False, "mode": "MSC", "reboot_sent": True}
    elif action == "sd_card_prepare":
        if request.get("confirm_replace") or scenario == "sd_copy_verified":
            result = {"status": "VERIFIED", "summary": "Offline SD fixture verified",
                      "source_path": request["source_dat"], "source_sha256": "fake-source-sha256",
                      "source_bytes": 1024, "target_path": request["volume"] + "/BODYDATA.DAT",
                      "target_sha256": "fake-source-sha256", "target_bytes": 1024,
                      "volume_identity": {"device_identifier": "fixture-disk",
                                          "mount_point": request["volume"]},
                      "receipt_path": "/tmp/fixture-sd-copy-receipt.json",
                      "readback_verified": True, "readback_utc": "2026-11-16T00:00:00+00:00",
                      "copy_replaced": False, "dat_metadata": {"status": "not_parsed"},
                      "sd_slot_1_presence_verified": False}
        else:
            result = {"status": "NEEDS_CONFIRMATION", "requires_confirmation": True,
                      "existing_sha256": "fixture-existing-hash",
                      "volume_device_identifier": "fixture-disk",
                      "summary": "Offline SD fixture conflict"}
    elif action == "stock_plan":
        if scenario == "stock_plan_error":
            print(json.dumps({"type": "error", "status": "NO_WRITE", "error": "Offline plan read interrupted",
                              "phase": "plan", "error_code": "FastbootError",
                              "failure_cause": "Offline plan read interrupted",
                              "journal_valid": True, "write_may_have_occurred": False,
                              "reboot_may_have_occurred": False, "confirmed_fragments": 0,
                              "inflight": None, "failed_check": "settings_profile",
                              "next_action": "Read current camera state again"}), flush=True)
            return
        result = {"status": "READY", "summary": "Offline stock plan", "plan_digest": "fake-digest",
                  "requires_confirmation": True, "confirmation_recorded": False,
                  "stock_reboot_acknowledged": scenario == "stock_ack"}
    elif action == "stock_run":
        attempts = sum(json.loads(line).get("action") == "stock_run"
                       for line in Path(record).read_text().splitlines()) if record else 1
        if scenario == "stock_fresh_failure" and attempts == 1:
            result = {"status": "NO_WRITE", "summary": "Offline current stock plan stopped",
                      "phase": "settings", "error_code": "FastbootError",
                      "failure_cause": "Offline camera USB read interrupted",
                      "write_may_have_occurred": False, "reboot_may_have_occurred": False,
                      "confirmed_fragments": 0, "inflight": None,
                      "next_action": "Create a new stock task from fresh camera reads"}
        else:
            result = {"status": "NEEDS_USER_ACTION", "summary": "Offline stock reboot acknowledged",
                      "stock_reboot_acknowledged": True}
    elif action == "confirm_stock_update_complete":
        result = {"status": "VERIFIED", "summary": "Offline user confirmation recorded",
                  "camera_update_observed_by_user": True}
    elif action == "confirm_stock_update_failed":
        result = {"status": "FAILURE_RECORDED", "summary": "Offline observed failure recorded",
                  "camera_update_observed_by_user": False}
    elif action == "restore_run":
        if scenario == "restore_journal_unknown":
            result = {"status": "UNKNOWN_AFTER_WRITE", "summary": "Offline journal unreadable",
                      "phase": "transaction", "error_code": "safety_or_plan_check",
                      "failure_cause": "Saved transaction journal is unreadable",
                      "write_may_have_occurred": None, "confirmed_fragments": None,
                      "verified_regions": None, "inflight": None, "journal_valid": False}
        elif scenario == "restore_worker_error":
            print(json.dumps({"type": "error", "error": "Offline worker interrupted"}))
            return
        elif scenario == "restore_failure_then_fresh":
            attempts = sum(json.loads(line).get("action") == "restore_run"
                           for line in Path(record).read_text().splitlines()) if record else 1
            result = ({"status": "NO_WRITE", "summary": "Offline interruption before write",
                       "phase": "capture", "error_code": "camera_usb_read",
                       "failure_cause": "Offline USB read interrupted", "write_may_have_occurred": False,
                       "confirmed_fragments": 0, "verified_regions": [], "inflight": None,
                       "normal_mode_status": "NEEDS_USER_ACTION", "current_mode": "UNKNOWN"}
                      if attempts == 1 else
                      {"status": "VERIFIED", "summary": "Offline fresh recovery verified"})
        elif scenario == "restore_nor_unsupported":
            result = {"status": "VERIFIED", "nor_status": "UNSUPPORTED", "nor_executed": False,
                      "success_with_skips": True, "skipped_regions": [{"name": "cp-nor",
                      "reason": "native_rebuild_unsupported_profile"}],
                      "summary": "eMMC restored; native NOR profile unsupported"}
        elif scenario == "restore_nor_success" or scenario == "restore_nor_failed":
            for stage in ["prepare", "install", "wait", "collect", "cleanup", "normal"]:
                print(json.dumps({"type": "progress", "event": {
                    "stage": "restore_nor_" + stage, "message": "Offline native NOR " + stage,
                    "estimated_seconds": 180, "elapsed_seconds": 91 if stage == "wait" else 0,
                    "fraction": 1.0 if stage == "prepare" else None}}), flush=True)
                time.sleep(0.12)
                print(json.dumps({"type": "progress", "event": {"stage": "capture",
                    "message": "Offline low-level NOR capture", "fraction": 0.75}}), flush=True)
                time.sleep(0.05)
            success = scenario == "restore_nor_success"
            result = {"status": "VERIFIED" if success else "INCOMPLETE",
                      "nor_status": "VERIFIED" if success else "INCOMPLETE",
                      "nor_executed": True, "nor_array_verified": success,
                      "nor_companion_verified": success, "nor_cleanup_completed": success,
                      "summary": "Offline complete restore verified" if success else "Offline native NOR verify failed",
                      "planned_fragments": 2, "verified_fragments": 2, "failed_fragments": 0,
                      "normal_return_handled": True}
        elif scenario == "restore_progress":
            for event in [
                {"stage": "restore_read_session_exit", "message": "Read session reboot acknowledged; preparing local restore files"},
                {"stage": "restore_capture_idle_reentry", "message": "Guarded RAM entry during current-state assembly"},
                {"stage": "restore_local_verify", "phase": "progress", "file": "nflasha5.target",
                 "files": 22, "bytes_done": 64 * 1024 * 1024, "bytes_total": 128 * 1024 * 1024,
                 "message": "Verifying saved restore files: nflasha5.target"},
                {"stage": "restore_local_verify", "phase": "done", "file": "saved inputs",
                 "files": 22, "bytes_done": 128 * 1024 * 1024, "bytes_total": 128 * 1024 * 1024,
                 "message": "Saved restore files verified"},
                {"stage": "restore_idle_probe", "message": "Read-only camera liveness probe"},
                {"stage": "restore_idle_reentry", "message": "Guarded RAM entry after local verification"},
                {"stage": "restore_write_session_entry", "message": "Local restore files verified; entering fresh RAM write session"},
                {"stage": "bridge", "message": "Entering camera RAM Fastboot"},
                {"stage": "bridge_upload", "message": "Bridge acknowledged reset and every RAM payload record"},
                {"stage": "uart", "message": "UART ready marker absent; checking for late Fastboot USB"},
                {"stage": "bridge_diagnostic", "message": "Bridge UART bytes=0, marker=False, result=RamPayloadNotReady"},
                {"stage": "restore_reentry_retry", "retry": 2, "max_retries": 5,
                 "message": "UART ready marker absent; guarded bridge attempt 2/5"},
                {"stage": "restore_plan_summary", "planned_fragments": 2,
                 "planned_bytes": 1024, "message": "Planned 2 fragments; 1024 target bytes to write"},
                {"stage": "fragment_write_verified",
                 "message": "nflasha5 fragment 1 written and readback verified; 1/2 completed, 1 verified, 0 failed",
                 "fraction": 0.5, "completed_fragments": 1,
                 "planned_fragments": 2, "verified_fragments": 1,
                 "failed_fragments": 0},
                {"stage": "saved_fragment_evidence",
                 "message": "Saved local fragment readback evidence verified"},
            ]:
                print(json.dumps({"type": "progress", "event": event}), flush=True)
                time.sleep(0.1)
            result = {"status": "NO_WRITE", "summary": "Offline reconnect not ready"}
        elif scenario == "restore_skip_nor":
            result = {"status": "VERIFIED", "summary": "Offline restore completed with a skipped region",
                      "success_with_skips": True, "fully_matches_snapshot": False,
                      "restored_regions": ["nflasha2"],
                      "skipped_regions": [{"name": "cp-nor", "reason": "read_only_no_write_route",
                                            "changed": True}]}
        elif scenario == "restore_partial_fragments":
            result = {"status": "PARTIAL", "summary": "1 of 2 planned fragments failed verification",
                      "validation_mode": "fragment-readback-v1", "planned_fragments": 2,
                      "verified_fragments": 1, "failed_fragments": 1,
                      "success_with_skips": True, "fully_matches_snapshot": False,
                      "skipped_regions": [{"name": "cp-nor", "reason": "not_captured_read_only",
                                            "changed": None}]}
        elif scenario == "restore_no_writable_fragments":
            result = {"status": "PARTIAL", "summary": "No writable restore fragments",
                      "validation_mode": "fragment-readback-v1", "planned_fragments": 0,
                      "verified_fragments": 0, "failed_fragments": 0,
                      "success_with_skips": True, "fully_matches_snapshot": False,
                      "skipped_regions": [{"name": "nflasha", "reason": "raw_layout_unavailable",
                                            "changed": None}]}
        else:
            result = {"status": "VERIFIED", "summary": "Offline restore complete"}
    else:
        print(json.dumps({"type": "error", "error": "Unsupported fixture action"}))
        return
    print(json.dumps({"type": "result", "result": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
