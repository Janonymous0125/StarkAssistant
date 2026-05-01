from __future__ import annotations

from typing import Dict, Sequence


def refine_concepts(items: Sequence[dict], *, dry_run: bool = True) -> Dict[str, object]:
    return {"ok": True, "dry_run": bool(dry_run), "candidate_count": len(list(items or [])), "writes": []}
