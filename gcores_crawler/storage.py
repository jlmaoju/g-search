from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict


class FileStore:
    def __init__(self, root: str = "data") -> None:
        self.root = Path(root)

    @property
    def state_path(self) -> Path:
        return self.root / "runs" / "crawl_state.json"

    @property
    def backfill_state_path(self) -> Path:
        return self.root / "runs" / "backfill_state.json"

    def save_raw(self, content_type: str, item_id: str, payload: Dict[str, Any]) -> Path:
        path = self.root / "raw" / content_type / f"{item_id}.json"
        self._write_json(path, payload)
        return path

    def save_normalized(self, content_type: str, item_id: str, payload: Dict[str, Any]) -> Path:
        path = self.root / "normalized" / content_type / f"{item_id}.json"
        self._write_json(path, payload)
        return path

    def save_summary(self, payload: Dict[str, Any]) -> Path:
        path = self.root / "runs" / "last_run_summary.json"
        self._write_json(path, payload)
        return path

    def save_state(self, payload: Dict[str, Any]) -> Path:
        path = self.state_path
        self._write_json(path, payload)
        return path

    def save_backfill_state(self, payload: Dict[str, Any]) -> Path:
        path = self.backfill_state_path
        self._write_json(path, payload)
        return path

    def load_state(self) -> Dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8-sig"))

    def load_backfill_state(self) -> Dict[str, Any]:
        return json.loads(self.backfill_state_path.read_text(encoding="utf-8-sig"))

    def has_state(self) -> bool:
        return self.state_path.exists()

    def has_backfill_state(self) -> bool:
        return self.backfill_state_path.exists()

    def normalized_exists(self, content_type: str, item_id: str) -> bool:
        path = self.root / "normalized" / content_type / f"{item_id}.json"
        return path.exists()

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if path.exists():
            try:
                if path.read_text(encoding="utf-8-sig") == rendered:
                    return
            except Exception:
                pass
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            rendered,
            encoding="utf-8-sig",
        )
        last_error = None
        for attempt in range(1, 11):
            try:
                temp_path.replace(path)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt == 10:
                    break
                time.sleep(0.05 * attempt)
        raise last_error
