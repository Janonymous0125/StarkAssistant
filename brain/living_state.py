from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, field_validator

from assistant.contracts.base import ContractModel


BrainMood = Literal["calm", "focused", "curious", "cautious", "strained", "alert", "satisfied"]
BrainEnergy = Literal["low", "normal", "high"]


class BrainLivingState(ContractModel):
    """Computed, side-effect-free living condition for Stark's brain.

    M-BrainAlive-2 makes Stark's internal condition visible without granting
    autonomy.  The state is derived from the current ThoughtState and queued
    observe-only spikes; it must not execute tools or mutate external systems.
    """

    mood: BrainMood = "calm"
    energy: BrainEnergy = "normal"
    attention_focus: Optional[str] = None
    stress_level: float = 0.0
    curiosity_pressure: float = 0.0
    active_goal_pressure: float = 0.0
    risk_pressure: float = 0.0
    last_shift_reason: Optional[str] = None
    signals: List[str] = Field(default_factory=list)

    @field_validator("stress_level", "curiosity_pressure", "active_goal_pressure", "risk_pressure")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("living-state scores must be in [0,1]")
        return score


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _contains_any(values: List[str], needles: set[str]) -> bool:
    return any(str(value or "") in needles for value in values)


def compute_living_state(state: Any) -> BrainLivingState:
    """Derive Stark's current living state from one completed ThoughtState.

    This function is intentionally deterministic and observe-only.  It turns
    existing brain signals into a compact condition that future HUD panels and
    instinct layers can inspect safely.
    """

    fast_pass = state.fast_pass
    goal_state = state.goal_state
    curiosity = state.curiosity
    memory = state.memory
    reasoning = state.reasoning
    spikes = list(getattr(state, "brain_spikes", []) or [])

    emotions = [str(tag) for tag in list(fast_pass.emotion_detected or [])]
    spike_types = [str(getattr(spike, "event_type", "") or "") for spike in spikes]
    approval_spikes = [spike for spike in spikes if str(getattr(spike, "risk_level", "") or "") == "approval_required"]
    high_spikes = [spike for spike in spikes if str(getattr(spike, "priority", "") or "") in {"high", "critical"}]

    stress = 0.0
    if fast_pass.urgency == "critical":
        stress += 0.55
    elif fast_pass.urgency == "high":
        stress += 0.35
    if _contains_any(emotions, {"frustrated", "angry", "scared", "stressed", "disappointed"}):
        stress += 0.30
    if memory.conflicts:
        stress += 0.20
    if approval_spikes:
        stress += 0.15
    if high_spikes:
        stress += 0.10
    stress = _clamp(stress)

    curiosity_pressure = _clamp(
        float(curiosity.curiosity_score or 0.0) * 0.70
        + float(curiosity.learning_value or 0.0) * 0.20
        + (0.10 if curiosity.should_think_now else 0.0)
    )

    active_goal_pressure = 0.0
    if goal_state.active_goal_ids:
        active_goal_pressure += 0.30
    if goal_state.ui_sync_required:
        active_goal_pressure += 0.20
    if goal_state.active_pm_task_status in {"blocked", "waiting_approval", "on_hold"}:
        active_goal_pressure += 0.25
    if goal_state.active_pm_task_requires_approval:
        active_goal_pressure += 0.25
    active_goal_pressure = _clamp(active_goal_pressure)

    risk_pressure = _clamp(
        (0.45 if approval_spikes else 0.0)
        + (0.20 if fast_pass.needs_tools else 0.0)
        + (0.20 if reasoning.safety_flags else 0.0)
        + (0.15 if memory.conflicts else 0.0)
    )

    if stress >= 0.70:
        mood: BrainMood = "alert"
    elif stress >= 0.45 or risk_pressure >= 0.50:
        mood = "strained"
    elif risk_pressure >= 0.25 or _contains_any(emotions, {"cautious", "uncertain"}):
        mood = "cautious"
    elif curiosity_pressure >= 0.60:
        mood = "curious"
    elif active_goal_pressure >= 0.35 or fast_pass.domain_guess in {"project", "goals", "memory"}:
        mood = "focused"
    elif _contains_any(emotions, {"satisfied", "excited", "confident"}):
        mood = "satisfied"
    else:
        mood = "calm"

    if fast_pass.urgency in {"high", "critical"} or stress >= 0.65 or active_goal_pressure >= 0.70:
        energy: BrainEnergy = "high"
    elif not state.input.normalized_text.strip() or (fast_pass.can_answer_directly and curiosity_pressure < 0.25 and active_goal_pressure < 0.25):
        energy = "low"
    else:
        energy = "normal"

    attention_focus = (
        goal_state.active_pm_task_title
        or goal_state.active_pm_project_title
        or goal_state.current_goal
        or state.meaning.user_goal
        or state.meaning.intent
        or fast_pass.intent_guess
    )

    signals: List[str] = []
    if emotions and emotions != ["neutral"]:
        signals.append("affect:" + ",".join(emotions[:4]))
    if spike_types:
        signals.append("spikes:" + ",".join(list(dict.fromkeys(spike_types))[:4]))
    if goal_state.active_goal_ids:
        signals.append("active_goal")
    if memory.conflicts:
        signals.append("memory_conflict")
    if curiosity_pressure >= 0.60:
        signals.append("curiosity_pressure")
    if risk_pressure >= 0.40:
        signals.append("risk_pressure")

    reason_parts: List[str] = []
    if stress >= 0.45:
        reason_parts.append("stress signals crossed threshold")
    if curiosity_pressure >= 0.60:
        reason_parts.append("curiosity pressure is elevated")
    if active_goal_pressure >= 0.35:
        reason_parts.append("active goal pressure is present")
    if risk_pressure >= 0.40:
        reason_parts.append("approval or safety risk is present")
    if not reason_parts:
        reason_parts.append("baseline turn signals are stable")

    return BrainLivingState(
        mood=mood,
        energy=energy,
        attention_focus=str(attention_focus or "").strip() or None,
        stress_level=stress,
        curiosity_pressure=curiosity_pressure,
        active_goal_pressure=active_goal_pressure,
        risk_pressure=risk_pressure,
        last_shift_reason="; ".join(reason_parts),
        signals=signals,
    )
