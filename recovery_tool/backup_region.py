"""Independent raw-region recapture using only an already-active reader."""

from datetime import datetime, timezone
import json
from pathlib import Path

from . import snapshot


def recapture(previous: Path, output: Path, *, region: str, same_camera_confirmed: bool,
              transport_factory, progress=None, independent_emmc: bool = False) -> dict:
    previous, output = (Path(path).expanduser().resolve() for path in (previous, output))
    if region not in snapshot.REGIONS:
        raise ValueError("unknown raw region")
    if independent_emmc and region != "nflasha":
        raise ValueError("independent recapture confirmation applies only to nflasha")
    if same_camera_confirmed is not True:
        return {"status": "NEEDS_USER_ACTION", "summary": "Confirm the same physical camera",
                "job_dir": str(output)}
    receipt = json.loads((previous / "capture-receipt.json").read_text())
    if receipt.get("status") != "INCOMPLETE" or receipt.get("capture_profile") is not None:
        raise ValueError("select an incomplete full-backup capture")
    manifest = previous / "verified.json"
    saved = json.loads(manifest.read_text()) if manifest.is_file() else {}
    mismatch = (receipt.get("failure") or {}).get("anchor_mismatch") or {}
    if region in saved:
        if region != "nflasha" or (mismatch.get("region") != "nflasha" and not independent_emmc):
            raise ValueError("previous backup already captured this region without documented drift")
        extent = saved[region]
        reads = receipt.get("reads", {}).get(region, {})
        if any(reads.get(attempt, {}).get(key) != extent.get(key)
               for attempt in ("first", "second") for key in ("bytes", "sha256")):
            raise ValueError("previous backup did not complete the region twice")
        anchors = snapshot.resume_anchors(previous, receipt, only_region=region)
        if not independent_emmc and not any(
                mismatch.get("offset") == row["offset"] and mismatch.get("bytes") == row["bytes"] and
                mismatch.get("saved_sha256") == row["sha256"] for row in anchors):
            raise ValueError("recorded drift does not match a saved double-read anchor")
    if output.is_relative_to(previous):
        raise ValueError("choose a new capture directory outside the previous snapshot")
    output.mkdir(mode=0o700)
    missing = sorted(set(snapshot.REGIONS) - {region})
    generation = {"status": "IN_PROGRESS", "same_camera_confirmed_by_operator": True,
                  "previous_snapshot": str(previous), "anchor_mismatch": mismatch,
                  "reused_regions": [], "atomic_camera_snapshot": False,
                  "requested_region": region, "missing_regions": missing, "complete_backup": False,
                  "independent_emmc_requested": independent_emmc,
                  "started_utc": datetime.now(timezone.utc).isoformat()}
    path = output / "recapture.json"
    snapshot.save(path, generation)
    result = {"job_dir": str(output), "region_capture": str(output), "previous_snapshot": str(previous),
              "missing_regions": missing, "complete_backup": False, "reused_regions": [],
              "atomic_camera_snapshot": False, "reboot_sent": False}
    try:
        with transport_factory() as transport:
            transport.verify_product()
        if progress:
            progress({"stage": "backup_recapture", "message": f"Reading the entire {region} region twice"})
        captured = snapshot.capture(output, transport_factory=transport_factory, only_region=region,
                                    require_all_regions=True, progress=progress)
    except Exception as exc:
        generation.update(status="INCOMPLETE", error=str(exc),
                          finished_utc=datetime.now(timezone.utc).isoformat())
        snapshot.save(path, generation)
        return {**result, "status": "INCOMPLETE", "summary": str(exc), "verified_regions": []}
    generation.update(status="PARTIAL", verified_regions={region: captured["reads"][region]},
                      capture_started_utc=captured["started_utc"], finished_utc=captured["finished_utc"])
    snapshot.save(path, generation)
    return {**result, "status": "PARTIAL", "summary": f"Complete {region} region verified by two reads",
            "verified_regions": [region]}
