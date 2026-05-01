from __future__ import annotations

from typing import List

from pydantic import Field

from assistant.contracts.base import ContractModel
from assistant.brain.brainstem.events import BrainObservationEvent


class SalienceScore(ContractModel):
    schema_version: str = "salience_score.v1"
    score: float
    reasons: List[str] = Field(default_factory=list)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def score_observation(
    event: BrainObservationEvent,
    *,
    user_priority: float = 0.0,
    project_relevance: float = 0.0,
    memory_match_strength: float = 0.0,
) -> SalienceScore:
    urgency = 1.0 if event.requires_attention else 0.0
    repeated_failure = 1.0 if event.kind == "repeated_failure" else 0.0
    components = {
        "importance": (_clamp(event.importance), 0.22),
        "risk": (_clamp(event.risk), 0.22),
        "novelty": (_clamp(event.novelty), 0.12),
        "urgency": (urgency, 0.14),
        "repeated_failure": (repeated_failure, 0.14),
        "project_relevance": (_clamp(project_relevance), 0.08),
        "memory_match_strength": (_clamp(memory_match_strength), 0.05),
        "user_priority": (_clamp(user_priority), 0.03),
    }
    score = _clamp(sum(value * weight for value, weight in components.values()))
    reasons = [name for name, (value, _) in components.items() if value >= 0.35]
    if not reasons and score > 0:
        reasons.append("low_signal")
    return SalienceScore(score=score, reasons=reasons)
