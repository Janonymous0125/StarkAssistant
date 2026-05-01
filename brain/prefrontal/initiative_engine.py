from __future__ import annotations

from pydantic import field_validator

from assistant.contracts.base import ContractModel

from .goal_generator import InternalGoal


class InitiativeDecision(ContractModel):
    schema_version: str = "initiative_decision.v1"
    goal_id: str
    decision: str
    authority_level: int
    allowed: bool
    requires_user_approval: bool
    reason: str
    required_backup: bool = False

    @field_validator("schema_version")
    @classmethod
    def _schema_version_supported(cls, value: str) -> str:
        if value != "initiative_decision.v1":
            raise ValueError("unsupported initiative decision schema_version")
        return value

    @field_validator("decision")
    @classmethod
    def _decision_supported(cls, value: str) -> str:
        text = str(value or "").strip()
        if text not in {"record", "analyze", "notify", "plan", "act", "ask", "block"}:
            raise ValueError("unsupported initiative decision")
        return text

    @field_validator("authority_level")
    @classmethod
    def _authority_level_supported(cls, value: int) -> int:
        level = int(value)
        if level < 0 or level > 7:
            raise ValueError("authority_level must be in [0,7]")
        return level


def decide_initiative(goal: InternalGoal) -> InitiativeDecision:
    authority = str(goal.authority_required or "").strip().lower()
    if authority in {"observe_only", "read_only_diagnosis"}:
        level = 2 if authority == "read_only_diagnosis" else 0
        return InitiativeDecision(
            goal_id=goal.goal_id,
            decision=("analyze" if level >= 2 else "record"),
            authority_level=level,
            allowed=True,
            requires_user_approval=False,
            reason="Read-only proactive analysis is allowed by the first supervised loop.",
        )
    return InitiativeDecision(
        goal_id=goal.goal_id,
        decision="ask",
        authority_level=6,
        allowed=False,
        requires_user_approval=True,
        reason="Goal requires authority above read-only proactive analysis.",
        required_backup=True,
    )
