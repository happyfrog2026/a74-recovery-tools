"""Persistent local app/task logs and a metadata-only support export."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
import zipfile

SUBFOLDER = "Camera Recovery Logs"
PREFERENCES = Path.home() / "Library/Application Support/Camera Recovery/preferences.json"
DEFAULT_PARENT = Path.home() / "Library/Logs"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


class LogStore:
    """One remembered log location for app sessions and all registered tasks."""

    def __init__(self, *, preferences: Path = PREFERENCES,
                 default_parent: Path = DEFAULT_PARENT):
        self.preferences = Path(preferences).expanduser()
        self.default_parent = Path(default_parent).expanduser().resolve()
        self._lock = threading.RLock()
        self.session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
        try:
            saved = json.loads(self.preferences.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            saved = {}
        parent = Path(saved.get("log_parent") or self.default_parent).expanduser().resolve()
        locations = [Path(item).expanduser().resolve() for item in saved.get("locations", [])]
        self._locations = list(dict.fromkeys([*locations, parent / SUBFOLDER]))
        self._tasks = list(dict.fromkeys(str(Path(item).expanduser().resolve())
                                         for item in saved.get("tasks", [])))
        self._parent = parent
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._save()

    @property
    def directory(self) -> Path:
        return self._parent / SUBFOLDER

    def _save(self) -> None:
        self.preferences.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {"schema": "camera-recovery-logs-v1", "log_parent": str(self._parent),
                "locations": [str(path) for path in self._locations],
                "tasks": self._tasks}
        temporary = self.preferences.with_name(self.preferences.name + f".{uuid.uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.preferences)
        finally:
            temporary.unlink(missing_ok=True)

    def set_directory(self, parent: Path) -> Path:
        parent = Path(parent).expanduser().resolve()
        if not parent.is_dir():
            raise NotADirectoryError(f"Log location is not an existing folder: {parent}")
        with self._lock:
            self._parent = parent
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.directory not in self._locations:
                self._locations.append(self.directory)
            self._save()
            return self.directory

    def append(self, message: str, *, job_dir: Path | None = None,
               level: str = "INFO", source: str = "app") -> None:
        row = {"time_utc": _utc(), "level": level, "source": source,
               "message": str(message), "session": self.session}
        with self._lock:
            _append_jsonl(self.directory / f"session-{self.session}.jsonl", row)
            if job_dir is not None:
                path = Path(job_dir).expanduser().resolve()
                key = str(path)
                if key not in self._tasks:
                    self._tasks.append(key)
                    self._save()
                token = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
                _append_jsonl(self.directory / "tasks" / f"task-{token}.jsonl",
                              row | {"job_dir": key})

    def export(self, destination: Path) -> dict:
        """Archive logs and small-form task journals/receipts, never raw images."""
        destination = Path(destination).expanduser().resolve()
        if destination.suffix.lower() != ".zip":
            raise ValueError("Choose a .zip destination for the log export")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            items: list[tuple[Path, str]] = []
            for index, directory in enumerate(self._locations):
                for folder in (directory, directory / "tasks"):
                    if folder.is_dir():
                        for path in sorted(folder.glob("*.jsonl")):
                            if path.is_file() and not path.is_symlink():
                                relative = path.relative_to(directory)
                                items.append((path, f"logs/location-{index:03d}/{relative}"))
            for index, raw in enumerate(self._tasks):
                task = Path(raw)
                legacy = task / "app-log.jsonl"
                if legacy.is_file() and not legacy.is_symlink():
                    items.append((legacy, f"legacy-tasks/task-{index:03d}/app-log.jsonl"))
                if task.is_dir():
                    for path in sorted(task.rglob("*.json")):
                        if (path.name == "journal.json" or path.name.endswith("receipt.json")) and \
                           path.is_file() and not path.is_symlink():
                            items.append((path, f"task-metadata/task-{index:03d}/{path.relative_to(task)}"))
            temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.tmp")
            manifest = {"schema": "camera-recovery-log-export-v1",
                        "created_utc": _utc(), "files": []}
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path, name in items:
                        digest = hashlib.sha256()
                        size = 0
                        with path.open("rb") as source, archive.open(name, "w") as target:
                            for chunk in iter(lambda: source.read(1 << 20), b""):
                                digest.update(chunk)
                                size += len(chunk)
                                target.write(chunk)
                        manifest["files"].append({"name": name, "bytes": size,
                                                  "sha256": digest.hexdigest()})
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return {"archive": str(destination), "log_files": len(items),
                    "bytes": destination.stat().st_size}
