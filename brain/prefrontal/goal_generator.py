from __future__ import annotations

from typing import List, Sequence

from pydantic import Field, field_validator

from assistant.contracts.base import ContractModel
from assistant.contracts.ids import new_uuid
from assistant.brain.brainstem.events import BrainObservationEvent


class InternalGoal(ContractModel):
    schema_version: str = "internal_goal.v1"
    goal_id: str = Field(default_factory=new_uuid)
    origin: str = "proactive"
    goal_type: str
    title: str
    reason: str
    priority: float
    authority_required: str
    status: str = "proposed"
    evidence_event_ids: List[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_supported(cls, value: str) -> str:
        if value != "internal_goal.v1":
            raise ValueError("unsupported internal goal schema_version")
        return value

    @field_validator("origin")
    @classmethod
    def _origin_supported(cls, value: str) -> str:
        text = str(value or "").strip()
        if text not in {"user", "proactive", "system"}:
            raise ValueError("unsupported goal origin")
        return text

    @field_validator("goal_type")
    @classmethod
    def _goal_type_supported(cls, value: str) -> str:
        text = str(value or "").strip()
        if text not in {"user", "maintenance", "learning", "repair", "opportunity", "safety"}:
            raise ValueError("unsupported goal_type")
        return text

    @field_validator("authority_required")
    @classmethod
    def _authority_supported(cls, value: str) -> str:
        text = str(value or "").strip()
        if text not in {"observe_only", "record_memory_only", "read_only_diagnosis", "draft_plan", "ask_permission", "blocked"}:
            raise ValueError("unsupported authority_required")
        return text

    @field_validator("status")
    @classmethod
    def _status_supported(cls, value: str) -> str:
        text = str(value or "").strip()
        if text not in {"proposed", "active", "blocked", "completed", "discarded"}:
            raise ValueError("unsupported goal status")
        return text

    @field_validator("priority")
    @classmethod
    def _score_range(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


def generate_internal_goals(events: Sequence[BrainObservationEvent]) -> List[InternalGoal]:
    goals: List[InternalGoal] = []
    for event in list(events or []):
        priority = max(float(event.importance), float(event.risk), 0.8 if event.kind == "repeated_failure" else 0.0, 0.7 if event.requires_attention else 0.0)
        if priority < 0.5:
            continue
        if event.kind in {"repeated_failure", "log_error"}:
            goal_type = "repair"
            title = event.suggested_goal or f"Investigate {event.summary}"
            authority = "read_only_diagnosis"
        elif event.kind in {"todo_marker", "missing_path"}:
            goal_type = "maintenance"
            title = event.suggested_goal or f"Review {event.summary}"
            authority = "read_only_diagnosis"
        else:
            goal_type = "opportunity"
            title = event.suggested_goal or f"Review observation: {event.summary}"
            authority = "observe_only"
        goals.append(
            InternalGoal(
                origin="proactive",
                goal_type=goal_type,
                title=title[:120],
                reason=event.summary,
                priority=priority,
                authority_required=authority,
                status="proposed",
                evidence_event_ids=[event.event_id],
            )
        )
    return goals
