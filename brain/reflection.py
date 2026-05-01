from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import Field

from assistant.contracts.base import ContractModel


BrainReflectionType = Literal[
    "none",
    "turn_review",
    "milestone_progress",
    "memory_candidate",
    "unresolved_followup",
    "risk_review",
]


class BrainReflectionState(ContractModel):
    """Side-effect-free reflection candidate for one completed brain turn.

    M-BrainAlive-4 lets Stark review what happened in a turn and propose what
    may need memory, follow-up, or user approval.  It is deliberately passive:
    this model must not persist memory, run tools, schedule work, or continue a
    task outside the normal user-approved runtime.
    """

    should_reflect: bool = False
    reflection_type: BrainReflectionType = "none"
    summary: Optional[str] = None
    learned_facts: List[str] = Field(default_factory=list)
    unresolved_items: List[str] = Field(default_factory=list)
    memory_candidates: List[str] = Field(default_factory=list)
    follow_up_candidates: List[str] = Field(default_factory=list)
    risk_notes: List[str] = Field(default_factory=list)
    next_best_step: Optional[str] = None
    requires_user_approval: bool = False
    triggered_by: List[str] = Field(default_factory=list)


def _clean(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def _bounded(text: Any, *, max_len: int = 220) -> str:
    value = _clean(text)
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "..."


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        text = _clean(value)
        if text and text not in out:
            out.append(text)
    return out


def compute_reflection_state(state: Any) -> BrainReflectionState:
    """Compute Stark's passive self-review candidate for a completed ThoughtState."""

    fast_pass = state.fast_pass
    meaning = state.meaning
    memory = state.memory
    goal_state = state.goal_state
    reasoning = state.reasoning
    curiosity = state.curiosity
    response_plan = state.response_plan
    memory_update = state.memory_update
    living = state.living_state
    instinct = state.instinct_state
    spikes = list(getattr(state, "brain_spikes", []) or [])

    spike_types = [str(getattr(spike, "event_type", "") or "") for spike in spikes]
    spike_priorities = [str(getattr(spike, "priority", "") or "") for spike in spikes]
    spike_risks = [str(getattr(spike, "risk_level", "") or "") for spike in spikes]

    triggered_by: List[str] = []
    learned_facts: List[str] = []
    unresolved_items: List[str] = []
    memory_candidates: List[str] = []
    follow_up_candidates: List[str] = []
    risk_notes: List[str] = []

    if bool(getattr(memory_update, "should_store", False)):
        triggered_by.append("memory_writeback_proposed")
        if getattr(memory_update, "summary_object", None) is not None:
            summary = _bounded(getattr(memory_update.summary_object, "summary", ""), max_len=180)
            if summary:
                memory_candidates.append(summary)
        else:
            goal = _clean(getattr(meaning, "user_goal", None) or getattr(meaning, "assistant_goal", None))
            if goal:
                memory_candidates.append(f"Potential memory from this turn: {goal}")

    if list(getattr(memory, "conflicts", []) or []):
        triggered_by.append("memory_conflict_detected")
        unresolved_items.extend([_bounded(item, max_len=160) for item in list(memory.conflicts or [])[:4]])
        risk_notes.append("Memory conflict remains unresolved; avoid treating the current interpretation as final.")


    if float(getattr(reasoning, "uncertainty", 0.0) or 0.0) >= 0.55:
        triggered_by.append("reasoning_uncertainty_detected")
        unresolved_items.append("Reasoning uncertainty is elevated; assumptions may need validation.")

    if list(getattr(reasoning, "safety_flags", []) or []):
        triggered_by.append("reasoning_safety_flags_detected")
        risk_notes.extend([f"Safety flag: {_bounded(flag, max_len=120)}" for flag in list(reasoning.safety_flags or [])[:4]])

    if float(getattr(curiosity, "curiosity_score", 0.0) or 0.0) >= 0.70 or float(getattr(living, "curiosity_pressure", 0.0) or 0.0) >= 0.60:
        triggered_by.append("curiosity_pressure_detected")
        if getattr(curiosity, "next_probe", None):
            follow_up_candidates.append(_bounded(curiosity.next_probe, max_len=180))
        elif list(getattr(curiosity, "questions", []) or []):
            follow_up_candidates.extend([_bounded(q, max_len=160) for q in list(curiosity.questions or [])[:3]])

    if float(getattr(living, "active_goal_pressure", 0.0) or 0.0) >= 0.35 or list(getattr(goal_state, "active_goal_ids", []) or []):
        triggered_by.append("active_goal_pressure_detected")
        goal_label = _clean(getattr(goal_state, "active_pm_task_title", None) or getattr(goal_state, "current_goal", None) or getattr(goal_state, "subgoal", None))
        if goal_label:
            follow_up_candidates.append(f"Continue focused progress on: {goal_label}")

    if bool(getattr(goal_state, "ui_sync_required", False)):
        triggered_by.append("goal_ui_sync_required")
        follow_up_candidates.append("Sync the visible goal/task state when the UI layer is ready to consume this signal.")

    if bool(getattr(instinct, "should_surface_uncertainty", False)):
        triggered_by.append("instinct_uncertainty_surface_requested")
        risk_notes.append("Instinct guidance recommends surfacing uncertainty in the response.")

    if bool(getattr(instinct, "should_ask_confirmation", False)):
        triggered_by.append("instinct_confirmation_requested")
        unresolved_items.append("User confirmation may be needed before any higher-risk action.")

    if bool(getattr(response_plan, "memory_writeback", False)):
        triggered_by.append("response_plan_memory_writeback")

    if any(priority in {"high", "critical"} for priority in spike_priorities):
        triggered_by.append("high_priority_spike_detected")

    if any(risk in {"approval_required", "blocked"} for risk in spike_risks):
        triggered_by.append("approval_or_blocked_spike_detected")
        risk_notes.append("A spike indicates approval-required or blocked posture; reflection remains proposal-only.")

    if _clean(getattr(meaning, "user_goal", None)):
        learned_facts.append(f"User goal this turn: {_bounded(meaning.user_goal, max_len=160)}")
    if _clean(getattr(meaning, "assistant_goal", None)):
        learned_facts.append(f"Assistant goal this turn: {_bounded(meaning.assistant_goal, max_len=160)}")

    should_reflect = bool(triggered_by or memory_candidates or unresolved_items or follow_up_candidates or risk_notes)
    if not should_reflect:
        return BrainReflectionState(
            should_reflect=False,
            reflection_type="none",
            summary="No elevated reflection pressure detected for this turn.",
            next_best_step=None,
            requires_user_approval=False,
            triggered_by=["baseline_stable"],
        )

    reflection_type: BrainReflectionType = "turn_review"
    if risk_notes or any(risk in {"approval_required", "blocked"} for risk in spike_risks):
        reflection_type = "risk_review"
    elif unresolved_items:
        reflection_type = "unresolved_followup"
    elif memory_candidates:
        reflection_type = "memory_candidate"
    elif "active_goal_pressure_detected" in triggered_by:
        reflection_type = "milestone_progress"

    next_best_step: Optional[str] = None
    if risk_notes:
        next_best_step = "Keep the next response bounded and require user approval before any risky action."
    elif unresolved_items:
        next_best_step = "Clarify or resolve the highest-impact unresolved item."
    elif follow_up_candidates:
        next_best_step = follow_up_candidates[0]
    elif memory_candidates:
        next_best_step = "Review whether the proposed memory candidate should be stored."

    requires_user_approval = bool(
        getattr(instinct, "should_ask_confirmation", False)
        or getattr(goal_state, "active_pm_task_requires_approval", False)
        or any(risk == "approval_required" for risk in spike_risks)
    )

    summary_parts: List[str] = []
    if learned_facts:
        summary_parts.append(learned_facts[0])
    if unresolved_items:
        summary_parts.append(f"Unresolved items: {len(unresolved_items)}.")
    if memory_candidates:
        summary_parts.append(f"Memory candidates: {len(memory_candidates)}.")
    if follow_up_candidates:
        summary_parts.append(f"Follow-up candidates: {len(follow_up_candidates)}.")
    if risk_notes:
        summary_parts.append(f"Risk notes: {len(risk_notes)}.")
    summary = " ".join(summary_parts) or "Reflection candidate generated for this turn."

    return BrainReflectionState(
        should_reflect=True,
        reflection_type=reflection_type,
        summary=_bounded(summary, max_len=260),
        learned_facts=_unique(learned_facts)[:6],
        unresolved_items=_unique(unresolved_items)[:6],
        memory_candidates=_unique(memory_candidates)[:6],
        follow_up_candidates=_unique(follow_up_candidates)[:6],
        risk_notes=_unique(risk_notes)[:6],
        next_best_step=next_best_step,
        requires_user_approval=requires_user_approval,
        triggered_by=_unique(triggered_by + [f"spike:{t}" for t in spike_types[:6]]),
    )
