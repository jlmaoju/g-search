from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNTIME_PATH_ENV = "GCORES_DAILY_RUNTIME_PATH"
STEP_KEY_ENV = "GCORES_DAILY_STEP_KEY"
STEP_LABEL_ENV = "GCORES_DAILY_STEP_LABEL"
STEP_INDEX_ENV = "GCORES_DAILY_STEP_INDEX"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_STALE_SECONDS = 300.0
LOCK_POLL_INTERVAL_SECONDS = 0.05


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_runtime(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "unknown", "steps": [], "updated_at": utc_now_iso()}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "unknown", "steps": [], "updated_at": utc_now_iso()}
    if not isinstance(payload, dict):
        return {"status": "unknown", "steps": [], "updated_at": utc_now_iso()}
    payload.setdefault("steps", [])
    payload.setdefault("updated_at", utc_now_iso())
    return payload


def save_runtime(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    last_error: Exception | None = None
    for _ in range(5):
        try:
            temp_path.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(LOCK_POLL_INTERVAL_SECONDS)
    if temp_path.exists():
        temp_path.unlink(missing_ok=True)
    if last_error is not None:
        raise last_error


class RuntimeFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")
        self.acquired = False

    def __enter__(self) -> "RuntimeFileLock":
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        payload = json.dumps({"pid": os.getpid(), "created_at": utc_now_iso()}, ensure_ascii=False)
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                self.acquired = True
                return self
            except (FileExistsError, PermissionError):
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age >= LOCK_STALE_SECONDS:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        continue
                    except PermissionError:
                        pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for runtime lock: {self.lock_path}")
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            self.acquired = False


def _find_step(payload: dict[str, Any], step_key: str) -> dict[str, Any]:
    steps = payload.setdefault("steps", [])
    for step in steps:
        if str(step.get("key") or "") == step_key:
            return step
    step: dict[str, Any] = {"key": step_key, "status": "pending"}
    steps.append(step)
    steps.sort(key=lambda entry: int(entry.get("index") or 10_000))
    return step


def update_run_state(
    path: Path,
    *,
    status: str | None = None,
    run_label: str | None = None,
    total_steps: int | None = None,
    current_step_key: str | None = None,
    current_step_index: int | None = None,
    current_step_label: str | None = None,
    message: str | None = None,
    note: str | None = None,
    new_run: bool = False,
) -> dict[str, Any]:
    with RuntimeFileLock(path):
        payload = load_runtime(path)
        now = utc_now_iso()
        previous_status = str(payload.get("status") or "")
        starting_run = status == "running" and (new_run or previous_status != "running")
        if starting_run:
            payload["run_id"] = uuid.uuid4().hex
            payload["steps"] = []
            for key in ("current_step_key", "current_step_index", "current_step_label", "message"):
                payload.pop(key, None)
        if run_label is not None:
            payload["run_label"] = run_label
        if total_steps is not None:
            payload["total_steps"] = int(total_steps)
        if status is not None:
            payload["status"] = status
            if status == "running":
                if starting_run:
                    payload["started_at"] = now
                payload.pop("finished_at", None)
                payload.pop("note", None)
            if status in {"completed", "failed", "paused"}:
                payload["finished_at"] = now
        if current_step_key is not None:
            payload["current_step_key"] = current_step_key
        if current_step_index is not None:
            payload["current_step_index"] = int(current_step_index)
        if current_step_label is not None:
            payload["current_step_label"] = current_step_label
        if message is not None:
            payload["message"] = message
        if note is not None:
            payload["note"] = note
        payload["updated_at"] = now
        save_runtime(path, payload)
        return payload


def update_step_state(
    path: Path,
    step_key: str,
    *,
    step_index: int | None = None,
    step_label: str | None = None,
    status: str | None = None,
    current: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with RuntimeFileLock(path):
        payload = load_runtime(path)
        step = _find_step(payload, step_key)
        now = utc_now_iso()
        previous_status = str(step.get("status") or "")
        if (status == "running" and previous_status != "running") or status == "skipped":
            for key in ("started_at", "finished_at", "progress", "details"):
                step.pop(key, None)
        if step_index is not None:
            step["index"] = int(step_index)
        if step_label is not None:
            step["label"] = step_label
        if status is not None:
            step["status"] = status
            if status == "running":
                if previous_status != "running":
                    step["started_at"] = now
                step.pop("finished_at", None)
            if status in {"completed", "failed", "skipped", "paused"}:
                step["finished_at"] = now
        progress = step.setdefault("progress", {})
        if current is not None:
            progress["current"] = int(current)
        if total is not None:
            progress["total"] = int(total)
        if unit is not None:
            progress["unit"] = unit
        if message is not None:
            progress["message"] = message
        current_value = progress.get("current")
        total_value = progress.get("total")
        if step.get("status") == "completed":
            # The phase is finished; keep the actual processed counts for auditing.
            progress["percent"] = 100.0
        elif isinstance(current_value, int) and isinstance(total_value, int) and total_value > 0:
            percent = max(0.0, min(100.0, round((current_value / total_value) * 100.0, 2)))
            progress["percent"] = percent
        elif "percent" in progress:
            progress.pop("percent", None)
        if extra:
            details = step.setdefault("details", {})
            details.update(extra)
        payload["status"] = payload.get("status") or "running"
        payload["current_step_key"] = step_key
        if step_index is not None:
            payload["current_step_index"] = int(step_index)
        elif step.get("index") is not None:
            payload["current_step_index"] = int(step["index"])
        if step_label is not None:
            payload["current_step_label"] = step_label
        elif step.get("label"):
            payload["current_step_label"] = step["label"]
        payload["updated_at"] = now
        save_runtime(path, payload)
        return payload


class DailyRuntimeReporter:
    def __init__(
        self,
        *,
        path: Path | None,
        step_key: str | None,
        step_index: int | None = None,
        step_label: str | None = None,
    ) -> None:
        self.path = path
        self.step_key = step_key
        self.step_index = step_index
        self.step_label = step_label

    @property
    def enabled(self) -> bool:
        return self.path is not None and bool(self.step_key)

    def update(
        self,
        *,
        status: str | None = None,
        current: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled or self.path is None or not self.step_key:
            return
        try:
            update_step_state(
                self.path,
                self.step_key,
                step_index=self.step_index,
                step_label=self.step_label,
                status=status,
                current=current,
                total=total,
                unit=unit,
                message=message,
                extra=extra,
            )
            if status is not None:
                update_run_state(
                    self.path,
                    status="running" if status == "running" else None,
                    current_step_key=self.step_key,
                    current_step_index=self.step_index,
                    current_step_label=self.step_label,
                    message=message,
                )
        except Exception:
            return

    def complete(self, *, message: str | None = None, extra: dict[str, Any] | None = None) -> None:
        self.update(status="completed", message=message, extra=extra)

    def fail(self, *, message: str | None = None, extra: dict[str, Any] | None = None) -> None:
        self.update(status="failed", message=message, extra=extra)


def get_daily_runtime_reporter() -> DailyRuntimeReporter:
    raw_path = str(os.environ.get(RUNTIME_PATH_ENV) or "").strip()
    raw_key = str(os.environ.get(STEP_KEY_ENV) or "").strip()
    raw_label = str(os.environ.get(STEP_LABEL_ENV) or "").strip() or None
    raw_index = str(os.environ.get(STEP_INDEX_ENV) or "").strip()
    step_index: int | None = None
    if raw_index:
        try:
            step_index = int(raw_index)
        except ValueError:
            step_index = None
    return DailyRuntimeReporter(
        path=Path(raw_path).resolve() if raw_path else None,
        step_key=raw_key or None,
        step_index=step_index,
        step_label=raw_label,
    )
