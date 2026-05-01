from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Sequence

from assistant.contracts.ids import new_uuid
from assistant.contracts.time import utc_now_iso
from assistant.brain.brainstem.events import BrainObservationEvent
from assistant.brain.prefrontal.goal_generator import InternalGoal
from assistant.brain.prefrontal.initiative_engine import InitiativeDecision


class ProactiveEpisodeWriter:
    def __init__(self, episodes_root: Path | str) -> None:
        self.episodes_root = Path(episodes_root)

    def write_episode(
        self,
        *,
        observations: Sequence[BrainObservationEvent],
        goal: InternalGoal,
        decision: InitiativeDecision,
        verification: str,
        lesson: str | None = None,
    ) -> Dict[str, object]:
        self.episodes_root.mkdir(parents=True, exist_ok=True)
        episode_id = new_uuid()
        ts = utc_now_iso()
        payload = {
            "schema_version": "proactive_episode.v1",
            "type": "proactive_episode",
            "episode_id": episode_id,
            "created_at": ts,
            "observation_ids": [event.event_id for event in observations],
            "observations": [event.to_json_dict() for event in observations],
            "goal": goal.to_json_dict(),
            "goal_id": goal.goal_id,
            "selected_action": decision.decision,
            "initiative_decision": decision.to_json_dict(),
            "authority_required": bool(decision.requires_user_approval),
            "result": "pending_user_approval" if decision.requires_user_approval else "recorded",
            "verification": verification,
            "lesson": lesson,
        }
        json_path = self.episodes_root / f"{episode_id}.json"
        md_path = self.episodes_root / f"{episode_id}.md"
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        md_path.write_text(
            "\n".join(
                [
                    f"# Proactive Episode {episode_id}",
                    "",
                    f"- Created: {ts}",
                    f"- Goal: {goal.title}",
                    f"- Decision: {decision.decision}",
                    f"- Verification: {verification}",
                    "",
                    goal.reason,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return {"episode_id": episode_id, "json_path": str(json_path), "markdown_path": str(md_path), "payload": payload}
