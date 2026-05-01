from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import Field, field_validator

from assistant.contracts.base import ContractModel


BrainGoalPressureStatus = Literal[
    "none",
    "active",
    "blocked",
    "waiting_approval",
    "on_hold",
    "resumable",
]


class BrainGoalPressureState(ContractModel):
    """Computed, side-effect-free goal pressure for Stark's brain.

    M-BrainAlive-6 lets Stark recognize unfinished work, blocked work,
    approval waits, and safe resume pressure.  It deliberately does not run
    tools, persist memory, schedule work, or continue tasks on its own.
    """

    has_active_goal: bool = False
    active_goal_title: Optional[str] = None
    active_goal_status: BrainGoalPressureStatus = "none"
    pressure_score: float = 0.0
    urgency_score: float = 0.0
    blocked_score: float = 0.0
    resume_score: float = 0.0
    approval_wait_score: float = 0.0
    unfinished_reason: Optional[str] = None
    should_resume_later: bool = False
    should_ask_user_to_continue: bool = False
    next_goal_step: Optional[str] = None
    risk_notes: List[str] = Field(default_factory=list)
    triggered_by: List[str] = Field(default_factory=list)

    @field_validator("pressure_score", "urgency_score", "blocked_score", "resume_score", "approval_wait_score")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("goal-pressure scores must be in [0,1]")
        return score


def _clean(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def _bounded(text: Any, *, max_len: int = 220) -> str:
    value = _clean(text)
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "..."


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        text = _clean(value)
        if text and text not in out:
            out.append(text)
    return out


def _status_text(value: Any) -> str:
    return _clean(value).lower().replace(" ", "_")


def _goal_title(state: Any) -> Optional[str]:
    goal_state = state.goal_state
    meaning = state.meaning
    title = (
        getattr(goal_state, "active_pm_task_title", None)
        or getattr(goal_state, "active_pm_project_title", None)
        or getattr(goal_state, "current_goal", None)
        or getattr(goal_state, "subgoal", None)
        or getattr(meaning, "user_goal", None)
        or getattr(meaning, "assistant_goal", None)
    )
    return _bounded(title, max_len=180) or None


def compute_goal_pressure_state(state: Any) -> BrainGoalPressureState:
    """Compute passive pressure around active, blocked, or resumable goals."""

    fast_pass = state.fast_pass
    meaning = state.meaning
    memory = state.memory
    goal_state = state.goal_state
    curiosity = state.curiosity
    living = state.living_state
    instinct = state.instinct_state
    reflection = state.reflection_state
    memory_update = state.memory_update
    spikes = list(getattr(state, "brain_spikes", []) or [])

    spike_types = [str(getattr(spike, "event_type", "") or "") for spike in spikes]
    spike_risks = [str(getattr(spike, "risk_level", "") or "") for spike in spikes]
    spike_priorities = [str(getattr(spike, "priority", "") or "") for spike in spikes]

    triggered_by: List[str] = []
    risk_notes: List[str] = []

    has_active_goal = bool(
        list(getattr(goal_state, "active_goal_ids", []) or [])
        or _clean(getattr(goal_state, "current_goal", None))
        or _clean(getattr(goal_state, "subgoal", None))
        or _clean(getattr(goal_state, "active_pm_project_title", None))
        or _clean(getattr(goal_state, "active_pm_task_title", None))
        or _clean(getattr(meaning, "user_goal", None))
        or _clean(getattr(meaning, "assistant_goal", None))
    )
    if has_active_goal:
        triggered_by.append("active_goal_detected")

    status_sources = {
        _status_text(getattr(goal_state, "status", None)),
        _status_text(getattr(goal_state, "active_pm_task_status", None)),
    }
    status_sources.discard("")

    blocked_score = 0.0
    if status_sources & {"blocked", "stuck", "failed", "error"}:
        blocked_score += 0.45
        triggered_by.append("blocked_status_detected")
    if list(getattr(memory, "conflicts", []) or []):
        blocked_score += 0.20
        triggered_by.append("memory_conflict_detected")
    if list(getattr(reflection, "unresolved_items", []) or []):
        blocked_score += 0.20
        triggered_by.append("reflection_unresolved_detected")
    if any("blocked" in item or "unresolved" in item for item in spike_types) or any(risk == "blocked" for risk in spike_risks):
        blocked_score += 0.20
        triggered_by.append("blocked_or_unresolved_spike_detected")
    blocked_score = _clamp(blocked_score)

    approval_wait_score = 0.0
    if bool(getattr(goal_state, "active_pm_task_requires_approval", False)):
        approval_wait_score += 0.45
        triggered_by.append("goal_approval_required")
    if bool(getattr(instinct, "should_ask_confirmation", False)):
        approval_wait_score += 0.25
        triggered_by.append("instinct_confirmation_requested")
    if bool(getattr(reflection, "requires_user_approval", False)):
        approval_wait_score += 0.25
        triggered_by.append("reflection_approval_required")
    if any(risk == "approval_required" for risk in spike_risks) or any("approval" in item for item in spike_types):
        approval_wait_score += 0.25
        triggered_by.append("approval_spike_detected")
    approval_wait_score = _clamp(approval_wait_score)

    urgency_score = 0.0
    if str(getattr(fast_pass, "urgency", "normal")) == "critical":
        urgency_score += 0.65
    elif str(getattr(fast_pass, "urgency", "normal")) == "high":
        urgency_score += 0.40
    if any(priority == "critical" for priority in spike_priorities):
        urgency_score += 0.30
    elif any(priority == "high" for priority in spike_priorities):
        urgency_score += 0.18
    if float(getattr(living, "stress_level", 0.0) or 0.0) >= 0.55:
        urgency_score += 0.15
    urgency_score = _clamp(urgency_score)
    if urgency_score > 0.0:
        triggered_by.append("urgency_pressure_detected")

    resume_score = 0.0
    if list(getattr(reflection, "follow_up_candidates", []) or []):
        resume_score += 0.30
        triggered_by.append("reflection_follow_up_detected")
    if getattr(reflection, "next_best_step", None):
        resume_score += 0.22
        triggered_by.append("reflection_next_step_detected")
    if getattr(curiosity, "next_probe", None):
        resume_score += 0.16
        triggered_by.append("curiosity_probe_detected")
    if bool(getattr(memory_update, "should_store", False)):
        resume_score += 0.12
        triggered_by.append("memory_candidate_detected")
    if has_active_goal and blocked_score < 0.50 and approval_wait_score < 0.50:
        resume_score += 0.20
    resume_score = _clamp(resume_score)

    pressure_score = _clamp(
        (0.25 if has_active_goal else 0.0)
        + urgency_score * 0.20
        + blocked_score * 0.22
        + resume_score * 0.25
        + approval_wait_score * 0.20
        + float(getattr(living, "active_goal_pressure", 0.0) or 0.0) * 0.18
    )

    active_goal_status: BrainGoalPressureStatus = "none"
    if approval_wait_score >= 0.50:
        active_goal_status = "waiting_approval"
    elif blocked_score >= 0.50:
        active_goal_status = "blocked"
    elif status_sources & {"on_hold", "paused", "waiting"}:
        active_goal_status = "on_hold"
    elif resume_score >= 0.45:
        active_goal_status = "resumable"
    elif has_active_goal:
        active_goal_status = "active"

    unfinished_reason: Optional[str] = None
    if active_goal_status == "waiting_approval":
        unfinished_reason = "The active goal appears to be waiting for user approval before any higher-risk continuation."
        risk_notes.append("Do not continue the goal automatically while approval pressure is active.")
    elif active_goal_status == "blocked":
        unfinished_reason = "The active goal has blocked or unresolved signals that should be clarified before continuing."
        risk_notes.append("Blocked goal pressure should remain proposal-only until the blocker is resolved.")
    elif active_goal_status == "resumable":
        unfinished_reason = "The active goal has follow-up or next-step signals that may be worth resuming later."
    elif active_goal_status == "active":
        unfinished_reason = "An active goal is present, but no blocking or approval pressure dominates."

    next_goal_step: Optional[str] = None
    if active_goal_status == "waiting_approval":
        next_goal_step = "Ask the user for approval before continuing the active goal."
    elif active_goal_status == "blocked":
        if list(getattr(reflection, "unresolved_items", []) or []):
            next_goal_step = _bounded(reflection.unresolved_items[0], max_len=180)
        else:
            next_goal_step = "Clarify the blocker before continuing the active goal."
    elif getattr(reflection, "next_best_step", None):
        next_goal_step = _bounded(reflection.next_best_step, max_len=180)
    elif list(getattr(reflection, "follow_up_candidates", []) or []):
        next_goal_step = _bounded(reflection.follow_up_candidates[0], max_len=180)
    elif getattr(curiosity, "next_probe", None):
        next_goal_step = _bounded(curiosity.next_probe, max_len=180)
    elif has_active_goal:
        next_goal_step = "Continue the current goal only inside the next user-approved turn."

    should_resume_later = bool(
        has_active_goal
        and resume_score >= 0.45
        and blocked_score < 0.70
        and approval_wait_score < 0.70
    )
    should_ask_user_to_continue = bool(
        has_active_goal
        and (
            approval_wait_score >= 0.50
            or blocked_score >= 0.60
            or (resume_score >= 0.65 and urgency_score >= 0.35)
        )
    )

    return BrainGoalPressureState(
        has_active_goal=has_active_goal,
        active_goal_title=_goal_title(state),
        active_goal_status=active_goal_status,
        pressure_score=pressure_score,
        urgency_score=urgency_score,
        blocked_score=blocked_score,
        resume_score=resume_score,
        approval_wait_score=approval_wait_score,
        unfinished_reason=unfinished_reason,
        should_resume_later=should_resume_later,
        should_ask_user_to_continue=should_ask_user_to_continue,
        next_goal_step=next_goal_step,
        risk_notes=_unique(risk_notes)[:6],
        triggered_by=_unique(triggered_by + [f"spike:{item}" for item in spike_types[:6]])[:12],
    )
