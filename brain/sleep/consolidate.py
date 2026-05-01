from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List


def consolidate_episodes(episodes_root: Path | str, *, dry_run: bool = True) -> Dict[str, object]:
    root = Path(episodes_root)
    episodes: List[dict] = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("type") == "proactive_episode":
            episodes.append(payload)
    grouped: Dict[str, int] = {}
    for episode in episodes:
        key = str((episode.get("goal") or {}).get("goal_type") or "unknown")
        grouped[key] = grouped.get(key, 0) + 1
    concept_candidates = [
        {
            "kind": key,
            "episode_count": count,
            "action": "propose_concept_or_procedure",
            "dry_run": bool(dry_run),
        }
        for key, count in sorted(grouped.items())
    ]
    return {
        "schema_version": "sleep_consolidation.v1",
        "ok": True,
        "dry_run": bool(dry_run),
        "episode_count": len(episodes),
        "groups": grouped,
        "concept_candidates": concept_candidates,
        "contradictions": [],
        "writes": [] if dry_run else [],
    }
