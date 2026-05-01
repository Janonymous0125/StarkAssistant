from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from assistant.brain.contracts import ThoughtState
from assistant.brain.spikes import BrainSpikeEvent


_PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}


def _spike_id(*, turn_id: str, event_type: str, reason: str) -> str:
    digest = hashlib.sha256(f"{turn_id}:{event_type}:{reason}".encode("utf-8")).hexdigest()[:16]
    return f"brain_spike_{digest}"


def _priority_max(*values: str) -> str:
    selected = "low"
    for value in values:
        clean = str(value or "low")
        if _PRIORITY_RANK.get(clean, 0) > _PRIORITY_RANK.get(selected, 0):
            selected = clean
    return selected


class BrainSpikeDetector:
    """Build observe-only spikes from the existing layered brain ThoughtState."""

    source = "assistant.brain.spike_detector"

    def detect(self, state: ThoughtState) -> List[BrainSpikeEvent]:
        spikes: List[BrainSpikeEvent] = []
        spikes.extend(self._urgency_spikes(state))
        spikes.extend(self._emotion_spikes(state))
        spikes.extend(self._tool_spikes(state))
        spikes.extend(self._memory_spikes(state))
        spikes.extend(self._curiosity_spikes(state))
        spikes.extend(self._goal_spikes(state))
        return self._dedupe(spikes)

    def _make(
        self,
        state: ThoughtState,
        *,
        event_type: str,
        reason: str,
        priority: str = "normal",
        urgency: str | None = None,
        risk_level: str = "safe",
        payload: Dict[str, Any] | None = None,
    ) -> BrainSpikeEvent:
        dedupe_key = f"{event_type}:{reason}"
        return BrainSpikeEvent(
            spike_id=_spike_id(turn_id=state.turn_id, event_type=event_type, reason=reason),
            turn_id=state.turn_id,
            timestamp=state.timestamp,
            source=self.source,
            event_type=event_type,
            priority=priority,  # type: ignore[arg-type]
            urgency=(urgency or priority),  # type: ignore[arg-type]
            risk_level=risk_level,  # type: ignore[arg-type]
            action_mode="observe_only",
            reason=reason,
            payload=dict(payload or {}),
            status="observed",
            dedupe_key=dedupe_key,
        )

    def _urgency_spikes(self, state: ThoughtState) -> List[BrainSpikeEvent]:
        urgency = str(state.fast_pass.urgency or "normal")
        if urgency not in {"high", "critical"}:
            return []
        return [
            self._make(
                state,
                event_type="high_urgency_detected",
                reason=f"fast_pass urgency is {urgency}",
                priority=urgency,
                urgency=urgency,
                risk_level="needs_review" if urgency == "high" else "approval_required",
                payload={"intent": state.fast_pass.intent_guess, "domain": state.fast_pass.domain_guess},
            )
        ]

    def _emotion_spikes(self, state: ThoughtState) -> List[BrainSpikeEvent]:
        emotions = [str(tag) for tag in list(state.fast_pass.emotion_detected or [])]
        watched = [tag for tag in emotions if tag in {"frustrated", "scared", "angry", "stressed", "disappointed", "unresolved"}]
        if not watched:
            return []
        priority = "high" if any(tag in watched for tag in ("scared", "angry", "frustrated")) else "normal"
        return [
            self._make(
                state,
                event_type="user_affect_needs_attention",
                reason="user affect includes " + ", ".join(watched),
                priority=priority,
                urgency=_priority_max(priority, state.fast_pass.urgency),
                risk_level="needs_review",
                payload={"emotion_detected": emotions},
            )
        ]

    def _tool_spikes(self, state: ThoughtState) -> List[BrainSpikeEvent]:
        if not state.fast_pass.needs_tools and not (state.response_plan.tool_actions or []):
            return []
        return [
            self._make(
                state,
                event_type="tool_intent_detected",
                reason="turn appears to need tools or has planned tool actions",
                priority="normal",
                urgency=state.fast_pass.urgency,
                risk_level="approval_required",
                payload={
                    "needs_tools": bool(state.fast_pass.needs_tools),
                    "planned_tool_action_count": len(state.response_plan.tool_actions or []),
                    "intent": state.fast_pass.intent_guess,
                },
            )
        ]

    def _memory_spikes(self, state: ThoughtState) -> List[BrainSpikeEvent]:
        spikes: List[BrainSpikeEvent] = []
        unresolved_count = sum(1 for item in list(state.memory.retrieved or []) if bool(item.unresolved))
        if unresolved_count:
            spikes.append(
                self._make(
                    state,
                    event_type="unresolved_memory_retrieved",
                    reason=f"{unresolved_count} retrieved memory object(s) are unresolved",
                    priority="normal",
                    urgency=state.fast_pass.urgency,
                    risk_level="needs_review",
                    payload={"unresolved_memory_count": unresolved_count},
                )
            )
        if state.memory.conflicts:
            spikes.append(
                self._make(
                    state,
                    event_type="memory_conflict_detected",
                    reason="brain memory state contains conflicts",
                    priority="high",
                    urgency=_priority_max("high", state.fast_pass.urgency),
                    risk_level="needs_review",
                    payload={"conflicts": list(state.memory.conflicts or [])[:5]},
                )
            )
        if state.memory_update.should_store:
            spikes.append(
                self._make(
                    state,
                    event_type="memory_writeback_proposed",
                    reason="brain proposed a memory writeback",
                    priority="normal",
                    urgency=state.fast_pass.urgency,
                    risk_level="needs_review",
                    payload={
                        "memory_class": state.memory_update.memory_class,
                        "importance": state.memory_update.importance,
                        "expiry_policy": state.memory_update.expiry_policy,
                    },
                )
            )
        return spikes

    def _curiosity_spikes(self, state: ThoughtState) -> List[BrainSpikeEvent]:
        curiosity = state.curiosity
        if float(curiosity.curiosity_score or 0.0) < 0.65 and not curiosity.should_think_now:
            return []
        return [
            self._make(
                state,
                event_type="curiosity_pressure_detected",
                reason="curiosity score or think-now flag crossed observe threshold",
                priority="normal" if curiosity.think_mode != "quick" else "high",
                urgency=state.fast_pass.urgency,
                risk_level="safe",
                payload={
                    "curiosity_score": curiosity.curiosity_score,
                    "think_mode": curiosity.think_mode,
                    "should_think_now": curiosity.should_think_now,
                    "triggers": list(curiosity.triggers or [])[:5],
                    "next_probe": curiosity.next_probe,
                },
            )
        ]

    def _goal_spikes(self, state: ThoughtState) -> List[BrainSpikeEvent]:
        goal_state = state.goal_state
        if not goal_state.active_goal_ids and not goal_state.ui_sync_required:
            return []
        risk = "approval_required" if goal_state.active_pm_task_requires_approval else "safe"
        priority = "high" if goal_state.active_pm_task_requires_approval else "normal"
        return [
            self._make(
                state,
                event_type="active_goal_context_detected",
                reason="turn has active goal context or requires UI sync",
                priority=priority,
                urgency=_priority_max(priority, state.fast_pass.urgency),
                risk_level=risk,
                payload={
                    "active_goal_ids": list(goal_state.active_goal_ids or []),
                    "current_goal": goal_state.current_goal,
                    "active_pm_project_title": goal_state.active_pm_project_title,
                    "active_pm_task_title": goal_state.active_pm_task_title,
                    "active_pm_task_status": goal_state.active_pm_task_status,
                    "ui_sync_required": goal_state.ui_sync_required,
                },
            )
        ]

    def _dedupe(self, spikes: List[BrainSpikeEvent]) -> List[BrainSpikeEvent]:
        out: List[BrainSpikeEvent] = []
        seen: set[str] = set()
        for spike in spikes:
            key = str(spike.dedupe_key or spike.spike_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(spike)
        return out
