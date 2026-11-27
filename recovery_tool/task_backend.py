"""GUI-facing task API. Importing this module never contacts hardware."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Callable
from uuid import uuid4

from . import board_install, snapshot, decrypt_snapshot, scoped_restore_plan
from .bridge import Bridge, RamPayloadNotReady, list_ports
from .stock_sd import (STOCK_DAT_SHA256, PROFILE as STOCK_PROFILE,
                       file_sha256, inspect_sd_environment)
from .stock_trigger import build as build_stock_trigger, _verify_sdm2 as verify_stock_sdm2
from .stock_profiles import CATALOG as STOCK_CATALOG, inspect_package, package_for_hash
from .stock_common import StockRecoveryError
from .transport import (FastbootTransport, FastbootError, FastbootEnumerationUnavailable,
                        USBReadError, observe_normal_camera_usb)
from .restore_session import IdleUSBSession, HostWorkCancelled
from .updater_restore import (build_plan as build_updater_plan, validate_settings,
                              SDM2_SECTOR0_SHA256, OEM_SHA256, CURRENT_SHA256,
                              SETTINGS_SHA256, SETTINGS_SIZE as BASELINE_SETTINGS_SIZE,
                              SIZE as UPDATER_SIZE)
from . import write_transaction as tx

BRIDGE_UART_TRACE_LIMIT = 64 * 1024


def _saved_restore_journal_state(stage: Path) -> dict | None:
    """Accept only a complete current-task journal before asserting write status."""
    path = stage / "journal.json"
    try:
        state = json.loads(path.read_text())
        if (not isinstance(state, dict) or
                state.get("schema") != "write-transaction-state-v1" or
                state.get("status") not in {"READY", "NO_WRITE", "WRITE_INFLIGHT",
                                            "UNKNOWN_AFTER_WRITE", "VERIFIED", "PARTIAL",
                                            "FRAGMENT_VERIFIED", "FRAGMENT_FAILED"} or
                type(state.get("write_may_have_occurred")) is not bool or
                type(state.get("reboot_may_have_occurred")) is not bool or
                type(state.get("confirmed_fragments")) is not int or
                state["confirmed_fragments"] < 0 or
                ("fragment_results" in state and not isinstance(state["fragment_results"], list)) or
                state.get("inflight") is not None and
                not isinstance(state.get("inflight"), dict)):
            return None
        return state
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _restore_failure_details(exc: Exception, phase: str, stage: Path) -> dict:
    """Report the current task's failure without translating old journals into live state."""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        chain.append({"type": type(current).__name__, "message": str(current),
                      "errno": getattr(current, "errno", None),
                      "backend_error_type": getattr(current, "backend_error_type", None),
                      "backend_error_code": getattr(current, "backend_error_code", None),
                      "usb_stage": getattr(current, "stage", None)})
        current = current.__cause__ or current.__context__
    if isinstance(exc, snapshot.CaptureFailure):
        code = exc.details.get("origin", "capture_failure")
    elif isinstance(exc, tx.CameraWriteIOError):
        code = "camera_usb_flash"
        phase = "flash"
    elif isinstance(exc, USBReadError):
        code = "camera_usb_read"
    elif isinstance(exc, FastbootError):
        code = "camera_transport"
    elif isinstance(exc, OSError):
        code = "local_io"
    elif isinstance(exc, (ValueError, RuntimeError)):
        code = "safety_or_plan_check"
    else:
        code = "unexpected_error"
    journal = stage / "journal.json"
    saved = _saved_restore_journal_state(stage) if journal.is_file() else None
    saved_valid = saved is not None
    state = saved or {}
    uncertain = (stage / "manifest.json").is_file() and not saved_valid
    pending = state.get("inflight") if isinstance(state.get("inflight"), dict) else None
    pending_valid = (pending is not None and isinstance(pending.get("region"), str) and
                     pending["region"] and type(pending.get("index")) is int and
                     pending["index"] >= 0 and type(pending.get("start_sector")) is int and
                     pending["start_sector"] >= 0 and type(pending.get("sectors")) is int and
                     pending["sectors"] > 0 and type(pending.get("attempts")) is int and
                     pending["attempts"] in (1, 2))
    pending_fragment = ({key: pending[key] for key in
                         ("region", "index", "start_sector", "sectors", "attempts")}
                        if pending_valid else None)
    original_error = state.get("error") if isinstance(state.get("error"), str) else None
    masked_flash_loss = (saved_valid and state.get("status") == "UNKNOWN_AFTER_WRITE" and
                         state.get("write_may_have_occurred") is True and
                         state.get("reboot_may_have_occurred") is False and
                         pending_fragment is not None and
                         str(exc) == "RAM session lost outside a safe reentry boundary" and
                         original_error is not None and
                         original_error.startswith("camera USB flash interrupted:"))
    if masked_flash_loss:
        phase = "flash"
        code = "camera_usb_flash"
    return {"phase": phase, "error_code": code,
            "failure_cause": original_error if masked_flash_loss else str(exc),
            "secondary_failure_cause": str(exc) if masked_flash_loss else None,
            "original_journal_error": original_error,
            "camera_error_detail": state.get("error_detail") if saved_valid else None,
            "pending_fragment": pending_fragment,
            "validation_mode": scoped_restore_plan.FRAGMENT_VALIDATION_MODE,
            "exception_chain": chain, "journal_path": str(journal) if journal.is_file() else None,
            "write_may_have_occurred": None if uncertain else state.get("write_may_have_occurred", False),
            "reboot_may_have_occurred": None if uncertain else state.get("reboot_may_have_occurred", False),
            "confirmed_fragments": None if uncertain else state.get("confirmed_fragments", 0),
            "verified_fragments": None if uncertain else state.get("confirmed_fragments", 0),
            "failed_fragments": None if uncertain else sum(
                isinstance(row, dict) and row.get("status") == "failed"
                for row in state.get("fragment_results", [])),
            "verified_regions": None if uncertain else state.get("verified_regions", []),
            "inflight": None if uncertain else state.get("inflight"),
            "journal_valid": saved_valid,
            "restore_retry_used": None if uncertain else state.get("restore_retry_used", 0)}


class _BridgeDiagnostic:
    """Private, bounded host trace of one explicit bridge RAM entry."""

    def __init__(self, port: str, payload: bytes, *, folder: Path | None = None):
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.port = port
        self.payload_sha256 = hashlib.sha256(payload).hexdigest()
        self.payload_bytes = len(payload)
        self.stages = []
        self.uart_hash = hashlib.sha256()
        self.uart_bytes = 0
        self.saved_bytes = 0
        self.error = None
        self.file = None
        self.receipt = None
        try:
            folder = folder or (Path.home() / "Library/Logs/Camera Recovery Logs/bridge-diagnostics")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)
            stem = f"bridge-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"
            raw = folder / f"{stem}.uart.bin"
            self.receipt = folder / f"{stem}.json"
            self.file = os.fdopen(os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
            self.raw = raw
        except OSError as exc:
            self.error = f"diagnostic file unavailable: {exc}"

    def stage(self, name: str, version: bytes):
        if name == "record_ack" and self.stages and self.stages[-1]["stage"] == name:
            self.stages[-1]["count"] += 1
            self.stages[-1]["last_utc"] = datetime.now(timezone.utc).isoformat()
            return
        self.stages.append({"stage": name, "first_utc": datetime.now(timezone.utc).isoformat(),
                            "last_utc": datetime.now(timezone.utc).isoformat(), "count": 1,
                            **({"version": version.decode("ascii", "replace")} if version else {})})

    def uart(self, data: bytes):
        self.uart_bytes += len(data)
        self.uart_hash.update(data)
        if self.file is not None and self.saved_bytes < BRIDGE_UART_TRACE_LIMIT:
            chunk = data[:BRIDGE_UART_TRACE_LIMIT - self.saved_bytes]
            try:
                self.file.write(chunk)
                self.saved_bytes += len(chunk)
            except OSError as exc:
                self.error = f"diagnostic UART write failed: {exc}"
                try:
                    self.file.close()
                except OSError:
                    pass
                self.file = None

    def finish(self, *, marker: bool | None, outcome: str) -> str:
        if self.file is not None:
            try:
                self.file.flush()
                os.fsync(self.file.fileno())
                self.file.close()
            except OSError as exc:
                self.error = f"diagnostic UART flush failed: {exc}"
        report = {"schema": "bridge-ram-entry-diagnostic-v1",
                  "started_utc": self.started_utc,
                  "finished_utc": datetime.now(timezone.utc).isoformat(),
                  "port": self.port, "payload_sha256": self.payload_sha256,
                  "payload_bytes": self.payload_bytes, "stages": self.stages,
                  "uart_marker_seen": marker, "outcome": outcome,
                  "uart_bytes": self.uart_bytes, "uart_saved_bytes": self.saved_bytes,
                  "uart_truncated": self.uart_bytes > self.saved_bytes,
                  "uart_sha256": self.uart_hash.hexdigest(),
                  "raw_uart_path": str(self.raw) if self.receipt else None,
                  "diagnostic_error": self.error}
        if self.receipt is not None:
            try:
                tx.durable_json(self.receipt, report)
            except OSError as exc:
                self.error = f"diagnostic receipt failed: {exc}"
        return str(self.receipt) if self.receipt and self.receipt.is_file() else self.error or "unavailable"

Progress = Callable[[dict], None]
KINDS = {"board_install", "stock_sd_recovery", "full_backup", "scoped_restore",
         "updater_restore"}
DEFAULT_PAYLOAD = (Path(__file__).resolve().parents[1] /
                   "ILCE-7M4-RE/payloads/fastboot/build/fastboot.bin")
CONNECTION_PAYLOAD_SHA256 = "7eb58b1e6957c848c54cfaeb42dd12f7bd42ca1db74e628d579b966519c0015a"
SD_READY_PLAN_DIGEST = hashlib.sha256(b"stock-sd-ready-user-confirmed-v1:ILCE-7M4:Sony-6.02").hexdigest()


def emit(progress: Progress | None, stage: str, message: str, *, fraction=None, status=None,
         **details):
    if progress is not None:
        event = {"stage": stage, "message": message}
        if fraction is not None:
            event["fraction"] = fraction
        if status is not None:
            event["status"] = status
        event.update(details)
        progress(event)


def _result(job_dir: Path, status: str, summary: str, next_action: str,
            *, confirmation=False, plan_digest=None, capability_verified=False,
            control_ping_verified=False) -> dict:
    task_file = Path(job_dir) / "task.json"
    kind = json.loads(task_file.read_text()).get("kind") if task_file.exists() else None
    result = {"kind": kind, "status": status, "job_dir": str(Path(job_dir).resolve()),
            "summary": summary, "next_action": next_action,
            "requires_confirmation": confirmation, "plan_digest": plan_digest,
            "capability_verified": capability_verified,
            "control_ping_verified": control_ping_verified}
    if kind == "stock_sd_recovery":
        pin = Path(job_dir) / "stock-composite-plan.json"
        task = json.loads(task_file.read_text())
        if pin.is_file() and tx.digest(pin) == task.get("composite_plan_digest"):
            plan = json.loads(pin.read_text())
            result.update({key: plan.get(key) for key in
                           ("updater_environment", "target_package_version", "host_dat_sha256", "prepared_card_dat_sha256")})
        elif task.get("target_package") is not None:
            result["target_package_version"] = task["target_package"]["version"]
            result["host_dat_sha256"] = (task["target_package"]["sha256"]
                                          if task["inputs"].get("dat") else None)
            if task.get("sd_preparation_evidence") is not None:
                result["prepared_card_dat_sha256"] = task["sd_preparation_evidence"]["source_sha256"]
    return result


def _stock_ack_time(journal: Path, state: dict) -> dict:
    """Report a host reboot ACK timestamp; legacy receipts use journal mtime."""
    if state.get("status") != "REBOOT_ACKNOWLEDGED":
        return {}
    timestamp = state.get("reboot_acknowledged_utc")
    if timestamp:
        return {"stock_reboot_acknowledged_utc": timestamp,
                "stock_reboot_timestamp_source": "journal_field"}
    return {"stock_reboot_acknowledged_utc": datetime.fromtimestamp(
                journal.stat().st_mtime, timezone.utc).isoformat(),
            "stock_reboot_timestamp_source": "journal_mtime"}


def _stock_bridge_allowed(job_dir: Path) -> bool:
    """A fresh stock task may enter RAM; its own uncertain write may not reset."""
    for folder in (job_dir / "updater-stage", job_dir):
        journal = folder / "journal.json"
        if not journal.is_file():
            if (folder / "manifest.json").is_file():
                return False
            continue
        try:
            state = json.loads(journal.read_text())
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(state, dict):
            return False
        if (state.get("schema") != "write-transaction-state-v1" or
                type(state.get("confirmed_fragments")) is not int or
                state["confirmed_fragments"] < 0):
            return False
        write_is_safe = (state.get("write_may_have_occurred") is False or
                         (state.get("write_may_have_occurred") is True and
                          state.get("status") == "VERIFIED"))
        if (not write_is_safe or state.get("reboot_may_have_occurred") is not False or
                state.get("inflight") is not None):
            return False
    return True


def _stock_failure_evidence(job_dir: Path) -> dict:
    """Report only write facts proved by this stock task's durable journals."""
    states = []
    for label, folder in (("updater", job_dir / "updater-stage"),
                          ("settings", job_dir)):
        journal = folder / "journal.json"
        if not journal.is_file():
            if (folder / "manifest.json").is_file():
                return {"journal_valid": False, "write_may_have_occurred": None,
                        "reboot_may_have_occurred": None, "confirmed_fragments": None,
                        "inflight": None, "failed_check": f"{label}_journal_missing"}
            continue
        try:
            state = json.loads(journal.read_text())
        except (OSError, ValueError, TypeError):
            state = None
        if (not isinstance(state, dict) or
                state.get("schema") != "write-transaction-state-v1" or
                type(state.get("write_may_have_occurred")) is not bool or
                type(state.get("reboot_may_have_occurred")) is not bool or
                type(state.get("confirmed_fragments")) is not int or
                state["confirmed_fragments"] < 0 or
                (state.get("inflight") is not None and
                 not isinstance(state.get("inflight"), dict))):
            return {"journal_valid": False, "write_may_have_occurred": None,
                    "reboot_may_have_occurred": None, "confirmed_fragments": None,
                    "inflight": None, "failed_check": f"{label}_journal_unreadable"}
        states.append((label, state))
    return {"journal_valid": True,
            "write_may_have_occurred": any(s["write_may_have_occurred"] for _, s in states),
            "reboot_may_have_occurred": any(s["reboot_may_have_occurred"] for _, s in states),
            "confirmed_fragments": sum(s["confirmed_fragments"] for _, s in states),
            "inflight": next(({"stage": label, **s["inflight"]} for label, s in states
                              if isinstance(s.get("inflight"), dict)), None),
            "failed_check": None}


def _backup_reentry_checkpoint(folder: Path) -> None:
    """Prove this backup has durable local identity evidence before any reset."""
    folder = Path(folder).resolve()
    receipt = json.loads((folder / "capture-receipt.json").read_text())
    if receipt.get("capture_profile") is not None:
        raise ValueError("full backup retry has an unexpected capture profile")
    snapshot.resume_anchors(folder, receipt)


def _task(job_dir: Path) -> dict:
    task = json.loads((Path(job_dir) / "task.json").read_text())
    if task.get("kind") not in KINDS:
        raise ValueError("unknown task kind")
    return task


def _check_manifest_pin(job_dir: Path, task: dict):
    manifest = Path(job_dir) / "manifest.json"
    diagnostic = Path(job_dir) / "diagnostic-plan.json"
    pinned = manifest if manifest.exists() else diagnostic if diagnostic.exists() else None
    # Stock candidate preparation writes its transaction before updating the
    # task index. A crash in that gap must be recoverable without rereading or
    # regenerating a different candidate. Rehash every transaction input first.
    if (pinned == manifest and task.get("kind") == "stock_sd_recovery" and
            task.get("plan_digest") in {*STOCK_CATALOG["packages"], SD_READY_PLAN_DIGEST}):
        reviewed, _ = tx.load(job_dir)
        if reviewed.get("kind") != "stock_sd_recovery" or not reviewed.get("reboot"):
            raise ValueError("unexpected stock transaction at index recovery")
        dat = task["inputs"].get("dat")
        if dat and not any(Path(pin["path"]) == _path(dat) and
                           pin["sha256"] == task["plan_digest"]
                           for pin in reviewed["input_files"]):
            raise ValueError("stock transaction does not pin the reviewed DAT")
        if not dat and task["inputs"].get("sd_ready") is not True:
            raise ValueError("stock transaction lacks SD slot 1 user confirmation")
        task["plan_digest"] = tx.digest(manifest)
        tx.durable_json(Path(job_dir) / "task.json", task)
    if pinned is not None and tx.digest(pinned) != task.get("plan_digest"):
        raise ValueError("task plan differs from prepared plan digest")


def _path(value) -> Path:
    return Path(value).expanduser().resolve()


def _restore_snapshot_folder(selected: Path) -> Path:
    """Accept a verified snapshot folder or its immediate parent task folder."""
    candidates = [folder for folder in (selected, selected / "snapshot")
                  if (folder / "verified.json").is_file()]
    if not candidates:
        raise ValueError("Choose a verified snapshot folder or its immediate parent backup task folder")
    if len(candidates) != 1:
        raise ValueError("Both selected folder and its snapshot child contain verified.json; choose the snapshot folder explicitly")
    return candidates[0]


def _inputs(values: dict) -> dict:
    result = {}
    for name, value in values.items():
        result[name] = str(_path(value)) if name not in ("port", "sd_ready", "restore_profile", "camera_model") and value else value
    return result


def _restore_verified_result(job_dir: Path, plan: dict, *, plan_digest: str,
                             state: dict | None = None) -> dict:
    skipped = plan.get("skipped_regions", [])
    rows = (state or {}).get("fragment_results", [])
    verified = [row for row in rows if row["status"] == "verified"]
    failed = [row for row in rows if row["status"] == "failed"]
    planned = sum(len(region["fragments"]) for region in plan["partitions"])
    status = (state or {}).get("status", "PARTIAL" if not planned and any(
        row.get("name") != "cp-nor" for row in skipped) else "VERIFIED")
    result = _result(job_dir, status,
                     f"{len(verified)}/{planned} planned fragments verified; {len(failed)} not restored",
                     "Review failed fragments and skipped regions" if failed or skipped else "Task complete",
                     plan_digest=plan_digest, capability_verified=True)
    result.update(validation_mode=scoped_restore_plan.FRAGMENT_VALIDATION_MODE,
                  completion_scope="planned_changed_fragments", planned_fragments=planned,
                  verified_fragments=len(verified), failed_fragments=len(failed),
                  failed_fragment_details=failed, fragment_results=rows,
                  skipped_regions=skipped, success_with_skips=bool(skipped),
                  fully_matches_snapshot=False,
                  restored_regions=[region["region"] for region in plan["partitions"]
                                    if sum(row["region"] == region["region"] for row in verified) == len(region["fragments"])])
    if status == "PARTIAL":
        result.update(reboot_sent=False, normal_mode_status="NOT_EXITED", current_mode="FASTBOOT_OR_UNKNOWN")
    return result


def _backup_verified_result(job_dir: Path, receipt: dict, decrypted: dict | None) -> dict:
    """Describe verified exposed bytes without claiming full physical coverage."""
    decrypted = decrypted or {}
    captured = receipt.get("capture_regions", {})
    if not captured and (job_dir / "snapshot/verified.json").is_file():
        captured = {name: row["bytes"] for name, row in
                    json.loads((job_dir / "snapshot/verified.json").read_text()).items()}
    count = len(decrypted.get("partitions", []))
    status = decrypted.get("decryption_status", "SUPPORTED" if count else "UNSUPPORTED")
    result = _result(job_dir, "VERIFIED" if captured else "INCOMPLETE",
                     f"{len(captured)} exposed raw regions verified; {count} decrypted slots; physical coverage is partial",
                     "Review captured and unavailable regions", capability_verified=bool(captured))
    result.update(captured_regions=captured, raw_regions=len(captured), decrypted_slots=count,
                  decryption_status=status, decryption_error=decrypted.get("decryption_error"),
                  region_coverage=receipt.get("region_coverage", {}), physical_device_complete=False,
                  coverage_note="Reader-exposed extents only; physical tail and RPMB are not captured")
    return result


def _verify_saved_scoped_readbacks(stage: Path, manifest: dict, state: dict,
                                   progress: Progress | None = None) -> None:
    """Validate the saved immediate fragment readbacks, never a whole-region hash."""
    emit(progress, "saved_fragment_evidence", "Checking saved local fragment readback evidence")
    tx.verify_fragment_results(stage, manifest, state)
    emit(progress, "saved_fragment_evidence", "Saved local fragment readback evidence verified")


def _stock_source(value, slot: int) -> Path | None:
    if not value:
        return None
    path = _path(value)
    if path.is_dir():
        path = path / f"decrypted/nflasha{slot:02d}.decrypted.bin"
    return path


def _validated_stock_sources(inputs: dict) -> tuple[Path | None, Path | None]:
    updater = _stock_source(inputs.get("oem_updater"), 1)
    settings = _stock_source(inputs.get("settings_baseline"), 2)
    if updater is not None and (updater.stat().st_size != UPDATER_SIZE or
                                tx.digest(updater) != OEM_SHA256):
        raise ValueError("chosen OEM updater differs from reviewed same-camera image")
    if settings is not None and (settings.stat().st_size != BASELINE_SETTINGS_SIZE or
                                 tx.digest(settings) != SETTINGS_SHA256):
        raise ValueError("chosen settings baseline differs from reviewed same-camera 6.04 image")
    return updater, settings


class Backend:
    """Stable four-action API for UI/worker callers."""

    def __init__(self, *, transport_factory=FastbootTransport, backup_fastboot_state=None):
        self.transport_factory = transport_factory
        self.backup_fastboot_state = backup_fastboot_state

    def _camera_transport(self, camera_model="ILCE-7M4"):
        if self.transport_factory is FastbootTransport and camera_model != "ILCE-7M4":
            return self.transport_factory(camera_model=camera_model)
        return self.transport_factory()

    def connection_probe(self, progress: Progress | None = None, *, camera_model="ILCE-7M4") -> dict:
        """Inspect control and DATA USB independently; never reset either device."""
        emit(progress, "control", "Checking RP2040 CDC or BOOTSEL")
        control = {"status": "MISSING", "ports": [], "port": None,
                   "ping": None, "bootsel_volumes": [], "message": "RP2040 board not found"}
        try:
            ports = list_ports()
            control["ports"] = ports
            volumes = board_install.bootsel_volumes()
            control["bootsel_volumes"] = [item["volume"] for item in volumes]
            if len(ports) > 1 or len(volumes) > 1 or (ports and volumes):
                control.update(status="AMBIGUOUS", message="Multiple RP2040 control candidates")
            elif len(volumes) == 1:
                control.update(status="BOOTSEL", message="RP2040 ROM BOOTSEL ready for board installation")
            elif len(ports) == 1:
                control["port"] = ports[0]
                try:
                    with Bridge(ports[0]) as bridge:
                        response = bridge.ping()
                    control["ping"] = response.decode("ascii", "replace")
                    if response in board_install.KNOWN_PINGS:
                        control.update(status="READY", message="RP2040 bridge PING verified")
                    else:
                        control.update(status="ERROR", message="RP2040 CDC returned an unknown PING")
                except Exception as exc:
                    control.update(status="ERROR", message=f"RP2040 CDC PING failed: {exc}")
        except Exception as exc:
            control.update(status="ERROR", message=f"RP2040 discovery failed: {exc}")

        emit(progress, "data", "Checking already-present camera RAM Fastboot")
        data = {"status": "UNAVAILABLE", "fastboot_usb_enumerated": False,
                "camera_usb_detected": False, "mode": None, "product": None,
                "message": "Camera RAM Fastboot USB not present"}
        try:
            with self._camera_transport(camera_model) as transport:
                product = transport.verify_product()
                with tempfile.TemporaryDirectory(prefix="camera-fastboot-check-") as temporary:
                    sample = Path(temporary) / "sector0.bin"
                    count = transport.read_region("nflasha", sample, length=512)
                    if count != 512 or sample.stat().st_size != 512:
                        raise FastbootError("Fastboot short read did not return 512 bytes")
                    sample_sha256 = hashlib.sha256(sample.read_bytes()).hexdigest()
            data.update(status="READY", fastboot_usb_enumerated=True,
                        camera_usb_detected=True, mode="FASTBOOT", product=product,
                        short_read_bytes=512, short_read_sha256=sample_sha256,
                        message="Fastboot product and 512-byte read verified")
        except FastbootError as exc:
            if "unavailable" in str(exc):
                try:
                    normal = (observe_normal_camera_usb(camera_model=camera_model)
                              if camera_model != "ILCE-7M4" else observe_normal_camera_usb())
                except Exception as detection_error:
                    data.update(status="ERROR", message=f"Normal camera USB discovery failed: {detection_error}")
                else:
                    if normal is not None:
                        data.update(status=normal["status"], mode=normal["status"],
                                    camera_usb_detected=True, product=normal["product"],
                                    normal_usb_vid=normal["vid"], normal_usb_pid=normal["pid"],
                                    interface_classes=normal["interface_classes"],
                                    message="ILCE-7M4 normal USB detected; RAM Fastboot is not active")
            else:
                data.update(status="ERROR", fastboot_usb_enumerated=None,
                            message=str(exc))
        except Exception as exc:
            data.update(status="ERROR", fastboot_usb_enumerated=None,
                        message=str(exc))

        if control["status"] == "BOOTSEL":
            next_action = "Open Board Install for the detected RP2040 BOOTSEL volume"
        elif control["status"] != "READY":
            next_action = "Connect and verify the RP2040 control board before camera tasks"
        elif data["status"] in {"MSC", "NORMAL_USB"}:
            next_action = "Camera normal USB is detected; enter RAM Fastboot through an explicit camera task to verify the recovery data path"
        elif data["status"] != "READY":
            next_action = "Camera RAM Fastboot is not verified; check the camera state and DATA connection"
        else:
            next_action = "Control and camera DATA connections are ready"
        return {"kind": "connection_probe", "status": "READY" if
                control["status"] == data["status"] == "READY" else "NEEDS_USER_ACTION",
                "control": control, "data": data, "summary": "Independent control and DATA checks complete",
                "next_action": next_action, "job_dir": None,
                "requires_confirmation": False, "plan_digest": None,
                "capability_verified": control["status"] == data["status"] == "READY"}

    def connection_enter_ram(self, *, confirmed: bool = False,
                             progress: Progress | None = None, camera_model="ILCE-7M4") -> dict:
        """Explicitly enter RAM Fastboot with a verified control bridge.

        This can reset the camera. A normal connection check never calls it.
        The camera may be boot-looping and have no ordinary USB enumeration.
        """
        if confirmed is not True:
            return {"kind": "connection_probe", "status": "NO_WRITE",
                    "summary": "RAM entry was not confirmed", "next_action": "Confirm the explicit camera mode change first",
                    "requires_confirmation": True, "capability_verified": False}
        before = self.connection_probe(progress, camera_model=camera_model)
        if before["data"]["status"] == "READY":
            return before
        if before["control"]["status"] != "READY" or before["data"]["status"] not in {"MSC", "NORMAL_USB", "UNAVAILABLE"}:
            return before
        if not DEFAULT_PAYLOAD.is_file() or tx.digest(DEFAULT_PAYLOAD) != CONNECTION_PAYLOAD_SHA256:
            return {**before, "status": "NO_WRITE", "capability_verified": False,
                    "summary": "Pinned RAM payload is missing or changed",
                    "next_action": "Use an intact local application package before trying again"}
        try:
            with self._transport({"inputs": {"camera_model": camera_model}, "payload_sha256": CONNECTION_PAYLOAD_SHA256},
                                 progress):
                pass
        except Exception as exc:
            detail = str(exc)
            if "did not report fastboot ready" in detail:
                stage = "UART_NOT_READY"
                next_action = "Check camera power and MULTI connection before a new explicit attempt"
            elif "did not enumerate after bridge load" in detail:
                stage = "FASTBOOT_USB_MISSING"
                next_action = "Bridge reported fastboot ready; check the DATA USB connection, then run a read-only check"
            else:
                stage = "RAM_ENTRY_INCOMPLETE"
                next_action = "Check camera power and connections, then run a fresh read-only connection check"
            return {**before, "status": "INCOMPLETE", "capability_verified": False,
                    "ram_entry_stage": stage,
                    "summary": f"RAM Fastboot entry did not complete: {detail}",
                    "next_action": next_action}
        return self.connection_probe(progress, camera_model=camera_model)

    def _transport(self, task: dict, progress: Progress | None, *, force_bridge: bool = False,
                   allow_bridge: bool = True):
        camera_model = task["inputs"].get("camera_model", "ILCE-7M4")
        from .card_connection import native_writer_blockers
        blocked = native_writer_blockers()
        if blocked:
            raise RuntimeError("native NOR writer may still be active; no RAM entry or reset: " + str(blocked))
        if not force_bridge:
            try:
                return self._camera_transport(camera_model)
            except FastbootError as exc:
                if not tx._fragment_transport_unavailable(exc):
                    raise
                if not allow_bridge:
                    raise FastbootError("current stock task has unresolved write or reboot; "
                                        "direct Fastboot readback is required before bridge reset") from exc
        if not allow_bridge:
            raise FastbootError("current stock task does not permit a bridge reset")
        inputs = task["inputs"]
        port = inputs.get("port")
        if not port:
            ports = list_ports()
            if len(ports) != 1:
                raise RuntimeError("one RP2040 bridge port is required to enter RAM Fastboot")
            port = ports[0]
        payload = _path(inputs.get("payload") or DEFAULT_PAYLOAD)
        expected = task.get("payload_sha256")
        if expected is not None and tx.digest(payload) != expected:
            raise RuntimeError("pinned RAM payload changed")
        emit(progress, "bridge", "Entering camera RAM Fastboot")
        trace = _BridgeDiagnostic(port, payload.read_bytes())
        uart_ready = None
        outcome = "bridge_incomplete"
        try:
            with Bridge(port) as bridge:
                bridge.boot_payload(payload.read_bytes(), on_stage=trace.stage, on_uart=trace.uart)
                emit(progress, "bridge_upload", "Bridge acknowledged reset and every RAM payload record")
                uart_ready = bridge.wait_uart(b"fastboot ready", 25, on_uart=trace.uart)
            if not uart_ready:
                emit(progress, "uart", "UART ready marker absent; checking for late Fastboot USB")
                late_deadline = time.monotonic() + 2
                while time.monotonic() < late_deadline:
                    try:
                        found = self._camera_transport(camera_model)
                        outcome = "late_fastboot_verified"
                        return found
                    except FastbootError as exc:
                        if not tx._fragment_transport_unavailable(exc):
                            raise
                        time.sleep(0.25)
                raise RamPayloadNotReady("RAM payload did not report fastboot ready")
            emit(progress, "uart", "Camera reported fastboot ready; waiting for DATA USB")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    found = self._camera_transport(camera_model)
                    outcome = "fastboot_verified"
                    return found
                except FastbootError as exc:
                    if not tx._fragment_transport_unavailable(exc):
                        raise
                    time.sleep(0.25)
            raise FastbootEnumerationUnavailable("RAM Fastboot did not enumerate after bridge load")
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            receipt = trace.finish(marker=uart_ready, outcome=outcome)
            emit(progress, "bridge_diagnostic",
                 f"Bridge UART bytes={trace.uart_bytes}, marker={uart_ready}, result={outcome}; receipt={receipt}")

    def _factory(self, task: dict, progress: Progress | None):
        return lambda: self._transport(task, progress)

    def _stock_transport(self, job_dir: Path, task: dict, progress: Progress | None):
        return self._transport(task, progress,
                               allow_bridge=_stock_bridge_allowed(job_dir))

    def _stock_factory(self, job_dir: Path, task: dict, progress: Progress | None):
        return lambda: self._stock_transport(job_dir, task, progress)

    def prepare(self, kind: str, inputs: dict, output_dir: Path,
                progress: Progress | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unsupported task kind: {kind}")
        output_dir = _path(output_dir)
        if output_dir.exists():
            raise FileExistsError(f"new task directory required: {output_dir}")
        inputs = _inputs(inputs)
        if kind == "scoped_restore":
            inputs["target_snapshot"] = str(_restore_snapshot_folder(_path(inputs["target_snapshot"])))
            if inputs.get("restore_profile") not in (None, "capability"):
                raise ValueError("Unknown restore capability profile")
            inputs["restore_profile"] = "capability"
        emit(progress, "prepare", f"Checking {kind} inputs")
        if kind == "board_install":
            uf2 = _path(inputs.get("uf2") or board_install.default_uf2())
            review = board_install.validate_uf2(uf2)
            capability = True
            plan_digest = review["sha256"]
        elif kind == "stock_sd_recovery":
            camera_model = inputs.get("camera_model", "ILCE-7M4")
            if camera_model not in ("ILCE-7M4", "ILCE-7RM5"):
                raise ValueError(f"SD adapter recovery is unsupported for {camera_model}")
            if inputs.get("sd_ready") is not True and not inputs.get("dat"):
                raise ValueError("Confirm official Sony BODYDATA.DAT is in camera SD slot 1")
            package = ((inspect_package(_path(inputs["dat"]), camera_model=camera_model)
                        if camera_model == "ILCE-7RM5" else inspect_package(_path(inputs["dat"])))
                       if inputs.get("dat") else None)
            sd_receipt = None
            if inputs.get("sd_preparation_receipt"):
                receipt_path = _path(inputs["sd_preparation_receipt"])
                sd_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                source_sha = sd_receipt.get("source_sha256")
                source_bytes = sd_receipt.get("source_bytes")
                volume_id = sd_receipt.get("volume_identity")
                if (sd_receipt.get("schema") != "sd-card-prepare-v1" or
                        sd_receipt.get("status") not in ("VERIFIED", "ALREADY_PRESENT") or
                        sd_receipt.get("readback_verified") is not True or
                        not isinstance(source_sha, str) or len(source_sha) != 64 or
                        any(c not in "0123456789abcdef" for c in source_sha) or
                        sd_receipt.get("target_sha256") != source_sha or
                        type(source_bytes) is not int or source_bytes <= 0 or
                        sd_receipt.get("target_bytes") != source_bytes or
                        not isinstance(volume_id, dict) or
                        not isinstance(volume_id.get("device_identifier"), str) or
                        not volume_id["device_identifier"] or
                        not isinstance(volume_id.get("mount_point"), str) or
                        not volume_id["mount_point"]):
                    raise ValueError("Selected SD preparation receipt is not a verified byte match")
                prepared_package = package_for_hash(source_sha)
                if prepared_package is not None and prepared_package["bytes"] == source_bytes:
                    prepared_package = {"sha256": source_sha, **prepared_package}
                    if package is not None and package["sha256"] != source_sha:
                        raise ValueError("Selected DAT differs from the prepared card receipt")
                    package = package or prepared_package
            _validated_stock_sources(inputs)
            plan_digest = package["sha256"] if inputs.get("dat") else SD_READY_PLAN_DIGEST
            capability = True
        elif kind == "full_backup":
            plan_digest = None
            capability = True
        elif kind == "scoped_restore":
            target = _path(inputs["target_snapshot"])
            if not inputs.get("current_snapshot"):
                decrypt_snapshot.verify_snapshot(target, progress=progress)
                target_pins = {name: tx.digest(target / name) for name in
                               ("verified.json", "capture-receipt.json",
                                "decrypted/manifest.json")}
                plan_digest = hashlib.sha256(json.dumps(target_pins, sort_keys=True).encode()).hexdigest()
                capability = True
            else:
                current = _path(inputs["current_snapshot"])
                profile = inputs.get("restore_profile") or "capability"
                plan = scoped_restore_plan.build_plan(current, target, profile=profile,
                         validation_mode=scoped_restore_plan.FRAGMENT_VALIDATION_MODE)
                identity = scoped_restore_plan.capability_identity(current, target, plan)
                reviewed = tx.digest(current / "capture-receipt.json") == plan["current_capture_receipt_sha256"]
                if not reviewed:
                    raise ValueError("snapshot capture receipt changed during prepare")
                regions = [{"name": item["region"], "original": item["current_image"],
                            "target": item["target_image"]} for item in plan["partitions"]]
                pin_files = [current / "verified.json", target / "verified.json",
                         current / "capture-receipt.json", target / "capture-receipt.json",
                         current / "decrypted/manifest.json", target / "decrypted/manifest.json"]
                tx.prepare(output_dir, kind=kind, regions=regions, identity=identity,
                       input_files=pin_files, metadata={"offline_plan": plan,
                                                         "validation_mode": scoped_restore_plan.FRAGMENT_VALIDATION_MODE,
                                                         "capability_profile":
                                                         "verified-capability-snapshot-v1",
                                                         "current_snapshot": str(current),
                                                         "target_snapshot": str(target)})
                plan_digest = tx.digest(output_dir / "manifest.json")
                capability = True
        else:  # updater_restore is available to the backend, hidden from the four-task UI.
            current, target = _path(inputs["current"]), _path(inputs["source"])
            settings = _path(inputs["settings_baseline"])
            build_updater_plan(current.read_bytes(), target.read_bytes())
            tx.prepare(output_dir, kind=kind,
                       regions=[{"name": "nflasha1", "original": current, "target": target}],
                       identity=[{"region": "nflasha", "bytes": 512,
                                  "sha256": SDM2_SECTOR0_SHA256}],
                       input_files=[settings], metadata={"settings_baseline": str(settings)})
            plan_digest = tx.digest(output_dir / "manifest.json")
            capability = True
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_dir.chmod(0o700)
        payload = _path(inputs.get("payload") or DEFAULT_PAYLOAD)
        task = {"schema": "camera-task-v1", "kind": kind, "inputs": inputs,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "payload_sha256": tx.digest(payload) if payload.is_file() else None,
                "plan_digest": plan_digest, "capability_verified": capability}
        if kind == "scoped_restore":
            task["validation_mode"] = scoped_restore_plan.FRAGMENT_VALIDATION_MODE
        if kind == "stock_sd_recovery":
            task["target_package"] = package
        if kind == "stock_sd_recovery" and sd_receipt is not None:
            task["sd_preparation_evidence"] = {
                "receipt_path": inputs["sd_preparation_receipt"],
                "receipt_sha256": tx.digest(_path(inputs["sd_preparation_receipt"])),
                "source_sha256": sd_receipt["source_sha256"],
                "target_sha256": sd_receipt["target_sha256"],
                "volume_identity": sd_receipt.get("volume_identity"),
                "sd_slot_1_presence_verified": False}
        if kind == "scoped_restore" and not inputs.get("current_snapshot"):
            task["target_snapshot_pins"] = target_pins
            task["session_policy"] = "keep-ram-idle-observe-v1"
        tx.durable_json(output_dir / "task.json", task)
        status = "READY" if capability else "NEEDS_USER_ACTION"
        emit(progress, "prepare", "Task prepared", status=status)
        return _result(output_dir, status, f"{kind} prepared",
                       "Review and reconcile" if capability else
                       "Independent capability review required before any write",
                       confirmation=capability, plan_digest=plan_digest,
                       capability_verified=capability)

    def _updater_validator(self, job_dir: Path):
        task = _task(job_dir)
        if task["kind"] != "updater_restore":
            return None
        baseline = _path(task["inputs"]["settings_baseline"]).read_bytes()

        def check(transport, epoch_dir):
            path = epoch_dir / "settings.identity.bin"
            transport.read_region("nflasha2", path)
            validate_settings(baseline, path.read_bytes())

        return check

    @staticmethod
    def _stock_model_options(task):
        model = task["inputs"].get("camera_model", "ILCE-7M4")
        return {"camera_model": model} if model == "ILCE-7RM5" else {}

    def _stock_transaction(self, job_dir: Path, task: dict, transport,
                           *, updater_target: Path | None = None):
        """Prepare the exact MODE5 target from fresh read-only camera images."""
        if (job_dir / "manifest.json").exists():
            return
        root = job_dir / "stock-inputs"
        root.mkdir(mode=0o700, exist_ok=True)
        attempt = 1
        inputs_dir = root / f"attempt-{attempt:03d}"
        while inputs_dir.exists():
            attempt += 1
            inputs_dir = root / f"attempt-{attempt:03d}"
        inputs_dir.mkdir(mode=0o700)
        sector0 = inputs_dir / "sector0.bin"
        transport.read_region("nflasha", sector0, length=512)
        verify_stock_sdm2(sector0.read_bytes(), **self._stock_model_options(task))
        updater = inputs_dir / "updater.bin"
        settings = inputs_dir / "settings.bin"
        transport.read_region("nflasha1", updater)
        transport.read_region("nflasha2", settings)
        stage_manifest = job_dir / "updater-stage/manifest.json"
        if stage_manifest.exists():
            expected_updater = json.loads(stage_manifest.read_text())["regions"][0]["target_sha256"]
        else:
            expected_updater = tx.digest(updater)
        trigger_updater = updater_target or updater
        fragment, candidate, report = build_stock_trigger(settings.read_bytes(),
                                                           trigger_updater.read_bytes(),
                                                           _path(task["inputs"]["dat"]) if task["inputs"].get("dat") else None,
                                                           expected_updater_sha256=expected_updater,
                                                           **self._stock_model_options(task))
        receipt = task.get("sd_preparation_evidence")
        receipt_inputs = []
        if receipt is not None:
            receipt_path = _path(receipt["receipt_path"])
            if tx.digest(receipt_path) != receipt["receipt_sha256"]:
                raise ValueError("Prepared SD card receipt changed")
            receipt_inputs.append(receipt_path)
            report["prepared_card_dat_sha256"] = receipt["source_sha256"]
            package = task.get("target_package")
            if package is not None:
                report["target_package_version"] = package["version"]
        target = inputs_dir / "settings.target.bin"
        target.write_bytes(candidate)
        target.chmod(0o600)
        tx.durable_json(inputs_dir / "stock-plan.json", report)
        tx.prepare(job_dir, kind="stock_sd_recovery",
                   regions=[{"name": "nflasha2", "original": settings, "target": target}],
                   identity=[{"region": "nflasha", "bytes": 512,
                              "sha256": tx.digest(sector0)},
                             {"region": "nflasha1", "bytes": UPDATER_SIZE,
                              "sha256": expected_updater}],
                   input_files=([_path(task["inputs"]["dat"])] if task["inputs"].get("dat") else []) +
                               receipt_inputs + [inputs_dir / "stock-plan.json"],
                   reboot=True, metadata={"candidate_sha256": report["candidate_sha256"],
                                          "expected_sectors": report["sparse_sectors"],
                                          "preexisting_trigger_verified": not report["sparse_sectors"],
                                          "sd_slot_1_user_confirmed": task["inputs"].get("sd_ready") is True,
                                          "sd_slot_1_file_verified_by_host": False})

    def _stock_updater_validator(self, task: dict, stage: Path):
        manifest = json.loads((stage / "manifest.json").read_text())
        profile = manifest["metadata"].get("settings_profile")
        if profile in ("captured-live-6.02-or-6.04", "captured-live-ILCE-7RM5"):
            baseline = _path(manifest["metadata"]["settings_baseline"])
            expected_versions = None if profile == "captured-live-ILCE-7RM5" else ("6.02", "6.04")
        else:
            _, baseline = _validated_stock_sources(task["inputs"])
            expected_versions = ("6.04",)
        if baseline is None:
            raise ValueError("pinned settings baseline is required for Erlla recovery")
        baseline_bytes = baseline.read_bytes()
        baseline_sha = tx.digest(baseline)

        def check(transport, epoch_dir):
            path = epoch_dir / "settings.identity.bin"
            transport.read_region("nflasha2", path)
            validate_settings(baseline_bytes, path.read_bytes(),
                              expected_sha=baseline_sha,
                              expected_versions=expected_versions)

        return check

    def _stock_updater_stage(self, job_dir: Path, task: dict, transport,
                             progress: Progress | None) -> dict | None:
        """Return pending updater stage, or None once OEM updater is verified."""
        stage = job_dir / "updater-stage"
        if (stage / "manifest.json").exists():
            plan = tx.reconcile(stage, transport=transport,
                                validator=self._stock_updater_validator(task, stage),
                                progress=progress)
            if plan["status"] == "VERIFIED":
                return None
            manifest = json.loads((stage / "manifest.json").read_text())
            sectors = sum(end - start for start, end in manifest["regions"][0]["scope"])
            target_hash = manifest["regions"][0]["target_sha256"]
            return _result(job_dir, plan["status"],
                           f"Erlla→OEM updater: target SHA-256 {target_hash}; "
                           f"{sectors} changed sectors; no reboot in this stage",
                           "Apply pending updater sectors before stock trigger",
                           confirmation=plan["status"] == "READY",
                           plan_digest=tx.digest(stage / "manifest.json"),
                           capability_verified=True)
        if stage.exists():
            if (stage / "journal.json").exists():
                raise ValueError("updater stage journal exists without manifest")
            archive = job_dir / "updater-stage.incomplete-001"
            number = 1
            while archive.exists():
                number += 1
                archive = job_dir / f"updater-stage.incomplete-{number:03d}"
            stage.rename(archive)  # No manifest means no transaction could have applied.
        probe = job_dir / "updater-probe"
        probe.mkdir(mode=0o700, exist_ok=True)
        attempt = 1
        while (probe / f"attempt-{attempt:03d}").exists():
            attempt += 1
        attempt_dir = probe / f"attempt-{attempt:03d}"
        attempt_dir.mkdir(mode=0o700)
        sector0, updater = attempt_dir / "sector0.bin", attempt_dir / "updater.bin"
        transport.read_region("nflasha", sector0, length=512)
        verify_stock_sdm2(sector0.read_bytes(), **self._stock_model_options(task))
        transport.read_region("nflasha1", updater)
        if updater.stat().st_size != UPDATER_SIZE:
            return _result(job_dir, "NO_WRITE",
                           f"nflasha1 size mismatch: expected={UPDATER_SIZE} actual={updater.stat().st_size}",
                           "Review the updater partition size before another SD recovery attempt",
                           plan_digest=task["plan_digest"])
        live_hash = tx.digest(updater)
        try:
            environment = inspect_sd_environment(updater.read_bytes(), STOCK_PROFILE, **self._stock_model_options(task))
        except StockRecoveryError:
            environment = None
        if environment is not None:
            emit(progress, "stock", "Complete official Sony stock boot profile already present; updater unchanged",
                 updater_environment=environment)
            return None
        source, baseline = _validated_stock_sources(task["inputs"])
        if (source is None) != (baseline is None):
            return _result(job_dir, "NEEDS_USER_ACTION",
                           "OEM source and settings baseline must be selected together",
                           "Both archived sources are required for this route; keep the read-only evidence and stop",
                           plan_digest=task["plan_digest"])
        if source is not None:
            if live_hash != CURRENT_SHA256:
                return _result(job_dir, "NO_WRITE", "Live updater differs from reviewed Erlla image",
                               "Live updater is outside the reviewed recovery profile; keep the read-only evidence and stop",
                               plan_digest=task["plan_digest"])
            build_updater_plan(updater.read_bytes(), source.read_bytes())
            target = source
            profile = "reviewed-same-camera-6.04"
            proof = {"source": "user-selected exact reviewed same-camera image"}
        else:
            from .stock_updater_source import reconstruct
            try:
                target_bytes, proof = reconstruct(updater.read_bytes(),
                                                  _path(task["inputs"]["dat"]) if task["inputs"].get("dat") else None,
                                                  **self._stock_model_options(task))
            except Exception as exc:
                return _result(job_dir, "NO_WRITE",
                               f"Live updater cannot be reconstructed under reviewed profile: {exc}",
                               "Live updater is outside the reviewed recovery profile; keep the read-only evidence and stop",
                               plan_digest=task["plan_digest"])
            try:
                if len(target_bytes) != UPDATER_SIZE:
                    raise ValueError(f"nflasha1 size mismatch: expected={UPDATER_SIZE} actual={len(target_bytes)}")
                inspect_sd_environment(target_bytes, STOCK_PROFILE, **self._stock_model_options(task))
            except Exception as exc:
                return _result(job_dir, "NO_WRITE",
                               f"Reconstructed updater failed stock profile verification: {exc}",
                               "Reconstructed updater failed stock verification; keep the read-only evidence and stop",
                               plan_digest=task["plan_digest"])
            if target_bytes == updater.read_bytes():
                emit(progress, "stock", "Complete official Sony stock boot profile already present; updater unchanged")
                return None
            target = attempt_dir / "updater.target.bin"
            target.write_bytes(target_bytes)
            target.chmod(0o600)
            profile = ("captured-live-ILCE-7RM5" if self._stock_model_options(task)
                       else "captured-live-6.02-or-6.04")
        settings_path = attempt_dir / "settings.bin"
        transport.read_region("nflasha2", settings_path)
        settings_bytes = settings_path.read_bytes()
        if profile in ("captured-live-6.02-or-6.04", "captured-live-ILCE-7RM5"):
            baseline = settings_path
            validate_settings(settings_bytes, settings_bytes,
                              expected_sha=tx.digest(settings_path),
                              expected_versions=None if profile == "captured-live-ILCE-7RM5" else ("6.02", "6.04"))
        else:
            validate_settings(baseline.read_bytes(), settings_bytes)
        tx.prepare(stage, kind="updater_restore",
                   regions=[{"name": "nflasha1", "original": updater, "target": target}],
                   identity=[{"region": "nflasha", "bytes": 512,
                              "sha256": tx.digest(sector0)}],
                   input_files=[baseline] + ([_path(task["inputs"]["dat"])] if task["inputs"].get("dat") else []),
                   metadata={"parent": str(job_dir), "stage": "erlla-to-oem",
                             "settings_profile": profile,
                             "settings_baseline": str(baseline), "source_proof": proof})
        plan = tx.reconcile(stage, transport=transport,
                            validator=self._stock_updater_validator(task, stage),
                            progress=progress)
        manifest = json.loads((stage / "manifest.json").read_text())
        sectors = sum(end - start for start, end in manifest["regions"][0]["scope"])
        target_hash = manifest["regions"][0]["target_sha256"]
        return _result(job_dir, plan["status"],
                       f"Erlla→OEM updater: target SHA-256 {target_hash}; "
                       f"{sectors} changed sectors; no reboot in this stage",
                       "Apply updater stage; stock trigger follows verified readback",
                       confirmation=plan["status"] == "READY",
                       plan_digest=tx.digest(stage / "manifest.json"),
                       capability_verified=True)

    def _composite_stock_plan(self, job_dir: Path, task: dict) -> dict:
        pin = job_dir / "stock-composite-plan.json"
        if not pin.exists():
            raise ValueError("complete stock plan has not been prepared")
        if tx.digest(pin) != task.get("composite_plan_digest"):
            raise ValueError("stock composite plan digest changed")
        plan = json.loads(pin.read_text())
        if plan.get("schema") != "stock-composite-plan-v1":
            raise ValueError("unsupported stock composite plan")
        for name, field in (("manifest.json", "settings_manifest_sha256"),
                            ("updater-stage/manifest.json", "updater_manifest_sha256")):
            path = job_dir / name
            if (tx.digest(path) if path.exists() else None) != plan[field]:
                raise ValueError(f"stock stage plan changed: {name}")
        tx.load(job_dir)
        if plan["updater_manifest_sha256"]:
            tx.load(job_dir / "updater-stage")
        return plan

    def stock_plan(self, job_dir: Path, progress: Progress | None = None) -> dict:
        """Read-only live preflight that pins both stages before one confirmation."""
        job_dir = _path(job_dir)
        task = _task(job_dir)
        if task["kind"] != "stock_sd_recovery":
            raise ValueError("stock_plan requires stock SD task")
        emit(progress, "stock_model", f"SD guided recovery selected model={task.get('inputs', {}).get('camera_model', 'ILCE-7M4')}; checking updater and settings")
        pin = job_dir / "stock-composite-plan.json"
        if pin.exists():
            plan = self._composite_stock_plan(job_dir, task)
        else:
            if (job_dir / "updater-stage/journal.json").exists():
                updater_state = json.loads((job_dir / "updater-stage/journal.json").read_text())
                if updater_state["write_may_have_occurred"]:
                    raise ValueError("updater write exists without complete stock plan")
            with self._stock_transport(job_dir, task, progress) as transport:
                updater_gate = self._stock_updater_stage(job_dir, task, transport, progress)
                if updater_gate is not None and updater_gate["status"] != "READY":
                    return updater_gate
                stage_manifest = job_dir / "updater-stage/manifest.json"
                target = (Path(json.loads(stage_manifest.read_text())["regions"][0]["target"])
                          if stage_manifest.exists() else None)
                self._stock_transaction(job_dir, task, transport, updater_target=target)
            settings_manifest = job_dir / "manifest.json"
            stock = json.loads(settings_manifest.read_text())
            stage = (json.loads(stage_manifest.read_text()) if stage_manifest.exists() else None)
            scope = stock["regions"][0]["scope"]
            sectors = [sector for start, end in scope for sector in range(start, end)]
            reports = [Path(pin["path"]) for pin in stock["input_files"]
                       if Path(pin["path"]).name == "stock-plan.json"]
            if len(reports) != 1:
                raise ValueError("stock transaction lacks one pinned semantic trigger report")
            stock_report = json.loads(reports[0].read_text())
            semantic_sectors = sorted({offset // 512 for offset in stock_report["plan"]["changed_offsets"]})
            if (sectors != stock_report["sparse_sectors"] or
                    sectors != semantic_sectors or
                    not stock["reboot"]):
                raise ValueError("stock settings scope or reboot differs from reviewed plan")
            plan = {"schema": "stock-composite-plan-v1",
                    "updater_manifest_sha256": tx.digest(stage_manifest) if stage else None,
                    "settings_manifest_sha256": tx.digest(settings_manifest),
                    "updater_target_sha256": (stage["regions"][0]["target_sha256"] if stage
                                              else stock["identity"][1]["sha256"]),
                    "updater_changed_sectors": (sum(b-a for a,b in stage["regions"][0]["scope"])
                                                if stage else 0),
                    "settings_target_sha256": stock["regions"][0]["target_sha256"],
                    "settings_changed_sectors": len(sectors),
                    "settings_sectors": sectors, "reboot": True,
                    "updater_environment": stock_report["updater_environment"],
                    "target_package_version": stock_report.get("target_package_version"),
                    "host_dat_sha256": stock_report.get("host_dat_sha256"),
                    "prepared_card_dat_sha256": stock_report.get("prepared_card_dat_sha256") }
            tx.durable_json(pin, plan)
            task["plan_digest"] = tx.digest(settings_manifest)
            task["composite_plan_digest"] = tx.digest(pin)
            tx.durable_json(job_dir / "task.json", task)
        summary = (f"OEM updater target {plan['updater_target_sha256']} "
                   f"({plan['updater_changed_sectors']} sectors); settings target "
                   f"{plan['settings_target_sha256']} "
                   f"(sectors {', '.join(map(str, plan['settings_sectors']))}); one reboot")
        auth = job_dir / "stock-authorization.json"
        confirmed = auth.exists() and json.loads(auth.read_text()).get("plan_digest") == task["composite_plan_digest"]
        stock_journal = job_dir / "journal.json"
        if stock_journal.exists() and json.loads(stock_journal.read_text()).get("reboot_may_have_occurred"):
            stock_state = json.loads(stock_journal.read_text())
            result = _result(job_dir, "NEEDS_USER_ACTION", "Stock reboot may already have occurred",
                             "Observe camera update screen and version; do not resend reboot",
                             plan_digest=task["composite_plan_digest"], capability_verified=True)
            result["confirmation_recorded"] = confirmed
            result["stock_reboot_acknowledged"] = stock_state.get("status") == "REBOOT_ACKNOWLEDGED"
            result.update(_stock_ack_time(stock_journal, stock_state))
            return result
        result = _result(job_dir, "READY", summary,
                       "Confirm the complete updater and SD trigger plan once",
                       confirmation=not confirmed, plan_digest=task["composite_plan_digest"],
                       capability_verified=True)
        result["confirmation_recorded"] = confirmed
        return result

    def stock_run(self, job_dir: Path, progress: Progress | None = None,
                  confirmed_plan_digest: str | None = None) -> dict:
        """Advance a previously confirmed two-stage stock plan; never replan here."""
        job_dir = _path(job_dir)
        task = _task(job_dir)
        if task["kind"] != "stock_sd_recovery":
            raise ValueError("stock_run requires stock SD task")
        phase = "plan"
        emit(progress, "stock_model", f"SD guided recovery selected model={task.get('inputs', {}).get('camera_model', 'ILCE-7M4')}; applying updater and settings stages")
        try:
            plan = self._composite_stock_plan(job_dir, task)
            _check_manifest_pin(job_dir, task)
            auth = job_dir / "stock-authorization.json"
            if auth.exists():
                if json.loads(auth.read_text()).get("plan_digest") != task["composite_plan_digest"]:
                    raise ValueError("saved stock confirmation differs from pinned plan")
            elif confirmed_plan_digest == task["composite_plan_digest"]:
                tx.durable_json(auth, {"schema": "stock-authorization-v1",
                                       "plan_digest": confirmed_plan_digest,
                                       "confirmed_utc": datetime.now(timezone.utc).isoformat()})
            else:
                return _result(job_dir, "NO_WRITE", "Complete stock plan is unconfirmed",
                               "Review the plan and confirm its exact digest",
                               confirmation=True, plan_digest=task["composite_plan_digest"])
            stock_state = json.loads((job_dir / "journal.json").read_text())
            if stock_state["reboot_may_have_occurred"]:
                result = _result(job_dir, "NEEDS_USER_ACTION", "Reboot may already have occurred",
                                 "Observe camera update screen; do not resend reboot",
                                 plan_digest=task["composite_plan_digest"], capability_verified=True)
                result["stock_reboot_acknowledged"] = stock_state.get("status") == "REBOOT_ACKNOWLEDGED"
                result.update(_stock_ack_time(job_dir / "journal.json", stock_state))
                return result
            if plan["updater_manifest_sha256"]:
                stage = job_dir / "updater-stage"
                stage_state = json.loads((stage / "journal.json").read_text())
                if stage_state["status"] != "VERIFIED":
                    phase = "updater"
                    emit(progress, "updater", "Reconciling and applying pinned OEM updater")
                    tx.apply(stage, transport_factory=self._stock_factory(job_dir, task, progress),
                             validator=self._stock_updater_validator(task, stage), progress=progress)
            phase = "settings"
            emit(progress, "settings", "Reconciling and applying pinned SD settings trigger")
            state = tx.apply(job_dir, transport_factory=self._stock_factory(job_dir, task, progress),
                             progress=progress)
            if state["status"] == "REBOOT_ACKNOWLEDGED":
                result = _result(job_dir, "NEEDS_USER_ACTION", "Stock reboot acknowledged once",
                                 "Observe camera update screen and version",
                                 plan_digest=task["composite_plan_digest"], capability_verified=True)
                result["stock_reboot_acknowledged"] = True
                result.update(_stock_ack_time(job_dir / "journal.json", state))
                return result
            return _result(job_dir, state["status"], "Stock storage target verified",
                           "Inspect receipt", plan_digest=task["composite_plan_digest"],
                           capability_verified=True)
        except Exception as exc:
            evidence = _stock_failure_evidence(job_dir)
            if evidence["reboot_may_have_occurred"] is True:
                status = "REBOOT_UNCERTAIN"
                phase = "reboot"
            elif (evidence["write_may_have_occurred"] is True or
                  evidence["journal_valid"] is False):
                status = "UNKNOWN_AFTER_WRITE"
            else:
                status = "NO_WRITE"
            emit(progress, "stock", str(exc), status=status, phase=phase,
                 error_code=type(exc).__name__)
            stale_plan = (status == "NO_WRITE" and
                          "unrecognized live sectors in" in str(exc))
            result = _result(job_dir, status, str(exc),
                             ("Create a new stock task from fresh camera reads; preserve this rejected plan"
                              if stale_plan else
                              "Reconcile actual sectors against the pinned plan before continuation"),
                             plan_digest=task.get("composite_plan_digest"))
            result.update(evidence)
            result.update(phase=phase, error_code=type(exc).__name__,
                          failure_cause=str(exc))
            if stale_plan:
                result["stale_plan"] = True
            return result


    def _restore_idle_session(self, job_dir: Path, task: dict, stage: Path,
                              progress: Progress | None) -> IdleUSBSession:
        """Observe a captured RAM session only while pure host work runs.

        USB stays on the calling thread.  The 512-byte content anchor is a
        liveness check against this capture, not proof of physical identity;
        the transaction still performs its full identity and live triage.
        """
        current = job_dir / "current-snapshot"
        source = current / "nflasha.raw.bin"
        capture_receipt = current / "capture-receipt.json"
        verified_path = current / "verified.json"
        receipt = json.loads(capture_receipt.read_text())
        regions = snapshot.captured_regions(receipt)
        if (receipt.get("status") != "VERIFIED" or not verified_path.is_file() or
                set(json.loads(verified_path.read_text())) != set(regions)):
            raise ValueError("restore RAM idle check requires a verified current capture")
        user_available = "nflasha" in regions
        if user_available:
            first = receipt.get("segments", {}).get("nflasha", {}).get("first", [])
            if not first or first[0].get("offset") != 0:
                raise ValueError("verified restore capture lacks first user segment")
            first_path = current / first[0]["file"]
            if (first_path.resolve().parent != (current / "segments/nflasha.first").resolve() or
                    first_path.is_symlink() or first_path.stat().st_size != first[0]["bytes"] or
                    tx.digest(first_path) != first[0]["sha256"]):
                raise ValueError("saved restore capture first segment changed")
            with source.open("rb") as stream:
                assembled_first = stream.read(first[0]["bytes"])
            if hashlib.sha256(assembled_first).hexdigest() != first[0]["sha256"]:
                raise ValueError("saved restore capture user image differs from verified segment")
        if (stage / "manifest.json").is_file():
            stage_task = _task(stage)
            manifest_path = stage / "manifest.json"
            if tx.digest(manifest_path) != stage_task.get("plan_digest"):
                raise ValueError("saved restore stage manifest changed")
            pins = json.loads(manifest_path.read_text()).get("input_files", [])
            for local in (capture_receipt, verified_path):
                matching = [item for item in pins if Path(item["path"]).resolve() == local.resolve()]
                if len(matching) != 1 or tx.digest(local) != matching[0]["sha256"]:
                    raise ValueError("saved restore capture receipt pin changed")
        anchor = None
        if user_available:
            with source.open("rb") as stream:
                anchor = stream.read(512)
            if len(anchor) != 512:
                raise ValueError("verified restore capture lacks a sector-zero anchor")
        policy_path = job_dir / "restore-session-policy.json"
        if policy_path.is_file():
            policy = json.loads(policy_path.read_text())
            if policy.get("schema") != "restore-ram-session-v1":
                raise ValueError("saved restore session policy differs")
        else:
            policy = {"schema": "restore-ram-session-v1",
                      "policy": task.get("session_policy", "fragment-restore-session-v1"),
                      "created_utc": datetime.now(timezone.utc).isoformat(),
                      "retries_used": 0, "idle_reentries_total": 0}
            tx.durable_json(policy_path, policy)
        probe_dir = job_dir / "session-probes"
        probe_dir.mkdir(exist_ok=True, mode=0o700)

        def probe() -> bool:
            path = probe_dir / f"sector0-{uuid4().hex}.bin"
            try:
                with self.transport_factory() as transport:
                    if transport.verify_product() != "ILCE-7M4":
                        raise ValueError("RAM Fastboot product changed")
                    if anchor is not None:
                        transport.read_region("nflasha", path, offset=0, length=512)
                if anchor is not None and path.read_bytes() != anchor:
                    raise ValueError("RAM liveness anchor differs from captured sector zero")
                emit(progress, "restore_idle_probe", "RAM Fastboot content anchor remains readable" if anchor is not None
                     else "RAM Fastboot product is readable; no immutable content anchor available")
                return True
            except USBReadError as exc:
                if not snapshot.is_transient_camera_usb(exc):
                    raise
                emit(progress, "restore_idle_probe", "RAM Fastboot USB read interrupted during local verification")
                return False
            except FastbootError as exc:
                if not tx._fragment_transport_unavailable(exc):
                    raise
                emit(progress, "restore_idle_probe", "RAM Fastboot unavailable during local verification")
                return False
            finally:
                path.unlink(missing_ok=True)
                path.with_name(path.name + ".partial").unlink(missing_ok=True)

        def stage_state() -> dict | None:
            path = stage / "journal.json"
            return json.loads(path.read_text()) if path.is_file() else None

        def can_reenter() -> bool:
            state = stage_state()
            if state is None:
                return True
            if state.get("reboot_may_have_occurred") is not False:
                return False
            if (state.get("status") in {"READY", "NO_WRITE"} and
                    state.get("write_may_have_occurred") is False and
                    state.get("confirmed_fragments") == 0):
                return True
            manifest_path = stage / "manifest.json"
            task_path = stage / "task.json"
            if not manifest_path.is_file() or not task_path.is_file():
                return False
            saved_task = json.loads(task_path.read_text())
            if tx.digest(manifest_path) != saved_task.get("plan_digest"):
                return False
            manifest = json.loads(manifest_path.read_text())
            if state.get("inflight") is not None:
                return tx._fragment_inflight_reentry_boundary(stage, manifest, state)
            return tx._verified_region_boundary(stage, manifest, state)

        def reenter() -> None:
            while True:
                state = stage_state()
                if state is not None and state.get("manual_retry_sequence", 0) > policy.get("manual_retry_sequence", 0):
                    policy["manual_retry_sequence"] = state["manual_retry_sequence"]
                    policy["retries_used"] = state.get("restore_retry_used", 0)
                    tx.durable_json(policy_path, policy)
                if (state is not None and state.get("reboot_may_have_occurred") is False and
                        state.get("confirmed_fragments", 0) > policy.get("verified_fragment_count", 0)):
                    manifest_path = stage / "manifest.json"
                    stage_task_path = stage / "task.json"
                    if (not manifest_path.is_file() or not stage_task_path.is_file() or
                            tx.digest(manifest_path) != json.loads(stage_task_path.read_text()).get("plan_digest") or
                            not (tx._fragment_inflight_reentry_boundary(
                                     stage, json.loads(manifest_path.read_text()), state)
                                 if state.get("inflight") else
                                 tx._verified_region_boundary(
                                     stage, json.loads(manifest_path.read_text()), state))):
                        raise RuntimeError("verified restore progress lacks full readback evidence")
                    policy["verified_fragment_count"] = state["confirmed_fragments"]
                    policy["retries_used"] = 0
                    state["restore_retry_used"] = 0
                    state["restore_retry_verified_fragments"] = state["confirmed_fragments"]
                    tx.durable_json(stage / "journal.json", state)
                    tx.durable_json(policy_path, policy)
                used = (max(state.get("restore_retry_used", 0), policy.get("retries_used", 0))
                        if state is not None else policy.get("retries_used", 0))
                if type(used) is not int or used < 0 or used >= tx.MAX_RESTORE_RECONNECTS:
                    raise RuntimeError("restore RAM reconnect budget exhausted")
                if not can_reenter():
                    raise RuntimeError("restore RAM reentry refused outside verified safe boundary")
                used += 1
                if state is not None:
                    state["restore_retry_used"] = used
                    state["reentry_intent"] = {
                        "retry": used, "reason": "idle_probe_unavailable",
                        "force_bridge": True,
                        "manifest_sha256": (tx.digest(stage / "manifest.json")
                                            if (stage / "manifest.json").is_file() else None),
                        "inflight_position": (state["inflight"]["position"]
                                              if state.get("inflight") else None),
                        "recorded_utc": datetime.now(timezone.utc).isoformat(),
                    }
                    tx.durable_json(stage / "journal.json", state)
                policy["retries_used"] = used
                policy["idle_reentries_total"] = policy.get("idle_reentries_total", 0) + 1
                tx.durable_json(policy_path, policy)
                emit(progress, "restore_idle_reentry", f"RAM session unavailable; guarded entry {used}/{tx.MAX_RESTORE_RECONNECTS}")
                try:
                    with self._transport(task, progress, force_bridge=True):
                        pass
                    return
                except Exception as exc:
                    if not tx._fragment_transport_unavailable(exc):
                        raise
                    if used >= tx.MAX_RESTORE_RECONNECTS:
                        raise

        return IdleUSBSession(probe=probe, reenter=reenter,
                              can_reenter=can_reenter, progress=progress)


    def _finish_restore(self, job_dir: Path, task: dict, result: dict,
                        progress: Progress | None = None) -> dict:
        """One finalizer for zero-diff, resumed and newly applied eMMC restores."""
        from . import nor_rebuild_flow
        emmc = dict(result)
        try:
            nor = nor_rebuild_flow.finish(job_dir, task, emmc, backend=self, progress=progress)
        except Exception as exc:
            # Keep native failures distinct from the already-verified eMMC journal.
            nor = {"nor_status":"INCOMPLETE", "nor_executed":False,
                   "nor_array_verified":False, "nor_companion_verified":False,
                   "nor_cleanup_completed":False, "nor_skip_reason":str(exc),
                   "nor_receipt":str(job_dir / "nor-flow.json"),
                   "normal_return_handled":True, "reboot_sent":False,
                   "normal_mode_status":"NEEDS_USER_ACTION", "current_mode":"UNKNOWN"}
        result.update(nor, emmc_status=emmc["status"])
        skipped = [row for row in emmc.get("skipped_regions", []) if row.get("name") != "cp-nor"]
        if nor["nor_status"] == "VERIFIED":
            result.update(completion_scope="planned_emmc_fragments_and_native_nor",
                          summary="eMMC restore, native NOR rebuild and temporary entry cleanup verified")
        else:
            skipped.append({"name":"cp-nor", "reason":
                            "native_rebuild_unsupported_profile" if nor["nor_status"] == "UNSUPPORTED"
                            else "native_rebuild_not_completed", "detail":nor.get("nor_skip_reason")})
            if nor["nor_status"] in {"INCOMPLETE", "UNKNOWN"}:
                result.update(status="UNKNOWN_AFTER_WRITE" if nor["nor_status"] == "UNKNOWN" else "INCOMPLETE",
                              summary="eMMC restore verified; native NOR rebuild did not complete",
                              next_action="Review the saved native NOR receipt before another camera operation")
            elif nor["nor_status"] == "NOT_STARTED" and emmc["status"] == "VERIFIED":
                result.update(status="INCOMPLETE",
                              summary="eMMC restore verified; native NOR rebuild did not start",
                              next_action="Check the saved NOR error before starting a new restore")
            elif nor["nor_status"] == "UNSUPPORTED":
                result.update(summary="eMMC restore verified; native NOR rebuild is unsupported for this profile",
                              next_action="Review skipped regions and check camera live view and menus")
        result.update(skipped_regions=skipped, success_with_skips=bool(skipped))
        tx.durable_json(job_dir / "restore-result.json", result)
        return result

    def restore_run(self, job_dir: Path, progress: Progress | None = None) -> dict:
        """Start or resume one-target restore with a private fresh preimage."""
        job_dir = _path(job_dir)
        task = _task(job_dir)
        if task["kind"] != "scoped_restore" or task["inputs"].get("current_snapshot"):
            raise ValueError("restore_run requires a single target snapshot task")
        if task.get("validation_mode") != scoped_restore_plan.FRAGMENT_VALIDATION_MODE:
            return _result(job_dir, "NEEDS_USER_ACTION", "Old restore tasks are not continued",
                           "Select the verified backup snapshot and create a new restore task")
        target = _path(task["inputs"]["target_snapshot"])
        restore_profile = task["inputs"].get("restore_profile") or "capability"
        pins = task["target_snapshot_pins"]
        stage = job_dir / "scoped-stage"
        stage_was_ready = (stage / "manifest.json").is_file()
        phase = "capture"
        try:
            if any(tx.digest(target / name) != digest for name, digest in pins.items()):
                raise ValueError("selected target backup receipt changed")
            if self.backup_fastboot_state is not None and not (job_dir / "restore-start.json").exists():
                from .card_connection import pending_restore_outcomes
                pending = pending_restore_outcomes(job_dir)
                if pending:
                    emit(progress, "restore_prior_state", "Checking ordinary Sony USB after an unresolved restore")
                    try:
                        observed = observe_normal_camera_usb()
                    except Exception as exc:
                        emit(progress, "restore_prior_state", f"USB observation failed: {exc}")
                        observed = None
                    if not observed or self.backup_fastboot_state() != "departed":
                        result = _result(job_dir, "NEEDS_USER_ACTION",
                            "Previous restore has an unconfirmed write or reboot.",
                            "Confirm the camera has returned to ordinary Sony USB, then start a fresh restore from the same backup.")
                        return {**result, "blocking_tasks": pending, "restore_previous_pending": True,
                                "write_may_have_occurred": False}
                    tx.durable_json(job_dir / "prior-restore-observation.json",
                        {"observed_utc": datetime.now(timezone.utc).isoformat(),
                         "mode": observed["status"], "previous_tasks": pending})
                    emit(progress, "restore_prior_state",
                         f"Ordinary Sony USB verified ({observed['status']}); starting a fresh read and diff")
            from . import nor_rebuild_flow
            admission_path = job_dir / "nor-admission.json"
            if admission_path.exists():
                admission = json.loads(admission_path.read_text())
            else:
                admission = nor_rebuild_flow.admission(target)
                tx.durable_json(admission_path, admission)
            if progress:
                if admission.get("resource_error"):
                    message = f"NOR profile matched but local resources are unavailable: {admission['resource_error']}"
                elif admission["supported"]:
                    message = f"Snapshot matches NOR component profile: {admission.get('profile') or 'matched'}"
                else:
                    message = f"No matching NOR profile for snapshot: {admission.get('reason') or 'unspecified'}"
                progress({"stage": "snapshot_nor_profile", "message": message})
            start = job_dir / "restore-start.json"
            if not start.exists():
                tx.durable_json(start, {"schema": "restore-start-v1",
                                        "target_plan_digest": task["plan_digest"],
                                        "started_utc": datetime.now(timezone.utc).isoformat()})
            elif json.loads(start.read_text()).get("target_plan_digest") != task["plan_digest"]:
                raise ValueError("saved restore start differs from selected backup")
            session_policy = job_dir / "restore-session-policy.json"
            if not session_policy.is_file():
                tx.durable_json(session_policy,
                                {"schema": "restore-ram-session-v1",
                                 "policy": task.get("session_policy", "fragment-restore-session-v1"),
                                 "created_utc": datetime.now(timezone.utc).isoformat(),
                                 "retries_used": 0, "idle_reentries_total": 0})
            if not (stage / "manifest.json").exists():
                current = job_dir / "current-snapshot"
                capture_receipt = current / "capture-receipt.json"
                decrypted_manifest = current / "decrypted/manifest.json"
                already_verified = (
                    (current / "verified.json").is_file() and
                    capture_receipt.is_file() and decrypted_manifest.is_file() and
                    json.loads(capture_receipt.read_text()).get("status") == "VERIFIED" and
                    json.loads(decrypted_manifest.read_text()).get("status") == "VERIFIED")
                if already_verified:
                    decrypt_snapshot.verify_snapshot(current)
                    emit(progress, "snapshot", "Reusing verified current restore capture")
                else:
                    emit(progress, "snapshot", "Capturing current eMMC regions twice; CP NOR is not read")
                first_open = True
                def capture_transport():
                    nonlocal first_open
                    if first_open:
                        first_open = False
                        return self._transport(task, progress)
                    return self.transport_factory()
                def guarded_capture_reentry():
                    emit(progress, "restore_reconnect", "Checking guarded RAM Fastboot reentry during restore capture")
                    return self._transport(task, progress)
                def capture_progress(event):
                    if progress:
                        if event.get("stage") in {"backup_retry", "backup_retry_reset"}:
                            event = {**event, "stage": event["stage"].replace("backup_", "restore_capture_")}
                        progress(event)
                next_capture_probe = time.monotonic() + 10
                def capture_idle_hook(folder, receipt, receipt_path, retry_state):
                    nonlocal next_capture_probe
                    if time.monotonic() < next_capture_probe:
                        return
                    next_capture_probe = time.monotonic() + 10
                    reads = receipt.get("reads", {})
                    if any(reads.get(region, {}).get("first", {}).get("sha256") !=
                           reads.get(region, {}).get("second", {}).get("sha256") or
                           not reads.get(region, {}).get("first", {}).get("sha256")
                           for region in ("nflashaB0", "nflashaB1")):
                        return  # No double-read identity yet; do not reset.
                    pairs = receipt.get("segments", {}).get("nflasha", {})
                    first = next((item for item in pairs.get("first", []) if item.get("offset") == 0), None)
                    second = next((item for item in pairs.get("second", []) if item.get("offset") == 0), None)
                    if not first or not second or first.get("sha256") != second.get("sha256"):
                        return
                    saved = folder / first["file"]
                    saved_second = folder / second["file"]
                    if (saved.resolve().parent != (folder / "segments/nflasha.first").resolve() or
                            saved_second.resolve().parent != (folder / "segments/nflasha.second").resolve() or
                            saved.is_symlink() or saved_second.is_symlink()):
                        raise ValueError("saved restore capture segment path changed")
                    if (tx.digest(saved) != first["sha256"] or
                            tx.digest(saved_second) != second["sha256"]):
                        raise ValueError("saved restore capture first segment changed during idle check")
                    with saved.open("rb") as stream:
                        anchor = stream.read(512)
                    probe = folder / f"capture-idle-{uuid4().hex}.bin"
                    try:
                        try:
                            with self.transport_factory() as transport:
                                if transport.verify_product() != "ILCE-7M4":
                                    raise ValueError("capture RAM product changed")
                                transport.read_region("nflasha", probe, offset=0, length=512)
                            if probe.read_bytes() != anchor:
                                raise ValueError("capture RAM sector-zero anchor changed")
                            return
                        except USBReadError as exc:
                            if not (snapshot.is_transient_camera_usb(exc) or tx._retryable_fragment_usb(exc)):
                                raise
                            pass
                        except FastbootError as exc:
                            if not tx._fragment_transport_unavailable(exc):
                                raise
                    finally:
                        probe.unlink(missing_ok=True)
                        probe.with_name(probe.name + ".partial").unlink(missing_ok=True)
                    while True:
                        if retry_state["used"] >= snapshot.MAX_USB_RETRIES:
                            raise RuntimeError("restore capture RAM reconnect budget exhausted")
                        retry_state["used"] += 1
                        receipt["retry_consecutive_used"] = retry_state["used"]
                        receipt.setdefault("retry_events", []).append({
                            "reason": "idle_probe_unavailable", "reentry_requested": True,
                            "retry": retry_state["used"], "max_retries": snapshot.MAX_USB_RETRIES,
                            "recorded_utc": datetime.now(timezone.utc).isoformat()})
                        snapshot.save(receipt_path, receipt)
                        emit(progress, "restore_capture_idle_reentry",
                             f"Capture RAM lost during host assembly; guarded entry {retry_state['used']}/{snapshot.MAX_USB_RETRIES}")
                        try:
                            with guarded_capture_reentry() as transport:
                                snapshot._verify_resume_identity(transport, folder, receipt)
                            return
                        except RamPayloadNotReady:
                            if retry_state["used"] >= snapshot.MAX_USB_RETRIES:
                                raise
                if not already_verified:
                    snapshot.capture(current, transport_factory=capture_transport,
                                     auto_retry_usb=True, progress=capture_progress,
                                     reentry_factory=guarded_capture_reentry,
                                     profile="restore_current", idle_hook=capture_idle_hook,
                                     backup_fastboot_state=self.backup_fastboot_state)
                capture_budget = json.loads(capture_receipt.read_text()).get("retry_consecutive_used", 0)
                if type(capture_budget) is not int or not 0 <= capture_budget <= snapshot.MAX_USB_RETRIES:
                    raise ValueError("saved restore capture retry count invalid")
                saved_policy = json.loads(session_policy.read_text())
                if capture_budget > saved_policy.get("retries_used", 0):
                    saved_policy["retries_used"] = capture_budget
                    saved_policy["capture_retry_count_inherited"] = capture_budget
                    tx.durable_json(session_policy, saved_policy)
                idle = self._restore_idle_session(job_dir, task, stage, progress)
                phase = "plan"
                def host_plan(cancelled, host_progress):
                    if not already_verified:
                        for partial in (current / "decrypted").glob("*.partial"):
                            partial.rename(partial.with_name(partial.name + f".interrupted-{uuid4().hex}"))
                        decrypt_snapshot.decrypt(current)
                        decrypt_snapshot.verify_snapshot(current)
                    if cancelled.is_set():
                        raise HostWorkCancelled("restore local plan cancelled")
                    emit(host_progress, "restore_plan",
                         "Calculating changed fragments for this backup")
                    plan = scoped_restore_plan.build_plan(current, target, profile=restore_profile,
                                                         validation_mode=task.get("validation_mode"))
                    fragments = [fragment for row in plan["partitions"]
                                 for fragment in row["fragments"]]
                    planned_bytes = sum(fragment["sectors"] * tx.SECTOR
                                        for fragment in fragments)
                    host_progress({"stage": "restore_plan_summary",
                                   "planned_fragments": len(fragments),
                                   "planned_bytes": planned_bytes,
                                   "message": f"Planned {len(fragments)} fragments; "
                                              f"{planned_bytes} target bytes to write"})
                    if cancelled.is_set():
                        raise HostWorkCancelled("restore local plan cancelled")
                    if not plan["partitions"]:
                        tx.durable_json(job_dir / "already-target.json",
                                        {"schema": "restore-already-target-v1",
                                         "current_verified_sha256": tx.digest(current / "verified.json"),
                                         "target_verified_sha256": tx.digest(target / "verified.json")})
                        return plan
                    if stage.exists():
                        if (stage / "journal.json").exists():
                            raise ValueError("scoped stage journal exists without manifest")
                        archived = job_dir / f"scoped-stage.incomplete-{uuid4().hex}"
                        stage.rename(archived)
                    self.prepare("scoped_restore",
                                 {"current_snapshot": str(current), "target_snapshot": str(target),
                                  "restore_profile": "capability",
                                  **{key: value for key, value in task["inputs"].items()
                                     if key in ("port", "payload")}}, stage, host_progress)
                    return plan
                plan = idle.run_host(host_plan)
                if not plan["partitions"]:
                    return self._finish_restore(job_dir, task,
                        _restore_verified_result(job_dir, plan, plan_digest=task["plan_digest"]), progress)
                saved_policy = json.loads(session_policy.read_text())
                stage_journal = stage / "journal.json"
                if saved_policy.get("retries_used", 0):
                    stage_state = json.loads(stage_journal.read_text())
                    stage_state["restore_retry_used"] = max(
                        stage_state.get("restore_retry_used", 0), saved_policy["retries_used"])
                    tx.durable_json(stage_journal, stage_state)
            phase = "transaction"
            stage_task = _task(stage)
            if tx.digest(stage / "manifest.json") != stage_task["plan_digest"]:
                raise ValueError("saved scoped restore manifest changed")
            stage_manifest = json.loads((stage / "manifest.json").read_text())
            stage_state = json.loads((stage / "journal.json").read_text())
            if stage_was_ready:
                saved_fragments = [fragment
                                   for row in stage_manifest["metadata"]["offline_plan"]["partitions"]
                                   for fragment in row["fragments"]]
                saved_bytes = sum(fragment["sectors"] * tx.SECTOR
                                  for fragment in saved_fragments)
                emit(progress, "restore_plan_summary",
                     f"Planned {len(saved_fragments)} fragments; {saved_bytes} target bytes to write",
                     planned_fragments=len(saved_fragments), planned_bytes=saved_bytes)
            if stage_state["status"] in {"VERIFIED", "PARTIAL"}:
                stage_manifest, stage_state = tx.load(stage, progress=progress)
                _verify_saved_scoped_readbacks(stage, stage_manifest, stage_state, progress)
                return self._finish_restore(job_dir, task,
                    _restore_verified_result(job_dir, stage_manifest["metadata"]["offline_plan"],
                                             plan_digest=stage_task["plan_digest"], state=stage_state), progress)
            _check_manifest_pin(stage, stage_task)
            def guarded_restore_transport(*, force_bridge: bool = False):
                journal_path = stage / "journal.json"
                state = json.loads(journal_path.read_text()) if journal_path.exists() else {}
                if state.get("reboot_may_have_occurred"):
                    raise RuntimeError("current restore transaction has unresolved write or reboot outcome")
                if state.get("inflight") and not tx._fragment_inflight_reentry_boundary(
                        stage, json.loads((stage / "manifest.json").read_text()), state):
                    raise RuntimeError("current restore fragment intent or readback evidence differs")
                emit(progress, "restore_reconnect", "Checking guarded RAM Fastboot reentry")
                emit(progress, "restore_write_session_entry",
                     "Local restore files verified; entering fresh RAM write session" if force_bridge
                     else "Checking RAM write session after local verification")
                return self._transport(task, progress, force_bridge=force_bridge)
            idle = self._restore_idle_session(job_dir, task, stage, progress)
            def host_load():
                def verify(cancelled, host_progress):
                    result = tx.load(stage, progress=host_progress, cancelled=cancelled)
                    if tx.digest(stage / "manifest.json") != stage_task["plan_digest"]:
                        raise ValueError("saved restore manifest changed during local verification")
                    return result
                return idle.run_host(verify)
            def guarded_restore_reentry(*, force_bridge: bool = False):
                with guarded_restore_transport(force_bridge=force_bridge):
                    pass
            state = tx.apply_with_reconnect(stage,
                                            transport_factory=self.transport_factory,
                                            progress=progress,
                                            reconnect=guarded_restore_reentry,
                                            host_load=host_load,
                                            max_retries=5)
            if state["status"] in {"VERIFIED", "PARTIAL"}:
                _verify_saved_scoped_readbacks(stage, stage_manifest, state, progress)
                return self._finish_restore(job_dir, task,
                    _restore_verified_result(job_dir, stage_manifest["metadata"]["offline_plan"],
                                             plan_digest=stage_task["plan_digest"], state=state), progress)
            return _result(job_dir, state["status"], "Restore not fully verified",
                           "Inspect receipt", plan_digest=stage_task["plan_digest"])
        except Exception as exc:
            journal = stage / "journal.json"
            saved_state = _saved_restore_journal_state(stage) if journal.is_file() else None
            state = saved_state or {}
            uncertain_journal = ((stage / "manifest.json").is_file() and saved_state is None)
            status = ("UNKNOWN_AFTER_WRITE" if uncertain_journal or state.get("write_may_have_occurred")
                      else "NO_WRITE" if (stage / "manifest.json").exists() or phase == "plan"
                      else "INCOMPLETE")
            details = _restore_failure_details(exc, phase, stage)
            emit(progress, "restore", details["failure_cause"], status=status)
            result = _result(job_dir, status, str(exc),
                           "Review the saved fragment evidence; select the backup snapshot for a new restore",
                           plan_digest=task["plan_digest"])
            result.update(details)
            if details["secondary_failure_cause"]:
                result["summary"] = details["failure_cause"]
            if progress:
                progress({"stage": "restore_failure", "job_dir": str(job_dir),
                          "status": status, "message": details["failure_cause"], **details})
            if phase == "capture" and isinstance(exc, snapshot.CaptureFailure):
                result["restore_failure_origin"] = exc.details["origin"]
            receipt = job_dir / "current-snapshot/capture-receipt.json"
            if phase == "capture" and receipt.is_file():
                try:
                    saved = json.loads(receipt.read_text())
                    result["restore_retry_used"] = saved.get(
                        "retry_consecutive_used", len(saved.get("retry_events", [])))
                    result["restore_retry_total"] = len(saved.get("retry_events", []))
                    result["restore_retry_limit"] = snapshot.MAX_USB_RETRIES
                except (OSError, ValueError, TypeError):
                    result["capture_receipt_valid"] = False
            if details["journal_valid"] and (stage / "manifest.json").is_file():
                result["restore_retry_used"] = state.get("restore_retry_used", 0)
                result["restore_retry_limit"] = tx.MAX_RESTORE_RECONNECTS
            return result

    def reconcile(self, job_dir: Path, progress: Progress | None = None) -> dict:
        job_dir = _path(job_dir)
        task = _task(job_dir)
        kind = task["kind"]
        if kind == "scoped_restore" and task.get("validation_mode") != scoped_restore_plan.FRAGMENT_VALIDATION_MODE:
            return _result(job_dir, "NEEDS_USER_ACTION", "Old restore tasks are not continued",
                           "Select the verified backup snapshot and create a new restore task")
        if kind == "scoped_restore":
            return _result(job_dir, "NEEDS_USER_ACTION", "Restore starts from a backup snapshot",
                           "Select the verified snapshot and create a new restore task")
        emit(progress, "reconcile", "Checking current device state")
        try:
            _check_manifest_pin(job_dir, task)
            if kind == "scoped_restore" and not task.get("capability_verified"):
                return _result(job_dir, "NEEDS_USER_ACTION", "Offline plan only; write route unreviewed",
                               "Obtain a reviewed capability profile for this exact task",
                               plan_digest=task["plan_digest"])
            journal_path = job_dir / "journal.json"
            if journal_path.exists():
                saved = json.loads(journal_path.read_text())
                if saved.get("reboot_may_have_occurred"):
                    return _result(job_dir, "NEEDS_USER_ACTION",
                                   "Reboot may already have occurred",
                                   "Check camera screen and version; do not reenter RAM or resend reboot",
                                   plan_digest=task["plan_digest"], capability_verified=True)
            if kind == "board_install":
                info = board_install.inspect(volume=_path(task["inputs"]["bootsel_volume"])
                                             if task["inputs"].get("bootsel_volume") else None,
                                             port=task["inputs"].get("port"))
                ports = info.get("ports", [])
                if ports and info["bootsel"]:
                    return _result(job_dir, "NEEDS_USER_ACTION",
                                   "RP2040 runtime port and BOOTSEL volume both found",
                                   "Disconnect the other board or identify the intended device before installation",
                                   plan_digest=task["plan_digest"])
                if len(ports) == 1:
                    try:
                        with Bridge(ports[0]) as bridge:
                            version = bridge.ping()
                    except Exception:
                        version = None
                    if version in board_install.KNOWN_PINGS:
                        return _result(job_dir, "ALREADY_INSTALLED",
                                       "RP2040 bridge PING verified",
                                       "Board ready; no UF2 write needed",
                                       plan_digest=task["plan_digest"], capability_verified=True,
                                       control_ping_verified=True)
                    return _result(job_dir, "NEEDS_USER_ACTION",
                                   "RP2040 runtime port did not answer known bridge PING",
                                   "Disconnect USB, hold BOOT while reconnecting, then release BOOT and check again",
                                   plan_digest=task["plan_digest"])
                if len(ports) > 1:
                    return _result(job_dir, "NEEDS_USER_ACTION", "Multiple RP2040 ports found",
                                   "Identify one board port before continuing",
                                   plan_digest=task["plan_digest"])
                if len(info["bootsel"]) == 1:
                    return _result(job_dir, "READY", "One verified RP2040 BOOTSEL volume",
                                   "Confirm UF2 installation", confirmation=True,
                                   plan_digest=task["plan_digest"], capability_verified=True)
                if len(info["bootsel"]) > 1:
                    return _result(job_dir, "NEEDS_USER_ACTION",
                                   "Multiple RP2040 BOOTSEL volumes found",
                                   "Select the intended board volume in advanced settings",
                                   plan_digest=task["plan_digest"])
                return _result(job_dir, "NEEDS_USER_ACTION", "No verified RP2040 runtime or BOOTSEL",
                               "Connect the RP2040 in BOOTSEL mode",
                               plan_digest=task["plan_digest"])
            if kind == "full_backup":
                with self._transport(task, progress) as transport:
                    sizes, unavailable = {}, {}
                    for name in snapshot.REGIONS:
                        try:
                            size = transport.region_size(name)
                            if type(size) is not int or size <= 0 or size % 512:
                                raise FastbootError("invalid reader extent")
                            sizes[name] = size
                        except FastbootError as exc:
                            if "unavailable" in str(exc):
                                raise
                            unavailable[name] = str(exc)
                tx.durable_json(job_dir / "region-sizes.json", sizes)
                tx.durable_json(job_dir / "region-coverage.json", {"readable": sizes, "unavailable": unavailable})
                result = _result(job_dir, "READY" if sizes else "INCOMPLETE",
                               f"RAM reader exposes {len(sizes)} raw regions; {len(unavailable)} unavailable",
                               "Capture backup", confirmation=True,
                               capability_verified=bool(sizes))
                result.update(captured_region_sizes=sizes, unavailable_regions=unavailable,
                              physical_device_complete=False)
                return result
            with (self._stock_transport(job_dir, task, progress) if kind == "stock_sd_recovery"
                  else self._transport(task, progress)) as transport:
                if kind == "stock_sd_recovery":
                    updater_stage = self._stock_updater_stage(job_dir, task, transport, progress)
                    if updater_stage is not None:
                        return updater_stage
                    self._stock_transaction(job_dir, task, transport)
                    task["plan_digest"] = tx.digest(job_dir / "manifest.json")
                    tx.durable_json(job_dir / "task.json", task)
                plan = tx.reconcile(job_dir, transport=transport,
                                    validator=self._updater_validator(job_dir), progress=progress)
            status = plan["status"]
            if kind == "stock_sd_recovery" and status == "READY":
                manifest = json.loads((job_dir / "manifest.json").read_text())
                candidate = manifest["regions"][0]["target_sha256"]
                return _result(job_dir, "READY",
                               f"Stock settings trigger: target SHA-256 {candidate}; "
                               "sectors 133 and 197; one reboot after full readback",
                               "Confirm settings trigger and one reboot",
                               confirmation=True, plan_digest=task["plan_digest"],
                               capability_verified=True)
            if kind == "stock_sd_recovery" and status == "VERIFIED":
                state = json.loads((job_dir / "journal.json").read_text())
                if state.get("write_may_have_occurred") and not state.get("reboot_may_have_occurred"):
                    status = "READY"
                    return _result(job_dir, status, "Storage target verified; reboot remains unsent",
                                   "Review and send one reboot", confirmation=True,
                                   plan_digest=task["plan_digest"], capability_verified=True)
            return _result(job_dir, status, "Live sectors reconciled",
                           "Apply only pending sectors" if status == "READY" else "Review result",
                           confirmation=status == "READY", plan_digest=task["plan_digest"],
                           capability_verified=True)
        except Exception as exc:
            status = "INCOMPLETE"
            journal = job_dir / "journal.json"
            if kind == "stock_sd_recovery" and not journal.exists():
                journal = job_dir / "updater-stage/journal.json"
            if journal.exists():
                recorded = json.loads(journal.read_text()).get("status")
                if recorded in {"NO_WRITE", "UNKNOWN_AFTER_WRITE", "REBOOT_UNCERTAIN"}:
                    status = recorded
            emit(progress, "reconcile", str(exc), status=status)
            return _result(job_dir, status, str(exc), "Inspect task journal and reconnect",
                           plan_digest=task.get("plan_digest"))

    def apply(self, job_dir: Path, progress: Progress | None = None) -> dict:
        job_dir = _path(job_dir)
        task = _task(job_dir)
        kind = task["kind"]
        if kind == "scoped_restore":
            return _result(job_dir, "NEEDS_USER_ACTION", "Restore starts from a backup snapshot",
                           "Select the verified snapshot and create a new restore task")
        emit(progress, "apply", f"Applying {kind}")
        try:
            _check_manifest_pin(job_dir, task)
            if kind == "stock_sd_recovery" and (job_dir / "journal.json").exists():
                stock_state = json.loads((job_dir / "journal.json").read_text())
                if stock_state.get("reboot_may_have_occurred"):
                    return _result(job_dir, "NEEDS_USER_ACTION",
                                   "Stock reboot may already have occurred",
                                   "Check camera update screen and version; do not resend reboot",
                                   plan_digest=task["plan_digest"], capability_verified=True)
            if kind == "scoped_restore" and not task.get("capability_verified"):
                return _result(job_dir, "NO_WRITE", "Scoped restore route unreviewed",
                               "Keep diagnostic plan for independent review",
                               plan_digest=task["plan_digest"])
            if kind == "board_install":
                result = board_install.install(uf2=_path(task["inputs"].get("uf2") or
                                                         board_install.default_uf2()),
                                               volume=_path(task["inputs"]["bootsel_volume"])
                                               if task["inputs"].get("bootsel_volume") else None,
                                               port=task["inputs"].get("port"),
                                               evidence_dir=job_dir)
                return _result(job_dir, "VERIFIED", result["status"], "Board ready",
                               plan_digest=task["plan_digest"], capability_verified=True,
                               control_ping_verified=True)
            if kind == "full_backup":
                def guarded_reentry():
                    try:
                        _backup_reentry_checkpoint(job_dir / "snapshot")
                    except Exception as exc:
                        raise RuntimeError(
                            f"automatic RAM reentry blocked by current backup checkpoint: {exc}") from exc
                    emit(progress, "backup_reconnect", "Checking guarded RAM Fastboot reentry")
                    return self._transport(task, progress)
                def inspect_fastboot():
                    state = self.backup_fastboot_state()
                    emit(progress, "backup_fastboot_probe", {
                        "present": "Windows found one Fastboot device; checking direct reconnect",
                        "departed": "Windows Fastboot device departed; checking continuation",
                    }.get(state, "Windows Fastboot enumeration uncertain; checking direct connection before any reset"))
                    return state

                folder = job_dir / "snapshot"
                receipt_path = folder / "capture-receipt.json"
                if receipt_path.is_file() and self.backup_fastboot_state is None:
                    saved = json.loads(receipt_path.read_text())
                    saved_segments = any(rows for passes in saved.get("segments", {}).values()
                                         for rows in passes.values())
                    if saved.get("status") != "VERIFIED" and (saved.get("reads") or saved_segments):
                        try:
                            with self.transport_factory() as transport:
                                snapshot._verify_resume_identity(transport, folder, saved)
                        except snapshot.BackupAnchorMismatch as exc:
                            item = exc.evidence
                            failure = snapshot.CaptureFailure(
                                origin="identity_or_protocol", region=item["region"], attempt="resume",
                                offset=item["offset"], path=folder, cause=exc)
                            saved.update(status="INCOMPLETE", error=str(failure), failure=failure.details)
                            snapshot.save(receipt_path, saved)
                            raise failure from exc
                        emit(progress, "backup_anchor_verified", "Saved and live backup anchors match")
                backup_receipt = snapshot.capture(
                    folder, transport_factory=self.transport_factory, auto_retry_usb=True,
                    progress=progress, reentry_factory=guarded_reentry,
                    backup_fastboot_state=inspect_fastboot if self.backup_fastboot_state else None)
                decrypted = decrypt_snapshot.decrypt(job_dir / "snapshot")
                result = _backup_verified_result(job_dir, backup_receipt, decrypted)
                result.update(backup_retry_used=backup_receipt.get("retry_consecutive_used", 0),
                              backup_retry_total=len(backup_receipt.get("retry_events", [])),
                              backup_retry_limit=(snapshot.WINDOWS_BACKUP_USB_RETRIES if self.backup_fastboot_state
                                                  else snapshot.MAX_USB_RETRIES))
                return result
            if kind == "stock_sd_recovery" and (job_dir / "updater-stage/manifest.json").exists():
                stage = job_dir / "updater-stage"
                _, updater_state = tx.load(stage)
                if updater_state["status"] != "VERIFIED":
                    emit(progress, "updater", "Restoring reviewed OEM updater first")
                    tx.apply(stage, transport_factory=self._stock_factory(job_dir, task, progress),
                             validator=self._stock_updater_validator(task, stage), progress=progress)
                if not (job_dir / "manifest.json").exists():
                    gate = self.reconcile(job_dir, progress)
                    return gate  # New stock target needs its own GUI confirmation.
            if kind == "stock_sd_recovery" and not (job_dir / "manifest.json").exists():
                return _result(job_dir, "NEEDS_USER_ACTION", "Stock candidate not prepared",
                               "Run read-only reconcile and review candidate before apply",
                               plan_digest=task["plan_digest"])
            stock_factory = (self._stock_factory(job_dir, task, progress)
                             if kind == "stock_sd_recovery" else self._factory(task, progress))
            state = tx.apply(job_dir, transport_factory=stock_factory,
                             validator=self._updater_validator(job_dir), progress=progress)
            status = state["status"]
            if status == "REBOOT_ACKNOWLEDGED":
                return _result(job_dir, "NEEDS_USER_ACTION", "Reboot acknowledged once",
                               "Observe camera update screen and version",
                               plan_digest=task["plan_digest"], capability_verified=True)
            return _result(job_dir, status, "Full partition readback verified",
                           "Inspect receipt", plan_digest=task["plan_digest"],
                           capability_verified=True)
        except Exception as exc:
            status = "INCOMPLETE"
            journal = job_dir / "journal.json"
            if kind == "stock_sd_recovery" and not journal.exists():
                journal = job_dir / "updater-stage/journal.json"
            if journal.exists():
                status = json.loads(journal.read_text()).get("status", status)
            emit(progress, "apply", str(exc), status=status)
            result = _result(job_dir, status, str(exc),
                             "Reconcile actual sectors before any continuation",
                             plan_digest=task.get("plan_digest"))
            if kind == "full_backup" and isinstance(exc, snapshot.CaptureFailure):
                result["backup_failure_origin"] = exc.details["origin"]
                result["next_action"] = ("Check the camera connection and continue the same backup task; "
                                         "the saved checkpoint will be verified before reading more data")
                drift = exc.details.get("anchor_mismatch")
                if drift is not None:
                    result["backup_anchor_mismatch"] = drift
                    result["next_action"] = "Keep this backup and recapture into a new directory"
                if exc.details["origin"] == "safety_guard":
                    result["failed_check"] = "current_backup_retry_checkpoint"
                receipt = job_dir / "snapshot/capture-receipt.json"
                if receipt.is_file():
                    backup_receipt = json.loads(receipt.read_text())
                    result["backup_retry_used"] = backup_receipt.get(
                        "retry_consecutive_used", len(backup_receipt.get("retry_events", [])))
                    result["backup_retry_total"] = len(backup_receipt.get("retry_events", []))
                    result["backup_retry_limit"] = (snapshot.WINDOWS_BACKUP_USB_RETRIES if self.backup_fastboot_state
                                                    else snapshot.MAX_USB_RETRIES)
            return result

    def backup_resume_status(self, job_dir: Path) -> dict:
        """Inspect the saved backup receipt before the UI starts a camera action."""
        job_dir = _path(job_dir)
        if _task(job_dir)["kind"] != "full_backup":
            raise ValueError("select an existing full backup task")
        mismatch = (snapshot.saved_emmc_drift(job_dir / "snapshot")
                    if self.backup_fastboot_state is None else None)
        if mismatch is not None:
            result = _result(job_dir, "NEEDS_USER_ACTION", "Saved eMMC content changed",
                             "Use a new directory for read-only regional recapture")
            return {**result, "backup_anchor_mismatch": mismatch}
        return _result(job_dir, "READY", "Saved backup task is ready to resume", "Continue backup")

    def backup_recapture(self, prior_snapshot: Path, output: Path, *,
                         same_camera_confirmed: bool, region: str | None = None,
                         progress: Progress | None = None) -> dict:
        """Start an independent backup on an already-active Fastboot connection."""
        if region is not None:
            from .backup_region import recapture
            return recapture(prior_snapshot, output, region=region,
                             same_camera_confirmed=same_camera_confirmed,
                             transport_factory=self.transport_factory, progress=progress)
        prior_snapshot = _path(prior_snapshot)
        output = _path(output)
        if same_camera_confirmed is not True:
            return _result(output, "NEEDS_USER_ACTION", "Confirm the same physical camera",
                           "Confirm the same camera and choose a new snapshot directory")
        previous = json.loads((prior_snapshot / "capture-receipt.json").read_text())
        mismatch = (previous.get("failure") or {}).get("anchor_mismatch") or {}
        prior_manifest = prior_snapshot / "verified.json"
        saved = json.loads(prior_manifest.read_text()) if prior_manifest.is_file() else {}
        if previous.get("status") != "INCOMPLETE" or previous.get("capture_profile") is not None:
            raise ValueError("select an incomplete full-backup capture")
        emmc = saved.get("nflasha")
        passes = previous.get("reads", {}).get("nflasha", {})
        if (mismatch.get("region") != "nflasha" or not emmc or
                any(passes.get(attempt, {}).get(key) != emmc.get(key)
                    for attempt in ("first", "second") for key in ("bytes", "sha256"))):
            raise ValueError("previous backup does not document drift after completing the requested region")
        anchors = snapshot.resume_anchors(prior_snapshot, previous, only_region="nflasha")
        expected = {key: mismatch.get(key) for key in ("region", "offset", "bytes", "saved_sha256")}
        if not any(expected == {"region": row["region"], "offset": row["offset"],
                                "bytes": row["bytes"], "saved_sha256": row["sha256"]}
                   for row in anchors):
            raise ValueError("recorded drift does not match a saved double-read anchor")
        if output == prior_snapshot or output.is_relative_to(prior_snapshot):
            raise ValueError("choose a new snapshot directory outside the prior snapshot")
        output.mkdir(mode=0o700)
        generation = {"status": "IN_PROGRESS", "same_camera_confirmed_by_operator": True,
                      "previous_snapshot": str(prior_snapshot), "anchor_mismatch": mismatch,
                      "reused_regions": [], "atomic_camera_snapshot": False,
                      "requested_region": None,
                      "started_utc": datetime.now(timezone.utc).isoformat()}
        provenance = output / "recapture.json"
        tx.durable_json(provenance, generation)
        try:
            with self.transport_factory() as transport:
                transport.verify_product()
            emit(progress, "backup_recapture", "Reading all exposed regions twice in a new snapshot")
            receipt = snapshot.capture(output, transport_factory=self.transport_factory,
                                       require_all_regions=True, progress=progress)
            decrypted = decrypt_snapshot.decrypt(output)
            decrypt_snapshot.verify_snapshot(output)
        except Exception as exc:
            generation.update(status="INCOMPLETE", error=str(exc),
                              finished_utc=datetime.now(timezone.utc).isoformat())
            tx.durable_json(provenance, generation)
            return _result(output, "INCOMPLETE", str(exc),
                           "Keep both captures and review the recapture evidence")
        generation["finished_utc"] = receipt["finished_utc"]
        generation["status"] = "VERIFIED"
        tx.durable_json(provenance, generation)
        return {**_backup_verified_result(output, receipt, decrypted),
                "snapshot": str(output), "previous_snapshot": str(prior_snapshot),
                "reused_regions": [], "atomic_camera_snapshot": False, "reboot_sent": False}

    def verify(self, job_dir: Path, progress: Progress | None = None) -> dict:
        job_dir = _path(job_dir)
        task = _task(job_dir)
        kind = task["kind"]
        _check_manifest_pin(job_dir, task)
        emit(progress, "verify", "Reading saved evidence")
        if kind == "scoped_restore" and not task["inputs"].get("current_snapshot"):
            target = _path(task["inputs"]["target_snapshot"])
            current = job_dir / "current-snapshot"
            profile = task["inputs"].get("restore_profile") or "capability"
            try:
                if any(tx.digest(target / name) != digest
                       for name, digest in task["target_snapshot_pins"].items()):
                    raise ValueError("selected target backup receipt changed")
                decrypt_snapshot.verify_snapshot(current)
                plan = scoped_restore_plan.build_plan(current, target, profile=profile,
                                                     validation_mode=task.get("validation_mode"))
                stage = job_dir / "scoped-stage"
                if not plan["partitions"]:
                    if not (job_dir / "already-target.json").is_file():
                        raise ValueError("zero-write restore has no saved verification receipt")
                    return _restore_verified_result(job_dir, plan, plan_digest=task["plan_digest"])
                stage_task = _task(stage)
                _check_manifest_pin(stage, stage_task)
                manifest, state = tx.load(stage)
                if state["status"] not in {"VERIFIED", "PARTIAL"}:
                    return _result(job_dir, state["status"], "Restore readback is incomplete",
                                   "Resume the same restore task", plan_digest=stage_task["plan_digest"])
                _verify_saved_scoped_readbacks(stage, manifest, state, progress)
                return _restore_verified_result(job_dir, plan, plan_digest=stage_task["plan_digest"], state=state)
            except Exception as exc:
                return _result(job_dir, "INCOMPLETE", str(exc), "Inspect saved restore evidence")
        if kind == "stock_sd_recovery" and not (job_dir / "manifest.json").exists():
            stage = job_dir / "updater-stage"
            if (stage / "journal.json").exists():
                state = json.loads((stage / "journal.json").read_text())
                if state["status"] == "UNKNOWN_AFTER_WRITE":
                    return _result(job_dir, "UNKNOWN_AFTER_WRITE",
                                   "Updater write outcome requires live sector reconciliation",
                                   "Reconnect and reconcile updater before continuing",
                                   plan_digest=task["plan_digest"], capability_verified=True)
            return self.reconcile(job_dir, progress)
        if kind in ("scoped_restore", "updater_restore", "stock_sd_recovery"):
            try:
                manifest, state = tx.load(job_dir)
            except Exception as exc:
                return _result(job_dir, "INCOMPLETE", str(exc), "Repair or reprepare task")
            if state["reboot_may_have_occurred"]:
                return _result(job_dir, "NEEDS_USER_ACTION", state["status"],
                               "Check camera screen and version; do not resend reboot",
                               plan_digest=task["plan_digest"], capability_verified=True)
            if (manifest.get("metadata", {}).get("validation_mode") == scoped_restore_plan.FRAGMENT_VALIDATION_MODE
                    and state["status"] in {"VERIFIED", "PARTIAL"}):
                _verify_saved_scoped_readbacks(job_dir, manifest, state)
                return _restore_verified_result(job_dir, manifest["metadata"]["offline_plan"],
                                                plan_digest=task["plan_digest"], state=state)
            if state["status"] == "VERIFIED":
                return _result(job_dir, "VERIFIED", "Storage readback verified",
                               "Task complete", plan_digest=task["plan_digest"],
                               capability_verified=True)
            return self.reconcile(job_dir, progress)
        if kind == "full_backup":
            folder = job_dir / "snapshot"
            try:
                decrypt_snapshot.verify_snapshot(folder)
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                return _result(job_dir, "INCOMPLETE", str(exc),
                               "Resume capture after reconnect")
            else:
                return _backup_verified_result(job_dir,
                    json.loads((folder / "capture-receipt.json").read_text()),
                    json.loads((folder / "decrypted/manifest.json").read_text()))
        return self.reconcile(job_dir, progress)

    def speed_probe(self, job_dir: Path, progress: Progress | None = None) -> dict:
        """Optional read-only camera throughput sample; no uptime estimate."""
        job_dir = _path(job_dir)
        _task(job_dir)
        sample = job_dir / "speed-probe-4mib.bin"
        if sample.exists() or sample.with_name(sample.name + ".partial").exists():
            raise FileExistsError("speed probe output already exists")
        emit(progress, "speed_probe", "Reading 4 MiB from RAM Fastboot")
        start = time.monotonic()
        with self.transport_factory() as transport:
            transport.read_region("nflasha", sample, length=4 * 1024 * 1024)
        elapsed = time.monotonic() - start
        result = _result(job_dir, "VERIFIED", "Read-only camera transfer sample complete",
                         "Review measured rate", capability_verified=True)
        result.update({"bytes": sample.stat().st_size, "seconds": elapsed,
                  "mib_per_second": sample.stat().st_size / 1024 / 1024 / elapsed,
                  "sha256": tx.digest(sample), "remaining_ram_window_seconds": None,
                  "scope": "read-only camera transfer sample; RAM session lifetime not measured"})
        tx.durable_json(job_dir / "speed-probe.json", result)
        return result
