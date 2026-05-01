from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import Field, field_validator

from assistant.contracts.base import ContractModel


BrainAutonomyMode = Literal[
    "observe_only",
    "suggest_only",
    "draft_only",
    "approval_required",
    "safe_direct_action",
    "blocked",
]
BrainPermissionLevel = Literal[
    "none",
    "observe",
    "suggest",
    "draft",
    "approval_required",
    "direct_safe",
    "blocked",
]


class BrainAutonomyState(ContractModel):
    """Side-effect-free executive control posture for Stark's brain.

    M-BrainAlive-7 decides what Stark is allowed to do next.  It deliberately
    does not execute tools, mutate files, persist memory, schedule background
    work, or bypass existing approval/runtime boundaries.
    """

    mode: BrainAutonomyMode = "observe_only"
    confidence: float = 0.0
    risk_score: float = 0.0
    permission_level: BrainPermissionLevel = "observe"
    allowed_actions: List[str] = Field(default_factory=lambda: ["observe_brain_state", "summarize_state"])
    blocked_actions: List[str] = Field(default_factory=list)
    requires_user_approval: bool = False
    approval_reason: Optional[str] = None
    inhibition_reason: Optional[str] = None
    next_safe_action: Optional[str] = None
    triggered_by: List[str] = Field(default_factory=list)

    @field_validator("confidence", "risk_score")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("autonomy scores must be in [0,1]")
        return score


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _clean(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        text = _clean(value)
        if text and text not in out:
            out.append(text)
    return out


def _score_for_caution(caution: str) -> float:
    return {"low": 0.05, "medium": 0.30, "high": 0.45}.get(str(caution or "").lower(), 0.10)


def _allowed_for_mode(mode: BrainAutonomyMode) -> List[str]:
    base = ["observe_brain_state", "summarize_state"]
    if mode == "observe_only":
        return base
    if mode == "suggest_only":
        return base + ["suggest_next_step", "surface_uncertainty"]
    if mode == "draft_only":
        return base + ["suggest_next_step", "draft_plan", "draft_patch", "draft_response"]
    if mode == "approval_required":
        return base + ["suggest_next_step", "draft_plan", "request_user_approval"]
    if mode == "safe_direct_action":
        return base + ["suggest_next_step", "format_internal_summary"]
    return base


def _blocked_for_mode(mode: BrainAutonomyMode) -> List[str]:
    always_blocked = [
        "execute_tool_without_user_request",
        "modify_files_without_user_request",
        "send_external_message",
        "persist_memory_without_policy",
        "schedule_background_work",
        "continue_task_in_background",
        "self_modify_without_explicit_scope",
    ]
    if mode in {"observe_only", "suggest_only", "draft_only", "approval_required"}:
        return always_blocked + ["safe_direct_action"]
    if mode == "blocked":
        return always_blocked + ["draft_patch", "draft_action", "safe_direct_action"]
    return always_blocked


def compute_autonomy_state(state: Any) -> BrainAutonomyState:
    """Compute Stark's current executive-control posture.

    The governor is conservative by default.  It selects permission for later
    surfaces to inspect, but it never performs the selected action.
    """

    fast_pass = state.fast_pass
    memory = state.memory
    reasoning = state.reasoning
    living = state.living_state
    instinct = state.instinct_state
    reflection = state.reflection_state
    goal_pressure = state.goal_pressure_state
    response_plan = state.response_plan
    memory_update = state.memory_update
    spikes = list(getattr(state, "brain_spikes", []) or [])

    triggered_by: List[str] = []
    risk_parts: List[float] = []

    caution_score = _score_for_caution(str(getattr(instinct, "caution_level", "low")))
    if caution_score >= 0.30:
        triggered_by.append("instinct_caution_detected")
    risk_parts.append(caution_score)

    if bool(getattr(reflection, "requires_user_approval", False)):
        risk_parts.append(0.35)
        triggered_by.append("reflection_requires_approval")
    if bool(getattr(instinct, "should_ask_confirmation", False)):
        risk_parts.append(0.25)
        triggered_by.append("instinct_confirmation_requested")
    if float(getattr(goal_pressure, "approval_wait_score", 0.0) or 0.0) >= 0.50:
        risk_parts.append(0.35)
        triggered_by.append("goal_approval_wait_detected")
    if float(getattr(goal_pressure, "blocked_score", 0.0) or 0.0) >= 0.60:
        risk_parts.append(0.30)
        triggered_by.append("goal_blocked_pressure_detected")
    if float(getattr(living, "risk_pressure", 0.0) or 0.0) >= 0.40:
        risk_parts.append(float(getattr(living, "risk_pressure", 0.0) or 0.0) * 0.35)
        triggered_by.append("living_risk_pressure_detected")
    if float(getattr(living, "stress_level", 0.0) or 0.0) >= 0.60:
        risk_parts.append(0.15)
        triggered_by.append("living_stress_detected")
    if list(getattr(memory, "conflicts", []) or []):
        risk_parts.append(0.25)
        triggered_by.append("memory_conflict_detected")
    if bool(getattr(fast_pass, "needs_tools", False)) or list(getattr(response_plan, "tool_actions", []) or []):
        risk_parts.append(0.20)
        triggered_by.append("tool_or_action_intent_detected")
    if bool(getattr(memory_update, "should_store", False)):
        risk_parts.append(0.10)
        triggered_by.append("memory_writeback_proposed")
    if list(getattr(reasoning, "safety_flags", []) or []):
        risk_parts.append(0.25)
        triggered_by.append("reasoning_safety_flags_detected")
    if float(getattr(reasoning, "uncertainty", 0.0) or 0.0) >= 0.55:
        risk_parts.append(0.12)
        triggered_by.append("reasoning_uncertainty_detected")

    spike_types = [str(getattr(spike, "event_type", "") or "") for spike in spikes]
    spike_risks = [str(getattr(spike, "risk_level", "") or "") for spike in spikes]
    spike_priorities = [str(getattr(spike, "priority", "") or "") for spike in spikes]
    if any(risk == "blocked" for risk in spike_risks):
        risk_parts.append(0.35)
        triggered_by.append("blocked_spike_detected")
    if any(risk == "approval_required" for risk in spike_risks):
        risk_parts.append(0.30)
        triggered_by.append("approval_spike_detected")
    if any(priority == "critical" for priority in spike_priorities):
        risk_parts.append(0.15)
        triggered_by.append("critical_spike_detected")

    risk_score = _clamp(sum(risk_parts))

    requires_user_approval = bool(
        risk_score >= 0.55
        or bool(getattr(reflection, "requires_user_approval", False))
        or bool(getattr(instinct, "should_ask_confirmation", False))
        or float(getattr(goal_pressure, "approval_wait_score", 0.0) or 0.0) >= 0.50
        or any(risk == "approval_required" for risk in spike_risks)
    )

    mode: BrainAutonomyMode = "observe_only"
    permission_level: BrainPermissionLevel = "observe"
    approval_reason: Optional[str] = None
    inhibition_reason: Optional[str] = None
    next_safe_action: Optional[str] = "Observe the current brain state and answer within the user-approved turn."

    if any(risk == "blocked" for risk in spike_risks) or str(getattr(instinct, "action_bias", "")) == "blocked" or risk_score >= 0.80:
        mode = "blocked"
        permission_level = "blocked"
        requires_user_approval = True
        inhibition_reason = "Blocked or very high-risk brain signals are active; do not continue without clarification."
        next_safe_action = "Clarify the blocker or ask the user what to do next."
        triggered_by.append("executive_inhibition_engaged")
    elif requires_user_approval:
        mode = "approval_required"
        permission_level = "approval_required"
        approval_reason = "Approval or confirmation pressure is active in the brain state."
        next_safe_action = "Ask the user for explicit approval before any action beyond drafting or explanation."
        triggered_by.append("approval_gate_engaged")
    elif float(getattr(goal_pressure, "pressure_score", 0.0) or 0.0) >= 0.60 and risk_score < 0.40:
        mode = "draft_only"
        permission_level = "draft"
        next_safe_action = getattr(goal_pressure, "next_goal_step", None) or "Draft the next plan or patch, but do not apply it automatically."
        triggered_by.append("draft_gate_from_goal_pressure")
    elif float(getattr(living, "curiosity_pressure", 0.0) or 0.0) >= 0.60 or str(getattr(instinct, "action_bias", "")) == "suggest_next_step":
        mode = "suggest_only"
        permission_level = "suggest"
        next_safe_action = "Offer one useful next step or probe without starting extra work automatically."
        triggered_by.append("suggest_gate_from_curiosity_or_instinct")
    else:
        mode = "suggest_only" if bool(getattr(instinct, "should_offer_next_step", False)) else "observe_only"
        permission_level = "suggest" if mode == "suggest_only" else "observe"
        triggered_by.append("conservative_default")

    # Keep direct action disabled in this milestone even if the mode exists in the contract.
    if mode == "safe_direct_action":
        mode = "suggest_only"
        permission_level = "suggest"
        inhibition_reason = "safe_direct_action is intentionally disabled for M-BrainAlive-7."
        triggered_by.append("direct_action_disabled_for_m7")

    confidence = _clamp(
        0.55
        + (0.12 if triggered_by else 0.0)
        + (0.10 if mode in {"approval_required", "blocked"} else 0.0)
        - (0.18 if float(getattr(reasoning, "uncertainty", 0.0) or 0.0) >= 0.70 else 0.0)
    )

    return BrainAutonomyState(
        mode=mode,
        confidence=confidence,
        risk_score=risk_score,
        permission_level=permission_level,
        allowed_actions=_allowed_for_mode(mode),
        blocked_actions=_blocked_for_mode(mode),
        requires_user_approval=requires_user_approval,
        approval_reason=approval_reason,
        inhibition_reason=inhibition_reason,
        next_safe_action=_clean(next_safe_action) or None,
        triggered_by=_unique(triggered_by + [f"spike:{item}" for item in spike_types[:6]])[:14],
    )
