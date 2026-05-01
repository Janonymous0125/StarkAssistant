from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from assistant.contracts.time import utc_now_iso

from .events import BrainObservationEvent


class LocalProjectObserver:
    """Read-only local/project observer for Stark's proactive loop."""

    _SKIPPED_PARTS = {
        ".git",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        "venv",
        ".venv",
    }
    _SKIPPED_NAMES = {".env", ".env.local", ".env.production"}
    _SKIPPED_SUFFIXES = {".zip", ".7z", ".tar", ".gz", ".rar", ".sqlite", ".db", ".pem", ".key"}

    def scan_paths(
        self,
        paths: Sequence[Path | str],
        *,
        limit: int = 50,
        persist_path: Optional[Path | str] = None,
    ) -> List[BrainObservationEvent]:
        safe_limit = max(0, min(int(limit or 0), 500))
        events: List[BrainObservationEvent] = []
        for root in [Path(path) for path in paths or []]:
            if not root.exists():
                events.append(
                    BrainObservationEvent(
                        source="filesystem",
                        kind="missing_path",
                        summary=f"Configured observation path is missing: {root}",
                        path=str(root),
                        importance=0.35,
                        risk=0.2,
                        novelty=0.5,
                        requires_attention=True,
                        suggested_goal="Review missing observation path",
                    )
                )
                continue
            if root.is_file():
                candidates = [root]
            else:
                candidates = list(self._iter_files(root, max_files=max(safe_limit * 4, 20)))
            for path in candidates:
                events.extend(self._events_for_file(path))
                if len(events) >= safe_limit:
                    break
            if len(events) >= safe_limit:
                break
        events = events[:safe_limit]
        if persist_path is not None:
            self.persist_recent(events, persist_path)
        return events

    def persist_recent(self, events: Sequence[BrainObservationEvent], persist_path: Path | str) -> None:
        path = Path(persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "brain_observations.m9",
            "generated_at": utc_now_iso(),
            "events": [event.to_json_dict() for event in events],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _iter_files(self, root: Path, *, max_files: int) -> Iterable[Path]:
        count = 0
        for path in root.rglob("*"):
            if self._should_skip_path(path):
                continue
            if not path.is_file():
                continue
            count += 1
            yield path
            if count >= max_files:
                break

    def _should_skip_path(self, path: Path) -> bool:
        parts = {str(part).lower() for part in path.parts}
        name = path.name.lower()
        suffix = path.suffix.lower()
        if parts.intersection(self._SKIPPED_PARTS):
            return True
        if name in self._SKIPPED_NAMES:
            return True
        if suffix in self._SKIPPED_SUFFIXES:
            return True
        if "secret" in name or "token" in name or "credential" in name:
            return True
        return False

    def _events_for_file(self, path: Path) -> List[BrainObservationEvent]:
        events: List[BrainObservationEvent] = []
        suffix = path.suffix.lower()
        name = path.name.lower()
        text = ""
        if suffix in {".py", ".md", ".txt", ".log", ".json", ".yaml", ".yml"}:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:12000]
            except Exception:
                text = ""
        lower = text.lower()
        if "todo" in lower or "fixme" in lower:
            events.append(
                BrainObservationEvent(
                    source="files",
                    kind="todo_marker",
                    summary=f"TODO/FIXME marker found in {path.name}",
                    path=str(path),
                    importance=0.45,
                    novelty=0.35,
                    requires_attention=True,
                    suggested_goal="Review unresolved TODO or FIXME marker",
                )
            )
        error_count = lower.count("error") + lower.count("failed") + lower.count("traceback")
        if suffix == ".log" or "log" in name:
            if error_count >= 2:
                kind = "repeated_failure"
                importance = 0.78
                risk = 0.7
            elif error_count == 1:
                kind = "log_error"
                importance = 0.58
                risk = 0.5
            else:
                kind = "log_file"
                importance = 0.2
                risk = 0.05
            events.append(
                BrainObservationEvent(
                    source="logs",
                    kind=kind,
                    summary=f"{path.name} contains {error_count} error/failure markers",
                    path=str(path),
                    importance=importance,
                    risk=risk,
                    novelty=0.45,
                    requires_attention=error_count > 0,
                    suggested_goal=("Investigate repeated log failure" if error_count >= 2 else None),
                )
            )
        return events
