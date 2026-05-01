from __future__ import annotations

from typing import List, Sequence

from pydantic import Field

from assistant.contracts.base import ContractModel
from assistant.brain.brainstem.events import BrainObservationEvent
from assistant.brain.limbic.salience import score_observation


class AttentionDecision(ContractModel):
    schema_version: str = "attention_decision.v1"
    focus_events: List[BrainObservationEvent] = Field(default_factory=list)
    ignored_events: List[BrainObservationEvent] = Field(default_factory=list)
    reason: str
    max_salience: float = 0.0
    scored_events: List[dict] = Field(default_factory=list)


def route_attention(events: Sequence[BrainObservationEvent], *, max_focus: int = 3) -> AttentionDecision:
    scored = sorted(
        [(score_observation(event), event) for event in list(events or [])],
        key=lambda item: item[0].score,
        reverse=True,
    )
    focus_count = max(0, min(int(max_focus or 0), len(scored)))
    focus = [event for score, event in scored[:focus_count] if score.score >= 0.25 or event.requires_attention]
    ignored = [event for _, event in scored if event not in focus]
    max_salience = scored[0][0].score if scored else 0.0
    reason = "selected_highest_salience_events_with_reasons" if focus else "no_observations_exceeded_attention_threshold"
    scored_events = [
        {"event_id": event.event_id, "score": salience.score, "reasons": list(salience.reasons)}
        for salience, event in scored
    ]
    return AttentionDecision(focus_events=focus, ignored_events=ignored, reason=reason, max_salience=max_salience, scored_events=scored_events)
