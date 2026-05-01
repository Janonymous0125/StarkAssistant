from __future__ import annotations

from pathlib import Path
import json
from typing import Dict, List, Optional

from assistant.body import BodyCommand
from assistant.brain.hippocampus.episode_writer import ProactiveEpisodeWriter
from assistant.brain.prefrontal.goal_generator import generate_internal_goals
from assistant.brain.prefrontal.initiative_engine import decide_initiative
from assistant.brain.thalamus.attention_router import route_attention

from .observer import LocalProjectObserver


class SupervisedAwakeLoop:
    def __init__(self, *, workspace_root: Path | str, episodes_root: Optional[Path | str] = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.episodes_root = Path(episodes_root) if episodes_root is not None else self.workspace_root / "assistant" / "brain" / "hippocampus" / "episodes"
        self.observer = LocalProjectObserver()

    def run_once(self) -> Dict[str, object]:
        observation_path = self.workspace_root / "assistant" / "brain" / "workspace" / "recent_observations.json"
        observations = self.observer.scan_paths([self.workspace_root], limit=80, persist_path=observation_path)
        attention = route_attention(observations, max_focus=5)
        goals = generate_internal_goals(attention.focus_events)
        decisions = [decide_initiative(goal) for goal in goals]
        body_command_proposals = self._body_command_proposals(goals=goals, decisions=decisions)
        writer = ProactiveEpisodeWriter(self.episodes_root)
        episodes: List[dict] = []
        for goal, decision in zip(goals, decisions):
            related = [event for event in attention.focus_events if event.event_id in goal.evidence_event_ids]
            episodes.append(
                writer.write_episode(
                    observations=related or attention.focus_events[:1],
                    goal=goal,
                    decision=decision,
                    verification="read_only_cycle_completed",
                )
            )
        result = {
            "schema_version": "supervised_awake_loop.v1",
            "ok": True,
            "observations": [event.to_json_dict() for event in observations],
            "attention": attention.to_json_dict(),
            "goals": [goal.to_json_dict() for goal in goals],
            "initiative_decisions": [decision.to_json_dict() for decision in decisions],
            "body_command_proposals": body_command_proposals,
            "episodes": episodes,
            "verification": {
                "mode": "read_only",
                "source_code_edits": False,
                "external_actions": False,
                "body_commands_executed": False,
            },
        }
        state_path = self.workspace_root / "assistant" / "brain" / "workspace" / "current_brain_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return result

    def _body_command_proposals(self, *, goals: List[object], decisions: List[object]) -> List[dict]:
        proposals: List[dict] = []
        for goal, decision in zip(goals, decisions):
            allowed = bool(getattr(decision, "allowed", False))
            authority_level = int(getattr(decision, "authority_level", 7) or 7)
            decision_name = str(getattr(decision, "decision", "") or "")
            goal_id = str(getattr(goal, "goal_id", "") or "")
            if not allowed or authority_level > 2 or decision_name not in {"record", "analyze"}:
                proposals.append(
                    {
                        "goal_id": goal_id,
                        "execute": False,
                        "status": "blocked_or_requires_approval",
                        "reason": str(getattr(decision, "reason", "") or "Decision is not allowed for read-only proactive body command proposal."),
                        "command": None,
                    }
                )
                continue
            command = BodyCommand(
                subsystem="tool_runtime",
                command_type="tool",
                tool_name="project.tree",
                args={"path": ".", "max_depth": 2},
                approval_context={"proactive": True, "read_only": True},
            )
            proposals.append(
                {
                    "goal_id": goal_id,
                    "execute": False,
                    "status": "proposed_read_only",
                    "reason": "Allowed read-only initiative may inspect local project shape, but supervised loop does not execute body commands yet.",
                    "command": command.to_json_dict(),
                }
            )
        return proposals
