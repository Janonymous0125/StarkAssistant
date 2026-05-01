from __future__ import annotations

from typing import Any, List, Literal

from pydantic import Field

from assistant.contracts.base import ContractModel


BrainToneBias = Literal[
    "neutral",
    "calm_precise",
    "focused_direct",
    "curious_helpful",
    "cautious_explicit",
    "supportive_recovery",
]
BrainCautionLevel = Literal["low", "medium", "high"]
BrainActionBias = Literal[
    "answer_directly",
    "suggest_next_step",
    "ask_confirmation",
    "approval_required",
    "blocked",
]


class BrainInstinctState(ContractModel):
    """Safe, side-effect-free reflex guidance for Stark's brain.

    M-BrainAlive-3 converts observe-only spikes and living-state signals into
    response guidance.  It must never execute tools, mutate files, schedule
    work, or bypass the existing approval/runtime seams.
    """

    tone_bias: BrainToneBias = "neutral"
    caution_level: BrainCautionLevel = "low"
    action_bias: BrainActionBias = "answer_directly"
    should_slow_down: bool = False
    should_ask_confirmation: bool = False
    should_surface_uncertainty: bool = False
    should_offer_next_step: bool = False
    guidance_notes: List[str] = Field(default_factory=list)
    triggered_by: List[str] = Field(default_factory=list)


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def compute_instinct_state(state: Any) -> BrainInstinctState:
    """Derive Stark's safe instinct guidance from one completed ThoughtState.

    The result is guidance only.  It intentionally performs no actions and does
    not modify the response plan; downstream surfaces can inspect it when they
    want to explain Stark's current reflex posture.
    """

    fast_pass = state.fast_pass
    memory = state.memory
    reasoning = state.reasoning
    curiosity = state.curiosity
    goal_state = state.goal_state
    living = state.living_state
    spikes = list(getattr(state, "brain_spikes", []) or [])

    emotions = [str(tag) for tag in list(fast_pass.emotion_detected or [])]
    spike_types = [str(getattr(spike, "event_type", "") or "") for spike in spikes]
    approval_spikes = [spike for spike in spikes if str(getattr(spike, "risk_level", "") or "") == "approval_required"]
    blocked_spikes = [spike for spike in spikes if str(getattr(spike, "risk_level", "") or "") == "blocked"]
    critical_spikes = [spike for spike in spikes if str(getattr(spike, "priority", "") or "") == "critical"]

    notes: List[str] = []
    triggered_by: List[str] = []

    tone_bias: BrainToneBias = "neutral"
    caution_level: BrainCautionLevel = "low"
    action_bias: BrainActionBias = "answer_directly"
    should_slow_down = False
    should_ask_confirmation = False
    should_surface_uncertainty = False
    should_offer_next_step = False

    negative_affect = {"frustrated", "angry", "scared", "stressed", "disappointed"}
    if any(tag in negative_affect for tag in emotions):
        tone_bias = "supportive_recovery"
        caution_level = "medium"
        should_slow_down = True
        should_offer_next_step = True
        notes.append("User affect suggests recovery support; stay calm, practical, and concise.")
        triggered_by.append("affect_pressure_detected")

    if str(living.mood) in {"strained", "alert"} or float(living.stress_level) >= 0.45:
        tone_bias = "calm_precise" if tone_bias == "neutral" else tone_bias
        caution_level = "high" if float(living.stress_level) >= 0.70 else "medium"
        should_slow_down = True
        should_surface_uncertainty = True
        notes.append("Living state is strained or alert; reduce overconfidence and surface uncertainty.")
        triggered_by.append(f"living_state:{living.mood}")

    if approval_spikes or bool(getattr(goal_state, "active_pm_task_requires_approval", False)):
        caution_level = "high"
        action_bias = "approval_required"
        should_ask_confirmation = True
        should_surface_uncertainty = True
        notes.append("Approval pressure detected; do not imply direct action without explicit approval.")
        triggered_by.append("approval_pressure_detected")

    if blocked_spikes or critical_spikes or float(living.risk_pressure) >= 0.70:
        caution_level = "high"
        action_bias = "blocked" if blocked_spikes else action_bias
        should_ask_confirmation = True
        should_surface_uncertainty = True
        notes.append("High risk or blocked signal detected; keep the response bounded and explicit.")
        triggered_by.append("risk_pressure_detected")

    if memory.conflicts:
        caution_level = "high" if caution_level == "medium" else caution_level
        should_surface_uncertainty = True
        notes.append("Memory conflict detected; state the conflict instead of pretending certainty.")
        triggered_by.append("memory_conflict_detected")

    if float(reasoning.uncertainty) >= 0.55 or reasoning.safety_flags:
        caution_level = "medium" if caution_level == "low" else caution_level
        should_surface_uncertainty = True
        notes.append("Reasoning uncertainty or safety flags are present; make assumptions visible.")
        triggered_by.append("reasoning_uncertainty_detected")

    if fast_pass.needs_tools:
        if action_bias == "answer_directly":
            action_bias = "ask_confirmation"
        should_ask_confirmation = True
        notes.append("Tool need detected; route through normal permission and runtime boundaries.")
        triggered_by.append("tool_intent_detected")

    if float(living.curiosity_pressure) >= 0.60 or float(curiosity.curiosity_score) >= 0.70:
        tone_bias = "curious_helpful" if tone_bias == "neutral" else tone_bias
        if action_bias == "answer_directly":
            action_bias = "suggest_next_step"
        should_offer_next_step = True
        notes.append("Curiosity pressure is elevated; offer one useful next probe without starting extra work.")
        triggered_by.append("curiosity_pressure_detected")

    if float(living.active_goal_pressure) >= 0.35 or goal_state.active_goal_ids:
        tone_bias = "focused_direct" if tone_bias == "neutral" else tone_bias
        if action_bias == "answer_directly":
            action_bias = "suggest_next_step"
        should_offer_next_step = True
        notes.append("Active goal pressure is present; keep the response focused on the current milestone.")
        triggered_by.append("active_goal_pressure_detected")

    if not notes:
        notes.append("No elevated instinct pressure detected; normal direct response is acceptable.")
        triggered_by.append("baseline_stable")

    if "tool_intent_detected" in triggered_by and action_bias == "ask_confirmation" and caution_level == "low":
        caution_level = "medium"

    if not should_offer_next_step and action_bias in {"suggest_next_step", "ask_confirmation", "approval_required"}:
        should_offer_next_step = True

    return BrainInstinctState(
        tone_bias=tone_bias,
        caution_level=caution_level,
        action_bias=action_bias,
        should_slow_down=should_slow_down,
        should_ask_confirmation=should_ask_confirmation,
        should_surface_uncertainty=should_surface_uncertainty,
        should_offer_next_step=should_offer_next_step,
        guidance_notes=_unique(notes),
        triggered_by=_unique(triggered_by + [f"spike:{t}" for t in spike_types[:6]]),
    )
