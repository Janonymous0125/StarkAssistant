from __future__ import annotations

from pathlib import Path
from typing import Dict


def summarize_brainstem_health(root: Path | str) -> Dict[str, object]:
    path = Path(root)
    return {
        "ok": path.exists(),
        "path": str(path),
        "has_logs": (path / "logs").exists(),
        "has_assistant": (path / "assistant").exists(),
    }
