"""Deliberate, journaled exit from camera RAM Fastboot for SD-card access."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import subprocess
import time

from .transport import (FastbootTransport, FastbootError, observe_normal_camera_usb,
                        PRODUCT, fastboot_products)
from .write_transaction import durable_json, digest, _verified_region_boundary


APP_ROOT = Path.home() / "Library/Application Support/Camera Recovery"
TASK_ROOT = APP_ROOT / "Tasks"
MODE_JOURNAL = APP_ROOT / "card-mode-transition.json"
TASK_REGISTRY = APP_ROOT / "task-registry.json"
VOLUME_ROOT = Path("/Volumes")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mounted_usb_volumes() -> list[dict]:
    """List writable USB volumes; user must identify the camera's volume."""
    found = []
    for volume in VOLUME_ROOT.iterdir():
        if volume.is_symlink() or not volume.is_dir():
            continue
        try:
            output = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(volume)],
                                    check=True, capture_output=True)
            info = plistlib.loads(output.stdout)
            if (info.get("MountPoint") == str(volume) and
                    info.get("BusProtocol") == "USB" and
                    info.get("WritableVolume") is True and
                    info.get("RemovableMediaOrExternalDevice") is True):
                found.append({"path": str(volume),
                              "device_identifier": info.get("DeviceIdentifier")})
        except (OSError, subprocess.CalledProcessError, ValueError, plistlib.InvalidFileException):
            continue
    return found


def native_entry_has_no_write(job_dir: Path) -> bool:
    """Prove the native entry transaction never issued a write or reboot."""
    folder = job_dir / "nor-rebuild/install-transaction"
    try:
        manifest = json.loads((folder / "manifest.json").read_text())
        state = json.loads((folder / "journal.json").read_text())
        return (manifest.get("schema") == "write-transaction-v1" and
                manifest.get("kind") == "nor_rebuild_entry" and
                state.get("schema") == "write-transaction-state-v1" and
                state.get("status") in {"READY", "NO_WRITE"} and
                state.get("write_may_have_occurred") is False and
                state.get("reboot_may_have_occurred") is False and
                state.get("inflight") is None and state.get("confirmed_fragments") == 0)
    except (OSError, ValueError, AttributeError):
        return False


def native_writer_blockers(*, task_root: Path | None = None, extra_jobs=()) -> list[dict]:
    """A lost worker must not turn an autonomous native write into a timed reset."""
    task_root = TASK_ROOT if task_root is None else Path(task_root)
    jobs = set(Path(path) for path in extra_jobs)
    if task_root.is_dir():
        jobs.update(task_root.iterdir())
    if TASK_REGISTRY.is_file():
        try:
            registered = json.loads(TASK_REGISTRY.read_text())
            if not isinstance(registered, dict) or not isinstance(registered.get("jobs"), list):
                raise ValueError("invalid task registry")
            for path in registered["jobs"]:
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise ValueError("invalid registered task path")
                jobs.add(Path(path))
        except (OSError, ValueError, TypeError):
            return [{"job_dir":str(TASK_REGISTRY), "reason":"unreadable native writer task registry"}]
    blocked = []
    for folder in jobs:
        receipt = folder / "nor-flow.json"
        if not receipt.is_file():
            continue
        try:
            state = json.loads(receipt.read_text())
            if not isinstance(state, dict) or type(state.get("writer_may_be_active")) is not bool:
                raise ValueError("missing native lifecycle state")
            if state["writer_may_be_active"]:
                if state.get("status") == "PREPARED" and native_entry_has_no_write(folder):
                    continue
                blocked.append({"job_dir":str(folder), "reason":"native NOR writer completion is unobserved"})
        except (OSError, ValueError, TypeError):
            blocked.append({"job_dir":str(folder), "reason":"unreadable native NOR lifecycle receipt"})
    return blocked


def pending_restore_outcomes(current: Path) -> list[dict]:
    """Find unresolved restore operations; verified fragment boundaries need no handoff."""
    current = Path(current).resolve()
    jobs = set(current.parent.glob("scoped_restore-*"))
    if TASK_ROOT.is_dir():
        jobs.update(TASK_ROOT.glob("scoped_restore-*"))
    if TASK_REGISTRY.is_file():
        raw = json.loads(TASK_REGISTRY.read_text())["jobs"]
        if not isinstance(raw, list) or not all(isinstance(path, str) for path in raw):
            raise ValueError("invalid restore task registry")
        jobs.update(Path(path) for path in raw)
    pending = []
    for job in sorted(jobs):
        if job.resolve() == current or not (job / "task.json").is_file():
            continue
        try:
            if json.loads((job / "task.json").read_text()).get("kind") != "scoped_restore":
                continue
            paths = {
                "scoped-stage/journal.json": lambda row: row.get("inflight") is not None or
                    row.get("reboot_may_have_occurred") is True or row.get("status") == "WRITE_INFLIGHT",
                "read-session-exit.json": lambda row: row.get("status") != "REBOOT_ACKNOWLEDGED",
                "post-restore-normal.json": lambda row: row.get("status") != "READY" and
                    row.get("reboot_may_have_occurred") is True,
            }
            for relative, unresolved in paths.items():
                journal = job / relative
                if journal.is_file() and unresolved(json.loads(journal.read_text())):
                    pending.append({"job_dir": str(job), "reason": relative})
        except (OSError, ValueError, AttributeError):
            pending.append({"job_dir": str(job), "reason": "unreadable restore receipt"})
    return pending

def _blocking_tasks(task_root: Path) -> list[dict]:
    blocked = []
    task_dirs = set(task_root.iterdir()) if task_root.is_dir() else set()
    registered_jobs = set()
    if TASK_REGISTRY.is_file():
        try:
            for item in json.loads(TASK_REGISTRY.read_text()).get("jobs", []):
                registered_jobs.add(Path(item))
                task_dirs.add(Path(item))
        except (OSError, ValueError, TypeError):
            return [{"job_dir": str(TASK_REGISTRY), "reason": "unreadable task registry"}]
    for task_dir in task_dirs:
        if not task_dir.is_dir() or not (task_dir / "task.json").is_file():
            continue
        try:
            kind = json.loads((task_dir / "task.json").read_text())["kind"]
        except (OSError, ValueError, KeyError):
            blocked.append({"job_dir": str(task_dir), "reason": "unreadable task index"})
            continue
        if kind == "scoped_restore" and (task_dir / "read-session-exit.json").exists():
            try:
                read_exit = json.loads((task_dir / "read-session-exit.json").read_text())
                if read_exit.get("status") != "REBOOT_ACKNOWLEDGED":
                    blocked.append({"job_dir": str(task_dir),
                                    "reason": "restore read-session reboot outcome unknown"})
            except (OSError, ValueError, TypeError):
                blocked.append({"job_dir": str(task_dir),
                                "reason": "unreadable restore read-session exit"})
        for relative in ("journal.json", "updater-stage/journal.json", "scoped-stage/journal.json"):
            journal = task_dir / relative
            if not journal.is_file():
                continue
            try:
                state = json.loads(journal.read_text())
            except (OSError, ValueError):
                blocked.append({"job_dir": str(task_dir), "reason": "unreadable transaction journal"})
                continue
            status = state.get("status")
            if state.get("reboot_may_have_occurred"):
                completed = task_dir / "camera-update-complete.json"
                failed = task_dir / "camera-update-failed.json"
                try:
                    outcome = (completed if completed.is_file() else failed)
                    confirmed = (kind == "stock_sd_recovery" and status == "REBOOT_ACKNOWLEDGED" and
                                 outcome.is_file() and
                                 json.loads(outcome.read_text()).get("journal_sha256") == digest(journal))
                except (OSError, ValueError):
                    confirmed = False
                if not confirmed:
                    blocked.append({"job_dir": str(task_dir), "reason": status or "reboot outcome unknown"})
            elif state.get("write_may_have_occurred") and status != "VERIFIED":
                if not (relative == "scoped-stage/journal.json" and
                        valid_restore_start_over_handoff(task_dir, registered_jobs)):
                    blocked.append({"job_dir": str(task_dir), "reason": status or "write outcome unknown"})
    return blocked


def valid_restore_start_over_handoff(old_job: Path, registered_jobs: set[Path]) -> bool:
    """A committed fresh-task handoff can retire only a fully read-back old boundary."""
    try:
        old_job = old_job.resolve(strict=True)
        receipt = json.loads((old_job / "restore-start-over.json").read_text())
        old_task_path = old_job / "task.json"
        old_task = json.loads(old_task_path.read_text())
        stage = old_job / "scoped-stage"
        manifest_path, journal_path = stage / "manifest.json", stage / "journal.json"
        read_exit_path = old_job / "read-session-exit.json"
        capture_path = old_job / "current-snapshot/capture-receipt.json"
        policy_path = old_job / "restore-session-policy.json"
        new_job = Path(receipt["new_job_dir"]).resolve(strict=True)
        new_task_path = new_job / "task.json"
        new_task = json.loads(new_task_path.read_text())
        if (receipt.get("schema") != "restore-start-over-v1" or
                receipt.get("status") != "COMMITTED" or
                receipt.get("old_job_dir") != str(old_job) or
                receipt.get("old_task_sha256") != digest(old_task_path) or
                receipt.get("new_task_sha256") != digest(new_task_path) or
                new_job not in {path.resolve() for path in registered_jobs} or
                old_task.get("kind") != "scoped_restore" or
                new_task.get("kind") != "scoped_restore" or
                new_task.get("supersedes_restore_job") != str(old_job) or
                new_task.get("target_snapshot_pins") != old_task.get("target_snapshot_pins") or
                new_task.get("inputs", {}).get("target_snapshot") !=
                old_task.get("inputs", {}).get("target_snapshot")):
            return False
        if read_exit_path.exists():
            if (receipt.get("old_read_exit_sha256") != digest(read_exit_path) or
                    json.loads(read_exit_path.read_text()).get("status") != "REBOOT_ACKNOWLEDGED"):
                return False
        elif receipt.get("old_read_exit_sha256") is not None:
            return False
        for path, field in ((capture_path, "old_capture_sha256"),
                            (policy_path, "old_policy_sha256")):
            if path.exists():
                if receipt.get(field) != digest(path):
                    return False
            elif receipt.get(field) is not None:
                return False
        saved_retry = 0
        for path, key in ((capture_path, "retry_consecutive_used"),
                          (policy_path, "retries_used")):
            if path.is_file():
                saved_retry = max(saved_retry, json.loads(path.read_text()).get(key, 0))
        if receipt.get("old_journal_sha256") is None:
            return (receipt.get("old_manifest_sha256") is None and
                    receipt.get("old_retry_used") == saved_retry and
                    not journal_path.exists() and not manifest_path.exists())
        if (receipt.get("old_manifest_sha256") != digest(manifest_path) or
                receipt.get("old_journal_sha256") != digest(journal_path)):
            return False
        manifest = json.loads(manifest_path.read_text())
        state = json.loads(journal_path.read_text())
        if receipt.get("old_retry_used") != state.get("restore_retry_used", 0):
            return False
        if (state.get("reboot_may_have_occurred") is not False or
                state.get("inflight") is not None):
            return False
        if state.get("write_may_have_occurred") is False:
            return state.get("confirmed_fragments") == 0
        if (state.get("write_may_have_occurred") is not True or
                not _verified_region_boundary(stage, manifest, state)):
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def register_task(job_dir: Path) -> None:
    """Record user-selected task locations for later mode safety checks."""
    job = str(Path(job_dir).expanduser().resolve())
    jobs = []
    if TASK_REGISTRY.is_file():
        jobs = json.loads(TASK_REGISTRY.read_text()).get("jobs", [])
    if job not in jobs:
        jobs.append(job)
        durable_json(TASK_REGISTRY, {"schema": "camera-task-registry-v1", "jobs": jobs})


def confirm_stock_update_complete(job_dir: Path, *, confirmed: bool) -> dict:
    """Record an explicit human observation; this does not contact the camera."""
    if confirmed is not True:
        raise ValueError("Camera update completion requires explicit confirmation")
    job_dir = Path(job_dir).expanduser().resolve(strict=True)
    task = json.loads((job_dir / "task.json").read_text())
    state_path = job_dir / "journal.json"
    state = json.loads(state_path.read_text())
    if (task.get("kind") != "stock_sd_recovery" or
            state.get("status") != "REBOOT_ACKNOWLEDGED" or
            state.get("reboot_may_have_occurred") is not True):
        raise ValueError("Stock update reboot has not been acknowledged for this task")
    if (job_dir / "camera-update-failed.json").exists():
        raise ValueError("This update was already confirmed failed")
    durable_json(job_dir / "camera-update-complete.json",
                 {"schema": "camera-update-complete-v1", "confirmed_utc": _now(),
                  "journal_sha256": digest(state_path)})
    return {"status": "VERIFIED", "summary": "User confirmed camera update completed",
            "next_action": "Camera mode changes can now be reviewed separately",
            "camera_update_observed_by_user": True}


def confirm_stock_update_failed(job_dir: Path, *, confirmed: bool, reason: str) -> dict:
    """Record a user-observed failed update; never infer failure from reboot ACK."""
    reasons = {"no_sd_card": "no SD card inserted",
               "blink_5_long_6_short": "Sony 5-long/6-short access-light error"}
    if confirmed is not True or reason not in reasons:
        raise ValueError("Failed update requires explicit supported observation confirmation")
    job_dir = Path(job_dir).expanduser().resolve(strict=True)
    task = json.loads((job_dir / "task.json").read_text())
    state_path = job_dir / "journal.json"
    state = json.loads(state_path.read_text())
    if (task.get("kind") != "stock_sd_recovery" or
            state.get("status") != "REBOOT_ACKNOWLEDGED" or
            state.get("reboot_may_have_occurred") is not True or
            not state.get("last_verified_utc")):
        raise ValueError("Verified stock write and reboot acknowledgement are required")
    if (job_dir / "camera-update-complete.json").exists():
        raise ValueError("This update was already confirmed complete")
    failed_path = job_dir / "camera-update-failed.json"
    journal_hash = digest(state_path)
    if failed_path.exists():
        previous = json.loads(failed_path.read_text())
        if (previous.get("schema") != "camera-update-failed-v1" or
                previous.get("reason") != reason or
                previous.get("journal_sha256") != journal_hash):
            raise ValueError("Saved failed-update observation differs from this request or journal")
    else:
        durable_json(failed_path,
                     {"schema": "camera-update-failed-v1", "confirmed_utc": _now(),
                      "reason": reason, "journal_sha256": journal_hash})
    return {"status": "FAILURE_RECORDED",
            "summary": f"User confirmed prior camera update failed: {reasons[reason]}",
            "next_action": "Prepare the SD card, then start a new recovery task",
            "camera_update_observed_by_user": False}


def return_normal(*, journal_path: Path, transport_factory=FastbootTransport,
                  normal_probe=observe_normal_camera_usb, task_root: Path = TASK_ROOT,
                  ignore_historical_tasks: bool = False,
                  once_only: bool = False, camera_model: str = PRODUCT,
                  progress=None, sleep=time.sleep, monotonic=time.monotonic,
                  timeout: float = 18.0) -> dict:
    """Exit RAM Fastboot at most once per intent and observe ordinary Sony USB."""
    accepted_products = fastboot_products(camera_model)
    if transport_factory is FastbootTransport:
        transport_factory = lambda: FastbootTransport(camera_model=camera_model)
    if normal_probe is observe_normal_camera_usb:
        normal_probe = lambda: observe_normal_camera_usb(camera_model=camera_model)
    journal_path = Path(journal_path)
    def emit(message):
        if progress:
            progress({"stage": "return_normal", "message": message})
    def normal():
        device = normal_probe()
        return device if device and device.get("status") in {"MSC", "NORMAL_USB"} else None
    emit("Checking ordinary Sony USB mode")
    found = normal()
    if found:
        if journal_path.is_file():
            state = json.loads(journal_path.read_text())
            if state.get("reboot_may_have_occurred") and state.get("status") != "READY":
                state.update(status="READY", observed_utc=_now())
                durable_json(journal_path, state)
        return {"normal_mode_status": "READY", "current_mode": found["status"],
                "reboot_sent": False}
    previous = json.loads(journal_path.read_text()) if journal_path.is_file() else {}
    if previous.get("reboot_may_have_occurred") and (previous.get("status") != "READY" or once_only):
        emit("Previous reboot outcome is uncertain; observing without another command")
        sent = False
    else:
        blocked = native_writer_blockers(task_root=task_root)
        if not ignore_historical_tasks:
            blocked += _blocking_tasks(task_root)
        if blocked:
            return {"normal_mode_status": "NO_WRITE", "current_mode": "FASTBOOT_OR_UNKNOWN",
                    "blocking_tasks": blocked, "reboot_sent": False}
        try:
            with transport_factory() as transport:
                product = transport.verify_product()
                if product not in accepted_products:
                    raise FastbootError(f"Fastboot product {product!r} differs from selected {camera_model}")
                intent = {"schema": "normal-mode-transition-v1", "status": "REBOOT_INFLIGHT",
                          "started_utc": _now(), "reboot_may_have_occurred": True,
                          "product": product, "camera_model": camera_model}
                durable_json(journal_path, intent)
                emit("Sending one Fastboot reboot to leave RAM mode")
                try:
                    transport._send("reboot")
                    transport._reply()
                except BaseException as exc:
                    intent.update(status="REBOOT_UNCERTAIN", error=str(exc))
                    durable_json(journal_path, intent)
                    return {"normal_mode_status": "REBOOT_UNCERTAIN", "current_mode": "UNKNOWN",
                            "reboot_sent": True}
                intent["status"] = "REBOOT_ACKNOWLEDGED"
                durable_json(journal_path, intent)
                sent = True
        except Exception:
            return {"normal_mode_status": "NEEDS_USER_ACTION", "current_mode": "UNKNOWN",
                    "reboot_sent": False}
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        found = normal()
        if found:
            state = json.loads(journal_path.read_text())
            state.update(status="READY", observed_utc=_now())
            durable_json(journal_path, state)
            return {"normal_mode_status": "READY", "current_mode": found["status"],
                    "reboot_sent": sent}
        sleep(0.3)
    return {"normal_mode_status": "REBOOT_UNCERTAIN", "current_mode": "UNKNOWN",
            "reboot_sent": sent}


def prepare_card_connection(*, route: str, transport_factory=FastbootTransport,
                            normal_probe=observe_normal_camera_usb,
                            volume_probe=mounted_usb_volumes,
                            task_root: Path = TASK_ROOT, journal_path: Path = MODE_JOURNAL,
                            progress=None, sleep=time.sleep, monotonic=time.monotonic,
                            timeout: float = 18.0, camera_model: str = PRODUCT) -> dict:
    if route != "camera":
        raise ValueError("prepare_card_connection supports the explicit camera route only")
    fastboot_products(camera_model)
    if normal_probe is observe_normal_camera_usb:
        normal_probe = lambda: observe_normal_camera_usb(camera_model=camera_model)
    emit = lambda stage, message: progress({"stage": stage, "message": message}) if progress else None

    def observed():
        sony = normal_probe()
        volumes = volume_probe()
        if sony and sony.get("status") == "MSC" and volumes:
            return {"status": "READY", "summary": "Sony camera MSC and writable USB volumes are present",
                    "next_action": "Select and verify the camera volume before copying BODYDATA.DAT",
                    "volumes": volumes, "camera_volume_verified": False,
                    "mode": "MSC", "reboot_sent": False}
        return sony, volumes

    emit("mode", "Checking Sony normal USB and mounted USB volumes")
    first = observed()
    if isinstance(first, dict):
        if journal_path.is_file():
            state = json.loads(journal_path.read_text())
            if state.get("reboot_may_have_occurred") and state.get("status") != "READY":
                state.update(status="READY", observed_utc=_now())
                durable_json(journal_path, state)
        return first
    sony, _ = first
    if sony and sony.get("status") == "NORMAL_USB":
        return {"status": "NEEDS_USER_ACTION", "summary": "Sony normal USB is not mass storage",
                "next_action": "Set camera USB mode to mass storage and retry; no reboot was sent",
                "volumes": [], "camera_volume_verified": False, "reboot_sent": False}

    exit_result = return_normal(journal_path=journal_path,
                                transport_factory=transport_factory,
                                normal_probe=normal_probe, task_root=task_root,
                                camera_model=camera_model,
                                progress=progress, sleep=sleep, monotonic=monotonic,
                                timeout=timeout)
    if exit_result["normal_mode_status"] == "NO_WRITE":
        return {"status": "NO_WRITE", "summary": "A camera write or updater reboot is unresolved",
                "next_action": "Resolve the recorded camera task before changing USB mode",
                "blocking_tasks": exit_result["blocking_tasks"], "reboot_sent": False}
    if exit_result["normal_mode_status"] == "REBOOT_UNCERTAIN":
        return {"status": "REBOOT_UNCERTAIN", "summary": "Fastboot reboot or normal USB outcome is uncertain",
                "next_action": "Observe Sony USB and camera screen; do not send reboot again",
                "reboot_sent": exit_result["reboot_sent"]}
    if exit_result["normal_mode_status"] != "READY":
        return {"status": "NEEDS_USER_ACTION", "summary": "Sony MSC and verified RAM Fastboot are unavailable",
                "next_action": "Connect the camera in mass-storage mode or use a direct SD reader",
                "reboot_sent": False}
    if exit_result["current_mode"] != "MSC":
        return {"status": "NEEDS_USER_ACTION", "summary": "Sony normal USB is not mass storage",
                "next_action": "Set camera USB mode to mass storage and retry",
                "reboot_sent": exit_result["reboot_sent"]}
    emit("mount", "Waiting for Sony MSC and a mounted writable USB volume")
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        current = observed()
        if isinstance(current, dict):
            if journal_path.is_file():
                state = json.loads(journal_path.read_text())
                if state.get("reboot_may_have_occurred"):
                    state["status"] = "READY"
                    state["observed_utc"] = _now()
                    durable_json(journal_path, state)
                    current["reboot_sent"] = exit_result["reboot_sent"]
            return current
        sleep(0.3)
    return {"status": "NEEDS_USER_ACTION", "summary": "Sony MSC is present but its card volume is not ready",
            "next_action": "Wait for the camera card volume or use a direct SD reader, then retry",
            "reboot_sent": exit_result["reboot_sent"]}
