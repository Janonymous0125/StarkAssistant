from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from assistant.contracts.base import ContractModel
from assistant.brain.contracts import ThoughtState, thought_state_summary
from assistant.brain.spikes import spike_summary


BrainStateViewStatus = Literal[
    "idle",
    "observing",
    "focused",
    "reflecting",
    "cautious",
    "approval_required",
    "blocked",
]


class BrainStateView(ContractModel):
    """Read-only brain state view for future HUD/debug wiring.

    M-BrainAlive-5 exposes Stark's live brain posture as a compact contract.
    It intentionally performs no HUD writes, tool execution, persistence,
    scheduling, or other side effects.
    """

    schema_version: str = "brain_state_view.m1"
    turn_id: str
    timestamp: str
    status: BrainStateViewStatus = "observing"
    title: str = "Stark Brain State"
    attention_focus: Optional[str] = None
    mood: str = "calm"
    energy: str = "normal"
    stress_level: float = 0.0
    curiosity_pressure: float = 0.0
    active_goal_pressure: float = 0.0
    risk_pressure: float = 0.0
    spike_count: int = 0
    highest_spike_priority: str = "low"
    spike_event_types: List[str] = Field(default_factory=list)
    instinct_tone_bias: str = "neutral"
    instinct_caution_level: str = "low"
    instinct_action_bias: str = "answer_directly"
    instinct_should_ask_confirmation: bool = False
    instinct_should_surface_uncertainty: bool = False
    instinct_should_offer_next_step: bool = False
    reflection_should_reflect: bool = False
    reflection_type: str = "none"
    reflection_summary: Optional[str] = None
    reflection_next_best_step: Optional[str] = None
    reflection_requires_user_approval: bool = False
    memory_gate_should_retrieve: bool = False
    retrieved_memory_count: int = 0
    writeback_should_store: bool = False
    active_goal_ids: List[str] = Field(default_factory=list)
    active_pm_task_title: Optional[str] = None
    active_pm_task_status: Optional[str] = None
    goal_pressure_score: float = 0.0
    goal_pressure_status: str = "none"
    goal_pressure_next_goal_step: Optional[str] = None
    goal_pressure_should_resume_later: bool = False
    goal_pressure_should_ask_user_to_continue: bool = False
    autonomy_mode: str = "observe_only"
    autonomy_permission_level: str = "observe"
    autonomy_risk_score: float = 0.0
    autonomy_requires_user_approval: bool = False
    autonomy_next_safe_action: Optional[str] = None
    autonomy_approval_reason: Optional[str] = None
    autonomy_inhibition_reason: Optional[str] = None
    current_signals: List[str] = Field(default_factory=list)
    guidance_notes: List[str] = Field(default_factory=list)
    risk_notes: List[str] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


def _status_for_state(state: ThoughtState, summary: Dict[str, Any]) -> BrainStateViewStatus:
    if str(summary.get("autonomy_mode") or "") == "blocked":
        return "blocked"
    if bool(summary.get("autonomy_requires_user_approval")) or bool(summary.get("goal_pressure_should_ask_user_to_continue")) or bool(summary.get("reflection_requires_user_approval")) or bool(getattr(state.instinct_state, "should_ask_confirmation", False)):
        return "approval_required"
    if float(summary.get("living_risk_pressure") or 0.0) >= 0.40 or str(summary.get("instinct_caution_level") or "") in {"medium", "high"}:
        return "cautious"
    if bool(summary.get("reflection_should_reflect")):
        return "reflecting"
    if float(summary.get("goal_pressure_score") or 0.0) >= 0.35 or float(summary.get("living_active_goal_pressure") or 0.0) >= 0.35 or list(summary.get("active_goal_ids") or []):
        return "focused"
    if int(summary.get("brain_spike_count") or 0) > 0:
        return "observing"
    return "idle"


def build_brain_state_view(state: ThoughtState | Dict[str, Any] | None) -> Dict[str, Any]:
    """Build a compact, read-only brain view for future UI/API consumers.

    This helper is intentionally side-effect-free.  It returns a JSON-safe dict
    that a later HUD rewrite can expose through an endpoint without importing
    the full ThoughtState shape.
    """

    if state is None:
        return {}
    model = state if isinstance(state, ThoughtState) else ThoughtState.model_validate(dict(state or {}))
    summary = thought_state_summary(model)
    spikes = spike_summary(model.brain_spikes)
    reflection = model.reflection_state
    view = BrainStateView(
        turn_id=str(model.turn_id),
        timestamp=str(model.timestamp),
        status=_status_for_state(model, summary),
        attention_focus=model.living_state.attention_focus,
        mood=str(model.living_state.mood),
        energy=str(model.living_state.energy),
        stress_level=float(model.living_state.stress_level),
        curiosity_pressure=float(model.living_state.curiosity_pressure),
        active_goal_pressure=float(model.living_state.active_goal_pressure),
        risk_pressure=float(model.living_state.risk_pressure),
        spike_count=int(spikes.get("total", 0)),
        highest_spike_priority=str(spikes.get("highest_priority", "low")),
        spike_event_types=list(spikes.get("event_types", [])),
        instinct_tone_bias=str(model.instinct_state.tone_bias),
        instinct_caution_level=str(model.instinct_state.caution_level),
        instinct_action_bias=str(model.instinct_state.action_bias),
        instinct_should_ask_confirmation=bool(model.instinct_state.should_ask_confirmation),
        instinct_should_surface_uncertainty=bool(model.instinct_state.should_surface_uncertainty),
        instinct_should_offer_next_step=bool(model.instinct_state.should_offer_next_step),
        reflection_should_reflect=bool(reflection.should_reflect),
        reflection_type=str(reflection.reflection_type),
        reflection_summary=reflection.summary,
        reflection_next_best_step=reflection.next_best_step,
        reflection_requires_user_approval=bool(reflection.requires_user_approval),
        memory_gate_should_retrieve=bool(model.memory_gate.should_retrieve),
        retrieved_memory_count=len(model.memory.retrieved or []),
        writeback_should_store=bool(model.memory_update.should_store),
        active_goal_ids=list(model.goal_state.active_goal_ids or []),
        active_pm_task_title=model.goal_state.active_pm_task_title,
        active_pm_task_status=model.goal_state.active_pm_task_status,
        goal_pressure_score=float(model.goal_pressure_state.pressure_score),
        goal_pressure_status=str(model.goal_pressure_state.active_goal_status),
        goal_pressure_next_goal_step=model.goal_pressure_state.next_goal_step,
        goal_pressure_should_resume_later=bool(model.goal_pressure_state.should_resume_later),
        goal_pressure_should_ask_user_to_continue=bool(model.goal_pressure_state.should_ask_user_to_continue),
        autonomy_mode=str(model.autonomy_state.mode),
        autonomy_permission_level=str(model.autonomy_state.permission_level),
        autonomy_risk_score=float(model.autonomy_state.risk_score),
        autonomy_requires_user_approval=bool(model.autonomy_state.requires_user_approval),
        autonomy_next_safe_action=model.autonomy_state.next_safe_action,
        autonomy_approval_reason=model.autonomy_state.approval_reason,
        autonomy_inhibition_reason=model.autonomy_state.inhibition_reason,
        current_signals=list(model.living_state.signals or []),
        guidance_notes=list(model.instinct_state.guidance_notes or [])[:6],
        risk_notes=list(reflection.risk_notes or [])[:6],
        summary=summary,
    )
    return view.to_json_dict()
