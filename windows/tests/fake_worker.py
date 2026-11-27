"""A JSONL worker used only by the Electron UI smoke on non-Windows hosts."""
import json
import sys

request = json.loads(sys.stdin.readline())
action = request.get("action")
job = request.get("job_dir", "")
prior = request.get("prior_snapshot", "")
print(json.dumps({"type": "progress", "event": {"stage": action, "message": "fake worker"}}), flush=True)
mismatch = {"status": "INCOMPLETE", "summary": "Saved eMMC content changed",
            "backup_anchor_mismatch": {"region": "nflasha"}}
if action == "prepare":
    if request.get("kind") == "stock_sd_recovery":
        if request.get("inputs", {}).get("camera_model") != "ILCE-7RM5":
            raise ValueError("R5 smoke requires the selected R5 model in stock task inputs")
        result = {"status": "READY", "summary": "Fake R5 stock task prepared"}
    else:
        result = {"status": "READY", "summary": "Fake task prepared"}
elif action == "reconcile" and "fixture-board-needs-boot" in job:
    result = {"status": "NEEDS_USER_ACTION", "summary": "No verified RP2040 runtime or BOOTSEL",
              "next_action": "Connect the RP2040 in BOOTSEL mode"}
elif action == "reconcile" and "fixture-board-boot-ready" in job:
    result = {"status": "READY", "summary": "One verified RP2040 BOOTSEL volume",
              "next_action": "Confirm UF2 installation"}
elif action == "backup_resume_status":
    result = mismatch if "stored-drift" in job else {"status": "READY"}
elif action == "reconcile" and "fixture-backup" in job:
    result = {"status": "READY", "summary": "Fake backup reader is online"}
elif action == "apply" and "fixture-backup" in job:
    if "nor-read-failed" in job:
        result = {"status": "INCOMPLETE", "summary": "CP NOR read failed"}
    else:
        result = {"status": "VERIFIED"} if "same-anchor" in job or "missing-nor" in job or "changed-anchor" in job else mismatch
elif action == "verify" and "fixture-backup" in job:
    regions = ["nflasha", "nflashaB0", "nflashaB1"]
    if "missing-nor" not in job:
        regions.append("cp-nor")
    result = {"status": "VERIFIED", "raw_regions": len(regions), "verified_regions": regions}
elif action == "backup_recapture":
    if request.get("same_camera_confirmed") is not True:
        raise ValueError("expected explicit camera confirmation")
    if "\\snapshot" not in prior or request["output_dir"].startswith(prior):
        raise ValueError("new capture must be outside original snapshot")
    if "no-reader" in prior:
        result = {"status": "INCOMPLETE", "summary": "Fastboot USB device unavailable"}
    else:
        result = {"status": "PARTIAL", "verified_regions": [request["region"]],
                  "region_capture": request["output_dir"]}
elif action == "backup_archive":
    if request.get("same_camera_confirmed") is not True or not request.get("emmc_capture"):
        raise ValueError("archive requires confirmed same-camera eMMC recapture")
    result = ({"status": "INCOMPLETE", "missing_regions": ["cp-nor"]}
              if not request.get("supplements") else
              {"status": "VERIFIED", "complete_region_set": True, "restorable": False})
elif action == "backup_archive_verify":
    if "backup-archive-" not in request["archive_dir"]:
        raise ValueError("expected new archive directory")
    result = {"status": "VERIFIED", "complete_region_set": True, "restorable": False}
elif action == "verify_saved":
    result = {"status": "VERIFIED", "source_kind": ("archive" if request["source_dir"].endswith("archive")
                                                   else "snapshot"), "raw_regions": 4}
elif action == "sd_inspect":
    if not request["path"].endswith("BODYDATA.DAT"):
        raise ValueError("expected a DAT selection")
    result = {"status": "VERIFIED", "identity": "match", "sha256": "a" * 64,
              "package": {"model": request["model"], "version": "6.02"}}
elif action == "restore_run" and "fixture-restore-success" in job:
    print(json.dumps({"type": "progress", "event": {"stage": "restore_nor_wait",
         "message": "Fake NOR rebuild", "verified_fragments": 12, "planned_fragments": 12}}), flush=True)
    result = {"status": "VERIFIED", "emmc_status": "VERIFIED", "nor_status": "VERIFIED",
              "normal_mode_status": "READY", "planned_fragments": 12, "verified_fragments": 12,
              "failed_fragments": 0, "summary": "Fake storage and NOR verified"}
elif action == "reconcile":
    result = {"status": "ALREADY_INSTALLED", "summary": "Fake adapter checked", "control_ping_verified": True}
else:
    result = {"status": "NO_WRITE", "summary": "Fake worker has no device route"}
print(json.dumps({"type": "result", "result": result}), flush=True)
