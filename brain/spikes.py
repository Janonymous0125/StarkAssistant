from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, field_validator

from assistant.contracts.base import ContractModel


SpikePriority = Literal["low", "normal", "high", "critical"]
SpikeRiskLevel = Literal["safe", "needs_review", "approval_required", "blocked"]
SpikeStatus = Literal["observed", "queued", "acknowledged", "dismissed"]
SpikeActionMode = Literal["observe_only", "suggest_only", "draft_only", "approval_required", "blocked"]


class BrainSpikeEvent(ContractModel):
    """Observe-only brain event emitted from ThoughtState signals.

    M-BrainAlive-1 intentionally records awareness only.  A spike can explain
    what Stark noticed, but it must not execute tools or mutate external state.
    """

    spike_id: str
    turn_id: str
    timestamp: str
    source: str = "assistant.brain.pipeline"
    event_type: str
    priority: SpikePriority = "normal"
    urgency: SpikePriority = "normal"
    risk_level: SpikeRiskLevel = "safe"
    action_mode: SpikeActionMode = "observe_only"
    reason: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    status: SpikeStatus = "observed"
    dedupe_key: Optional[str] = None

    @field_validator("event_type", "reason", "source")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text

    @field_validator("spike_id", "turn_id", "timestamp")
    @classmethod
    def _non_empty_id(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text


class BrainSpikeSummary(ContractModel):
    total: int = 0
    highest_priority: SpikePriority = "low"
    approval_required_count: int = 0
    event_types: List[str] = Field(default_factory=list)
    observe_only: bool = True


def spike_summary(spikes: List[BrainSpikeEvent] | None) -> Dict[str, Any]:
    items = list(spikes or [])
    rank = {"low": 0, "normal": 1, "high": 2, "critical": 3}
    highest: SpikePriority = "low"
    for spike in items:
        if rank.get(spike.priority, 0) > rank.get(highest, 0):
            highest = spike.priority
    summary = BrainSpikeSummary(
        total=len(items),
        highest_priority=highest,
        approval_required_count=sum(1 for spike in items if spike.risk_level == "approval_required"),
        event_types=list(dict.fromkeys(str(spike.event_type) for spike in items)),
        observe_only=all(spike.action_mode == "observe_only" for spike in items),
    )
    return summary.to_json_dict()
