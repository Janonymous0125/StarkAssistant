from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, field_validator

from assistant.contracts.base import ContractModel
from assistant.contracts.memory import MemoryKind
from assistant.brain.autonomy import BrainAutonomyState
from assistant.brain.goal_pressure import BrainGoalPressureState
from assistant.brain.instincts import BrainInstinctState
from assistant.brain.living_state import BrainLivingState
from assistant.brain.reflection import BrainReflectionState
from assistant.brain.spikes import BrainSpikeEvent, spike_summary


AffectTag = Literal[
    "neutral",
    "curious",
    "confident",
    "uncertain",
    "important",
    "urgent",
    "unresolved",
    "frustrated",
    "scared",
    "angry",
    "stressed",
    "excited",
    "cautious",
    "satisfied",
    "disappointed",
]

AFFECT_TAGS: tuple[str, ...] = (
    "neutral",
    "curious",
    "confident",
    "uncertain",
    "important",
    "urgent",
    "unresolved",
    "frustrated",
    "scared",
    "angry",
    "stressed",
    "excited",
    "cautious",
    "satisfied",
    "disappointed",
)


class BrainInputState(ContractModel):
    modalities: List[str] = Field(default_factory=lambda: ["text"])
    raw_text: str
    normalized_text: str
    source_refs: List[str] = Field(default_factory=list)
    source_metadata: Dict[str, Any] = Field(default_factory=dict)
    voice: Optional[Dict[str, Any]] = None
    image: Optional[Dict[str, Any]] = None
    file: Optional[Dict[str, Any]] = None

    @field_validator("modalities", "source_refs")
    @classmethod
    def _clean_string_list(cls, value: List[str]) -> List[str]:
        out: List[str] = []
        for raw in list(value or []):
            item = str(raw or "").strip()
            if item and item not in out:
                out.append(item)
        return out


class FastPassState(ContractModel):
    intent_guess: str
    domain_guess: str
    urgency: Literal["low", "normal", "high", "critical"] = "normal"
    emotion_detected: List[AffectTag] = Field(default_factory=lambda: ["neutral"])
    needs_memory: bool = False
    needs_tools: bool = False
    can_answer_directly: bool = False
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("confidence must be in [0,1]")
        return score


class MeaningState(ContractModel):
    intent: str
    entities: List[str] = Field(default_factory=list)
    relationships: List[Dict[str, Any]] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    references: List[str] = Field(default_factory=list)
    ambiguities: List[str] = Field(default_factory=list)
    user_goal: Optional[str] = None
    assistant_goal: Optional[str] = None


class MemoryGateState(ContractModel):
    should_retrieve: bool
    memory_types: List[MemoryKind] = Field(default_factory=list)
    reason: str


class SummarizedMemoryObject(ContractModel):
    memory_id: str
    memory_type: MemoryKind
    summary: str
    source_kind: str
    source_refs: List[str] = Field(default_factory=list)
    importance: float = 0.0
    emotion_tags: List[AffectTag] = Field(default_factory=lambda: ["neutral"])
    created_at: str
    updated_at: str
    related_goal_ids: List[str] = Field(default_factory=list)
    relevance_score: float = 0.0
    unresolved: bool = False
    expiry_policy: str = "retain_until_superseded"

    @field_validator("importance", "relevance_score")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("score must be in [0,1]")
        return score


class BrainMemoryState(ContractModel):
    retrieved: List[SummarizedMemoryObject] = Field(default_factory=list)
    active_summary: Optional[str] = None
    conflicts: List[str] = Field(default_factory=list)
    relevance_score: float = 0.0
    retrieval_reason: Optional[str] = None
    source_breakdown: Dict[str, int] = Field(default_factory=dict)
    usage_hints: List[str] = Field(default_factory=list)
    obsidian_status: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("relevance_score")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("relevance_score must be in [0,1]")
        return score


class BrainGoalState(ContractModel):
    active_goal_ids: List[str] = Field(default_factory=list)
    current_goal: Optional[str] = None
    subgoal: Optional[str] = None
    status: Optional[str] = None
    ui_sync_required: bool = False
    goal_manager_authority: Optional[str] = None
    active_pm_project_goal_id: Optional[str] = None
    active_pm_project_title: Optional[str] = None
    active_pm_task_goal_id: Optional[str] = None
    active_pm_task_title: Optional[str] = None
    active_pm_task_status: Optional[str] = None
    brain_spike_count: int = 0
    highest_brain_spike_priority: str = "low"
    brain_spike_event_types: List[str] = Field(default_factory=list)
    living_mood: str = "calm"
    living_energy: str = "normal"
    living_attention_focus: Optional[str] = None
    living_stress_level: float = 0.0
    living_curiosity_pressure: float = 0.0
    living_active_goal_pressure: float = 0.0
    living_risk_pressure: float = 0.0
    active_pm_task_requires_approval: bool = False


class BrainReasoningState(ContractModel):
    mode: Literal["fast", "slow"]
    problem_type: str
    options: List[str] = Field(default_factory=list)
    chosen_strategy: str
    uncertainty: float = 0.0
    safety_flags: List[str] = Field(default_factory=list)

    @field_validator("uncertainty")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("uncertainty must be in [0,1]")
        return score


class BrainCuriosityState(ContractModel):
    curiosity_score: float = 0.0
    triggers: List[str] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)
    think_mode: Literal["none", "quick", "deep_deferred"] = "none"
    should_think_now: bool = False
    learning_value: float = 0.0
    user_relevance: float = 0.0
    next_probe: Optional[str] = None
    defer_reason: Optional[str] = None
    safety_limits: List[str] = Field(default_factory=list)
    reflection_seed: Optional[str] = None

    @field_validator("curiosity_score", "learning_value", "user_relevance")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("curiosity scores must be in [0,1]")
        return score


class BrainResponsePlan(ContractModel):
    target_outcome: str
    tone: str
    depth: str
    format: str
    ask_question: bool = False
    tool_actions: List[Dict[str, Any]] = Field(default_factory=list)
    memory_writeback: bool = False


class BrainActionDecision(ContractModel):
    """Brain-owned recommendation for what Stark should do next."""

    next_action: Literal[
        "answer_directly",
        "ask_user",
        "retrieve_more_context",
        "create_plan",
        "call_tool",
        "propose_memory",
        "defer_or_block",
    ] = "answer_directly"
    body_subsystem: Optional[Literal["tool_runtime", "browser", "computer", "mcp", "coding", "connector"]] = None
    requires_orchestrator: bool = False
    reason: str = ""
    safety_flags: List[str] = Field(default_factory=list)
    evidence_needed: List[str] = Field(default_factory=list)


class BrainOutputState(ContractModel):
    draft: Optional[str] = None
    final: Optional[str] = None


class MemoryWritebackProposal(ContractModel):
    should_store: bool
    memory_class: Optional[MemoryKind] = None
    summary_object: Optional[SummarizedMemoryObject] = None
    importance: float = 0.0
    emotion_tags: List[AffectTag] = Field(default_factory=lambda: ["neutral"])
    expiry_policy: str = "no_store"
    graph_links: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("importance")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0 or score > 1.0:
            raise ValueError("importance must be in [0,1]")
        return score


class ThoughtState(ContractModel):
    schema_version: str = Field(default="layered_brain.m1")
    turn_id: str
    timestamp: str
    input: BrainInputState
    fast_pass: FastPassState
    meaning: MeaningState
    memory_gate: MemoryGateState
    memory: BrainMemoryState
    goal_state: BrainGoalState
    reasoning: BrainReasoningState
    curiosity: BrainCuriosityState = Field(default_factory=BrainCuriosityState)
    response_plan: BrainResponsePlan
    action_decision: BrainActionDecision = Field(default_factory=BrainActionDecision)
    output: BrainOutputState
    memory_update: MemoryWritebackProposal
    brain_spikes: List[BrainSpikeEvent] = Field(default_factory=list)
    living_state: BrainLivingState = Field(default_factory=BrainLivingState)
    instinct_state: BrainInstinctState = Field(default_factory=BrainInstinctState)
    reflection_state: BrainReflectionState = Field(default_factory=BrainReflectionState)
    goal_pressure_state: BrainGoalPressureState = Field(default_factory=BrainGoalPressureState)
    autonomy_state: BrainAutonomyState = Field(default_factory=BrainAutonomyState)


class BrainThoughtSummary(ContractModel):
    turn_id: str
    reasoning_mode: Literal["fast", "slow"]
    memory_gate_should_retrieve: bool
    retrieved_memory_count: int = 0
    active_goal_ids: List[str] = Field(default_factory=list)
    writeback_should_store: bool = False
    ui_sync_required: bool = False
    curiosity_score: float = 0.0
    curiosity_questions_count: int = 0
    curiosity_think_mode: Literal["none", "quick", "deep_deferred"] = "none"
    goal_manager_authority: Optional[str] = None
    active_pm_task_goal_id: Optional[str] = None
    active_pm_task_title: Optional[str] = None
    active_pm_task_status: Optional[str] = None
    brain_spike_count: int = 0
    highest_brain_spike_priority: str = "low"
    brain_spike_event_types: List[str] = Field(default_factory=list)
    living_mood: str = "calm"
    living_energy: str = "normal"
    living_attention_focus: Optional[str] = None
    living_stress_level: float = 0.0
    living_curiosity_pressure: float = 0.0
    living_active_goal_pressure: float = 0.0
    living_risk_pressure: float = 0.0
    instinct_tone_bias: str = "neutral"
    instinct_caution_level: str = "low"
    instinct_action_bias: str = "answer_directly"
    instinct_should_ask_confirmation: bool = False
    instinct_should_surface_uncertainty: bool = False
    instinct_should_offer_next_step: bool = False
    reflection_should_reflect: bool = False
    reflection_type: str = "none"
    reflection_unresolved_count: int = 0
    reflection_memory_candidate_count: int = 0
    reflection_follow_up_count: int = 0
    reflection_requires_user_approval: bool = False
    reflection_next_best_step: Optional[str] = None
    goal_pressure_score: float = 0.0
    goal_pressure_resume_score: float = 0.0
    goal_pressure_blocked_score: float = 0.0
    goal_pressure_approval_wait_score: float = 0.0
    goal_pressure_should_resume_later: bool = False
    goal_pressure_should_ask_user_to_continue: bool = False
    goal_pressure_next_goal_step: Optional[str] = None
    autonomy_mode: str = "observe_only"
    autonomy_permission_level: str = "observe"
    autonomy_risk_score: float = 0.0
    autonomy_requires_user_approval: bool = False
    autonomy_next_safe_action: Optional[str] = None
    action_next_action: str = "answer_directly"
    action_body_subsystem: Optional[str] = None
    action_requires_orchestrator: bool = False
    action_reason: str = ""


def thought_state_summary(state: ThoughtState | Dict[str, Any] | None) -> Dict[str, Any]:
    if state is None:
        return {}
    model = state if isinstance(state, ThoughtState) else ThoughtState.model_validate(dict(state or {}))
    summary = BrainThoughtSummary(
        turn_id=str(model.turn_id),
        reasoning_mode=model.reasoning.mode,
        memory_gate_should_retrieve=bool(model.memory_gate.should_retrieve),
        retrieved_memory_count=len(model.memory.retrieved or []),
        active_goal_ids=list(model.goal_state.active_goal_ids or []),
        writeback_should_store=bool(model.memory_update.should_store),
        ui_sync_required=bool(model.goal_state.ui_sync_required),
        curiosity_score=float(model.curiosity.curiosity_score),
        curiosity_questions_count=len(model.curiosity.questions or []),
        curiosity_think_mode=model.curiosity.think_mode,
        goal_manager_authority=(str(model.goal_state.goal_manager_authority or "") or None),
        active_pm_task_goal_id=(str(model.goal_state.active_pm_task_goal_id or "") or None),
        active_pm_task_title=(str(model.goal_state.active_pm_task_title or "") or None),
        active_pm_task_status=(str(model.goal_state.active_pm_task_status or "") or None),
        brain_spike_count=int(spike_summary(model.brain_spikes).get("total", 0)),
        highest_brain_spike_priority=str(spike_summary(model.brain_spikes).get("highest_priority", "low")),
        brain_spike_event_types=list(spike_summary(model.brain_spikes).get("event_types", [])),
        living_mood=str(model.living_state.mood),
        living_energy=str(model.living_state.energy),
        living_attention_focus=model.living_state.attention_focus,
        living_stress_level=float(model.living_state.stress_level),
        living_curiosity_pressure=float(model.living_state.curiosity_pressure),
        living_active_goal_pressure=float(model.living_state.active_goal_pressure),
        living_risk_pressure=float(model.living_state.risk_pressure),
        instinct_tone_bias=str(model.instinct_state.tone_bias),
        instinct_caution_level=str(model.instinct_state.caution_level),
        instinct_action_bias=str(model.instinct_state.action_bias),
        instinct_should_ask_confirmation=bool(model.instinct_state.should_ask_confirmation),
        instinct_should_surface_uncertainty=bool(model.instinct_state.should_surface_uncertainty),
        instinct_should_offer_next_step=bool(model.instinct_state.should_offer_next_step),
        reflection_should_reflect=bool(model.reflection_state.should_reflect),
        reflection_type=str(model.reflection_state.reflection_type),
        reflection_unresolved_count=len(model.reflection_state.unresolved_items or []),
        reflection_memory_candidate_count=len(model.reflection_state.memory_candidates or []),
        reflection_follow_up_count=len(model.reflection_state.follow_up_candidates or []),
        reflection_requires_user_approval=bool(model.reflection_state.requires_user_approval),
        reflection_next_best_step=model.reflection_state.next_best_step,
        goal_pressure_score=float(model.goal_pressure_state.pressure_score),
        goal_pressure_resume_score=float(model.goal_pressure_state.resume_score),
        goal_pressure_blocked_score=float(model.goal_pressure_state.blocked_score),
        goal_pressure_approval_wait_score=float(model.goal_pressure_state.approval_wait_score),
        goal_pressure_should_resume_later=bool(model.goal_pressure_state.should_resume_later),
        goal_pressure_should_ask_user_to_continue=bool(model.goal_pressure_state.should_ask_user_to_continue),
        goal_pressure_next_goal_step=model.goal_pressure_state.next_goal_step,
        autonomy_mode=str(model.autonomy_state.mode),
        autonomy_permission_level=str(model.autonomy_state.permission_level),
        autonomy_risk_score=float(model.autonomy_state.risk_score),
        autonomy_requires_user_approval=bool(model.autonomy_state.requires_user_approval),
        autonomy_next_safe_action=model.autonomy_state.next_safe_action,
        action_next_action=str(model.action_decision.next_action),
        action_body_subsystem=(str(model.action_decision.body_subsystem) if model.action_decision.body_subsystem else None),
        action_requires_orchestrator=bool(model.action_decision.requires_orchestrator),
        action_reason=str(model.action_decision.reason or ""),
    )
    return summary.to_json_dict()
