from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from assistant.brain.contracts import (
    AFFECT_TAGS,
    BrainCuriosityState,
    BrainGoalState,
    BrainInputState,
    BrainActionDecision,
    BrainMemoryState,
    BrainOutputState,
    BrainReasoningState,
    BrainResponsePlan,
    FastPassState,
    MeaningState,
    MemoryGateState,
    MemoryWritebackProposal,
    SummarizedMemoryObject,
    ThoughtState,
)
from assistant.contracts.memory import MemoryKind
from assistant.brain.autonomy import compute_autonomy_state
from assistant.brain.goal_pressure import compute_goal_pressure_state
from assistant.brain.instincts import compute_instinct_state
from assistant.brain.living_state import compute_living_state
from assistant.brain.reflection import compute_reflection_state
from assistant.brain.spike_detector import BrainSpikeDetector
from assistant.brain.spike_queue import BrainSpikeQueue, default_spike_queue
from assistant.contracts.time import utc_now_iso
from assistant.memory.taxonomy import MEMORY_KINDS


_MEMORY_TYPES: Tuple[MemoryKind, ...] = ("preference", "fact", "project", "rule", "research", "error")
_GOAL_STATUSES = ("active", "blocked", "on_hold")


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    t = str(text or "").lower()
    return any(n in t for n in needles)


def _bounded_summary(text: str, *, max_len: int = 260) -> str:
    t = _clean_ws(text)
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "..."


def _safe_json_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def _score_clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _token_set(text: str) -> set[str]:
    return {m.group(0).lower() for m in re.finditer(r"[A-Za-z0-9_]+", str(text or ""))}


@dataclass
class GoalSnapshot:
    goal_id: str
    title: str
    status: str
    priority: str
    summary: Optional[str] = None
    tags: Tuple[str, ...] = ()
    parent_goal_id: Optional[str] = None
    context_json: Dict[str, Any] = field(default_factory=dict)

    @property
    def searchable_text(self) -> str:
        return " ".join([self.title, self.summary or "", " ".join(self.tags)]).strip()

    @property
    def is_pm_project(self) -> bool:
        if bool(self.context_json.get("pm_project")):
            return True
        return "pm_project" in self.tags and "pm_task" not in self.tags

    @property
    def is_pm_task(self) -> bool:
        return "pm_task" in self.tags or bool(str(self.context_json.get("pm_task_status") or "").strip())

    @property
    def pm_task_status(self) -> Optional[str]:
        raw = str(self.context_json.get("pm_task_status") or "").strip()
        if raw:
            return raw
        if self.is_pm_task:
            if self.status == "active":
                return "active"
            if self.status == "blocked":
                return "blocked"
            if self.status == "on_hold":
                return "waiting_approval"
        return None

    @property
    def requires_approval(self) -> bool:
        return bool(self.context_json.get("requires_approval"))


class SummarizedMemoryAdapter:
    """Converts existing durable memories into bounded summarized memory objects."""

    def __init__(self, memory_store: Optional[Any]) -> None:
        self.memory_store = memory_store

    def retrieve(
        self,
        *,
        query_text: str,
        memory_types: Sequence[MemoryKind],
        related_goal_ids: Sequence[str],
        limit: int = 5,
    ) -> List[SummarizedMemoryObject]:
        store = self.memory_store
        if store is None or not str(query_text or "").strip():
            return []
        kinds = [str(k) for k in memory_types if str(k) in MEMORY_KINDS] or None
        try:
            hits = list(store.search_memories(query_text=query_text, kinds=kinds, status="active", limit=max(1, int(limit))))
        except Exception:
            return []
        if not hits:
            compact_query = self._compact_memory_query(query_text)
            if compact_query and compact_query != str(query_text or "").strip():
                try:
                    hits = list(store.search_memories(query_text=compact_query, kinds=kinds, status="active", limit=max(1, int(limit))))
                except Exception:
                    hits = []
        if not hits:
            identity_query = self._identity_memory_query(query_text)
            if identity_query:
                try:
                    hits = list(store.search_memories(query_text=identity_query, kinds=kinds, status="active", limit=max(1, int(limit))))
                except Exception:
                    hits = []
        if not hits:
            return []
        hits = self._rank_explicit_title_hits(query_text=query_text, hits=hits)

        rows_by_id = self._memory_rows_by_id([str(getattr(hit, "memory_id", "") or "") for hit in hits])
        now = utc_now_iso()
        objects: List[SummarizedMemoryObject] = []
        for hit in hits:
            memory_id = str(getattr(hit, "memory_id", "") or "").strip()
            if not memory_id:
                continue
            row = dict(rows_by_id.get(memory_id) or {})
            kind = str(row.get("kind") or getattr(hit, "kind", "") or "fact").strip().lower()
            if kind not in MEMORY_KINDS:
                kind = "fact"
            title = str(row.get("title") or getattr(hit, "title", "") or kind).strip()
            content = str(row.get("content") or getattr(hit, "content", "") or "").strip()
            tags = str(row.get("tags") or getattr(hit, "tags", "") or "")
            evidence = _safe_json_dict(row.get("evidence_json"))
            meta = _safe_json_dict(row.get("meta_json"))
            vault_content = self._vault_anchor_content(row)
            if vault_content:
                content = vault_content
            relevance = _score_clamp(float(getattr(hit, "best_match_score", 0.0) or 0.0))
            source_kind = self._source_kind(evidence=evidence, meta=meta)
            source_refs = [f"sqlite:memories/{memory_id}"]
            vault_ref = self._vault_source_ref(evidence=evidence, meta=meta)
            if vault_ref:
                source_refs.append(vault_ref)
            for event_id in list(evidence.get("event_ids") or [])[:3]:
                sid = str(event_id or "").strip()
                if sid:
                    source_refs.append(f"sqlite:events/{sid}")
            importance = self._importance(kind=kind, tags=tags, content=content, score_hint=meta.get("score_hint"))
            emotions = self._emotion_tags_from_text(" ".join([tags, title, content]))
            summary_source = ". ".join([x for x in [title, _bounded_summary(content, max_len=220)] if x])
            objects.append(
                SummarizedMemoryObject(
                    memory_id=memory_id,
                    memory_type=kind,  # type: ignore[arg-type]
                    summary=_bounded_summary(summary_source, max_len=280),
                    source_kind=source_kind,
                    source_refs=source_refs,
                    importance=importance,
                    emotion_tags=emotions,  # type: ignore[arg-type]
                    created_at=str(row.get("ts_utc") or now),
                    updated_at=str(row.get("updated_ts_utc") or now),
                    related_goal_ids=[str(gid) for gid in related_goal_ids if str(gid).strip()],
                    relevance_score=relevance,
                    unresolved=bool(_contains_any(" ".join([tags, title, content]), ["unresolved", "blocked", "todo", "follow up", "follow-up"])),
                    expiry_policy=str(meta.get("expiry_policy") or meta.get("retention_hint") or "retain_until_superseded"),
                )
            )
        return objects

    def _rank_explicit_title_hits(self, *, query_text: str, hits: Sequence[Any]) -> List[Any]:
        raw = str(query_text or "")
        match = re.search(r"\bmemory\s+titled\s+(.+?)(?:[.?]|$)", raw, re.IGNORECASE)
        if not match:
            return list(hits or [])
        wanted = _clean_ws(match.group(1)).lower()
        if not wanted:
            return list(hits or [])

        def key(hit: Any) -> Tuple[int, float]:
            title = _clean_ws(str(getattr(hit, "title", "") or "")).lower()
            exactish = wanted in title or title.endswith(wanted)
            return (0 if exactish else 1, -float(getattr(hit, "best_match_score", 0.0) or 0.0))

        return sorted(list(hits or []), key=key)

    def _vault_source_ref(self, *, evidence: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
        vault = dict(meta.get("vault") or {})
        refs = dict(evidence.get("refs") or {})
        relative_path = str(vault.get("relative_path") or refs.get("relative_path") or "").strip()
        anchor = str(vault.get("anchor") or refs.get("anchor") or "").strip()
        if not relative_path:
            return None
        return f"obsidian:{relative_path}{('#' + anchor) if anchor else ''}"

    def _vault_anchor_content(self, row: Dict[str, Any]) -> Optional[str]:
        if self.memory_store is None:
            return None
        meta = _safe_json_dict(row.get("meta_json"))
        if str(meta.get("source_of_truth") or "").strip().lower() != "obsidian_vault":
            return None
        try:
            from assistant.memory.obsidian import read_vault_memory_anchor

            return read_vault_memory_anchor(store=self.memory_store, row=row)
        except Exception:
            return None

    def obsidian_status(self) -> Dict[str, Any]:
        store = self.memory_store
        if store is None:
            return {"enabled": False, "reason": "memory_store_not_ready"}
        getter = getattr(store, "obsidian_status", None)
        if not callable(getter):
            return {"enabled": False, "reason": "obsidian_status_unavailable"}
        try:
            return dict(getter() or {})
        except Exception as exc:
            return {"enabled": False, "reason": str(exc)}

    def obsidian_status_snapshot(self) -> Dict[str, Any]:
        return self.obsidian_status()

    def _source_kind(self, *, evidence: Dict[str, Any], meta: Dict[str, Any]) -> str:
        source = meta.get("source")
        source_type = ""
        if isinstance(source, dict):
            source_type = str(source.get("type") or "").strip().lower()
        evidence_source = str(evidence.get("source") or "").strip().lower()
        if source_type == "obsidian" or evidence_source == "obsidian_vault":
            return "obsidian_vault"
        return "durable_memory"

    def _memory_rows_by_id(self, memory_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        ids = [str(mid or "").strip() for mid in memory_ids if str(mid or "").strip()]
        if not ids or self.memory_store is None:
            return {}
        placeholders = ",".join("?" for _ in ids)
        try:
            with self.memory_store.db._open() as con:
                rows = con.execute(
                    f"""
                    SELECT id, ts_utc, updated_ts_utc, kind, subtype, status, task_signature,
                           title, content, tags, evidence_json, meta_json
                    FROM memories
                    WHERE id IN ({placeholders});
                    """,
                    tuple(ids),
                ).fetchall()
        except Exception:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            out[str(row["id"])] = {key: row[key] for key in row.keys()}
        return out

    def _compact_memory_query(self, query_text: str) -> str:
        stop = {
            "again",
            "and",
            "about",
            "as",
            "before",
            "comparison",
            "continue",
            "does",
            "from",
            "goal",
            "ignore",
            "last",
            "memory",
            "only",
            "previous",
            "recall",
            "remember",
            "resume",
            "that",
            "the",
            "titled",
            "time",
            "what",
            "when",
            "where",
            "with",
            "you",
        }
        tokens = [tok.lower() for tok in re.findall(r"[A-Za-z0-9_]+", str(query_text or ""))]
        kept = [tok for tok in tokens if len(tok) >= 4 and tok not in stop]
        return " ".join(list(dict.fromkeys(kept))[:6]).strip()

    def _identity_memory_query(self, query_text: str) -> str:
        text = str(query_text or "").lower()
        asks_user_identity = _contains_any(text, ["about me", "who am i", "what am i", "my name", "relationship to stark"])
        asks_named_identity = bool("jeremiah" in text and "relationship" in text and _contains_any(text, ["stark", "assistant"]))
        if not (asks_user_identity or asks_named_identity):
            return ""
        if not (asks_named_identity or _contains_any(text, ["remember", "what do you know", "who am i", "what am i"])):
            return ""
        if asks_named_identity:
            return "jeremiah creator stark"
        if _contains_any(text, ["stark", "assistant"]):
            return "user identity creator stark assistant"
        return "user identity"

    def _importance(self, *, kind: str, tags: str, content: str, score_hint: Any) -> float:
        try:
            if score_hint is not None:
                return _score_clamp(float(score_hint))
        except Exception:
            pass
        base = {
            "preference": 0.7,
            "project": 0.68,
            "rule": 0.72,
            "research": 0.62,
            "error": 0.74,
            "fact": 0.55,
        }.get(kind, 0.5)
        text = f"{tags} {content}".lower()
        if _contains_any(text, ["urgent", "important", "critical", "blocked", "unresolved"]):
            base += 0.12
        return _score_clamp(base)

    def _emotion_tags_from_text(self, text: str) -> List[str]:
        tags = detect_affect_tags(text)
        return tags or ["neutral"]


class GoalManagerBridge:
    """Read-only bridge to Stark's existing UI GoalStore authority."""

    def __init__(self, goal_store: Optional[Any]) -> None:
        self.goal_store = goal_store

    def _goal_matches_session(self, *, goal: GoalSnapshot, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return True
        goal_session = str((goal.context_json or {}).get("session_id") or "").strip()
        if goal_session:
            return goal_session == sid
        store = self.goal_store
        if store is not None and hasattr(store, "list_goal_activity"):
            try:
                activity = list(store.list_goal_activity(goal_id=goal.goal_id, limit=10))
            except Exception:
                activity = []
            for item in activity:
                activity_session = str((item or {}).get("session_id") or "").strip()
                if activity_session == sid:
                    return True
        return False

    def active_goals(self, *, session_id: str, limit: int = 20) -> List[GoalSnapshot]:
        store = self.goal_store
        if store is None or not hasattr(store, "list_goals"):
            return []
        try:
            goals = list(store.list_goals(statuses=list(_GOAL_STATUSES), limit=max(1, int(limit))))
        except Exception:
            return []
        out: List[GoalSnapshot] = []
        for goal in goals:
            out.append(
                GoalSnapshot(
                    goal_id=str(getattr(goal, "goal_id", "") or ""),
                    title=str(getattr(goal, "title", "") or ""),
                    status=str(getattr(goal, "status", "") or ""),
                    priority=str(getattr(goal, "priority", "") or ""),
                    summary=(str(getattr(goal, "summary", "") or "").strip() or None),
                    tags=tuple(str(x) for x in list(getattr(goal, "tags", []) or [])),
                    parent_goal_id=(str(getattr(goal, "parent_goal_id", "") or "").strip() or None),
                    context_json=_safe_json_dict(getattr(goal, "context_json", {}) or {}),
                )
            )
        return [g for g in out if g.goal_id and self._goal_matches_session(goal=g, session_id=session_id)]

    def reflect(
        self,
        *,
        normalized_text: str,
        active_goals: Sequence[GoalSnapshot],
        memory_writeback_hint: bool,
    ) -> BrainGoalState:
        relevant = self.match_relevant_goals(normalized_text=normalized_text, active_goals=active_goals)
        authority = self._goal_manager_authority(active_goals=active_goals)
        pm_project, pm_task = self._resolve_pm_focus(relevant_goals=relevant, active_goals=active_goals)
        goal_keywords = _contains_any(
            normalized_text,
            ["goal", "task", "todo", "milestone", "project", "ship", "complete", "done", "blocked", "unresolved", "continue", "next"],
        )
        current = pm_project or pm_task or (relevant[0] if relevant else None)
        active_goal_ids: List[str] = []
        for goal in [pm_project, pm_task, *list(relevant)]:
            if goal is None:
                continue
            goal_id = str(goal.goal_id or "").strip()
            if goal_id and goal_id not in active_goal_ids:
                active_goal_ids.append(goal_id)
        ui_sync = bool(
            (relevant or pm_task or pm_project)
            and (
                goal_keywords
                or memory_writeback_hint
                or _contains_any(normalized_text, ["complete", "done", "blocked", "unblocked", "new task", "create task"])
                or bool(pm_task and _contains_any(normalized_text, ["continue", "resume", "plan", "project", "task", "next"]))
            )
        )
        subgoal = self._subgoal_guess(normalized_text)
        if pm_task is not None:
            subgoal = pm_task.title
        return BrainGoalState(
            active_goal_ids=active_goal_ids,
            current_goal=((pm_project.title if pm_project else current.title) if current else None),
            subgoal=subgoal,
            status=((pm_task.pm_task_status or pm_task.status) if pm_task else (current.status if current else None)),
            ui_sync_required=ui_sync,
            goal_manager_authority=authority,
            active_pm_project_goal_id=(pm_project.goal_id if pm_project else None),
            active_pm_project_title=(pm_project.title if pm_project else None),
            active_pm_task_goal_id=(pm_task.goal_id if pm_task else None),
            active_pm_task_title=(pm_task.title if pm_task else None),
            active_pm_task_status=((pm_task.pm_task_status or pm_task.status) if pm_task else None),
            active_pm_task_requires_approval=(pm_task.requires_approval if pm_task else False),
        )

    def match_relevant_goals(self, *, normalized_text: str, active_goals: Sequence[GoalSnapshot]) -> List[GoalSnapshot]:
        text_tokens = _token_set(normalized_text)
        text = str(normalized_text or "").lower()
        if not active_goals:
            return []
        continuity = _contains_any(text, ["goal", "task", "todo", "project", "milestone", "continue", "resume", "next", "ship", "release", "unresolved", "blocked"])
        matched: List[GoalSnapshot] = []
        for goal in active_goals:
            goal_tokens = _token_set(goal.searchable_text)
            overlap = sorted(tok for tok in text_tokens.intersection(goal_tokens) if len(tok) >= 4)
            if overlap or continuity:
                matched.append(goal)
        return matched[:8]

    def _subgoal_guess(self, normalized_text: str) -> Optional[str]:
        text = str(normalized_text or "").strip()
        if not text:
            return None
        if _contains_any(text, ["subgoal", "next step", "todo", "task"]):
            return _bounded_summary(text, max_len=120)
        return None

    def _goal_manager_authority(self, *, active_goals: Sequence[GoalSnapshot]) -> Optional[str]:
        for goal in active_goals:
            authority = str(goal.context_json.get("goal_manager_authority") or "").strip()
            if authority:
                return authority
        if self.goal_store is not None:
            return "GoalStore"
        return None

    def _resolve_pm_focus(
        self,
        *,
        relevant_goals: Sequence[GoalSnapshot],
        active_goals: Sequence[GoalSnapshot],
    ) -> Tuple[Optional[GoalSnapshot], Optional[GoalSnapshot]]:
        preferred_pool = list(relevant_goals or [])
        fallback_pool = list(active_goals or [])
        project_index: Dict[str, GoalSnapshot] = {
            goal.goal_id: goal for goal in fallback_pool if goal.is_pm_project and goal.goal_id
        }
        for goal in preferred_pool:
            if goal.is_pm_project and goal.goal_id and goal.goal_id not in project_index:
                project_index[goal.goal_id] = goal

        pm_tasks = [goal for goal in preferred_pool if goal.is_pm_task]
        if not pm_tasks:
            pm_tasks = [goal for goal in fallback_pool if goal.is_pm_task]
        pm_task = self._select_pm_task(pm_tasks)

        pm_project: Optional[GoalSnapshot] = None
        if pm_task is not None:
            parent_goal_id = str(pm_task.parent_goal_id or "").strip()
            if parent_goal_id:
                pm_project = project_index.get(parent_goal_id)
        if pm_project is None:
            project_candidates = [goal for goal in preferred_pool if goal.is_pm_project]
            if not project_candidates:
                project_candidates = [goal for goal in fallback_pool if goal.is_pm_project]
            pm_project = self._select_pm_project(project_candidates)
        return pm_project, pm_task

    def _select_pm_task(self, goals: Sequence[GoalSnapshot]) -> Optional[GoalSnapshot]:
        ranked = [goal for goal in goals if goal.goal_id]
        if not ranked:
            return None

        def _task_key(goal: GoalSnapshot) -> Tuple[int, int, str]:
            status = str(goal.pm_task_status or goal.status or "").strip().lower()
            status_rank = {
                "active": 0,
                "waiting_approval": 1,
                "blocked": 2,
                "proposed": 3,
                "verified": 4,
                "done": 5,
            }.get(status, 9)
            try:
                order = int(goal.context_json.get("task_order") or 9999)
            except Exception:
                order = 9999
            return (status_rank, order, goal.title.lower())

        return sorted(ranked, key=_task_key)[0]

    def _select_pm_project(self, goals: Sequence[GoalSnapshot]) -> Optional[GoalSnapshot]:
        ranked = [goal for goal in goals if goal.goal_id]
        if not ranked:
            return None

        def _project_key(goal: GoalSnapshot) -> Tuple[int, str]:
            status = str(goal.status or "").strip().lower()
            status_rank = {"active": 0, "on_hold": 1, "blocked": 2}.get(status, 9)
            return (status_rank, goal.title.lower())

        return sorted(ranked, key=_project_key)[0]


def detect_affect_tags(text: str) -> List[str]:
    t = str(text or "").lower()
    tags: List[str] = []
    checks: List[Tuple[str, Sequence[str]]] = [
        ("curious", ("?", "wonder", "curious", "why", "how does")),
        ("confident", ("definitely", "sure", "confident", "works", "solved")),
        ("uncertain", ("maybe", "not sure", "uncertain", "confused", "ambiguous")),
        ("important", ("important", "critical", "must", "non-negotiable", "priority")),
        ("urgent", ("urgent", "asap", "immediately", "right now", "emergency")),
        ("unresolved", ("unresolved", "blocked", "stuck", "pending", "todo", "not done")),
        ("frustrated", ("frustrated", "annoyed", "fed up", "broken again")),
        ("scared", ("scared", "afraid", "worried", "panic")),
        ("angry", ("angry", "furious", "mad", "rage")),
        ("stressed", ("stressed", "overwhelmed", "too much", "pressure")),
        ("excited", ("excited", "great", "awesome", "love this", "fantastic")),
        ("cautious", ("careful", "cautious", "safely", "safe", "risk")),
        ("satisfied", ("thanks", "thank you", "perfect", "nice", "satisfied")),
        ("disappointed", ("disappointed", "not good", "failed me", "bad result")),
    ]
    for tag, needles in checks:
        if _contains_any(t, needles):
            tags.append(tag)
    if not tags:
        tags.append("neutral")
    return [tag for tag in tags if tag in AFFECT_TAGS]


class LayeredBrainPipeline:
    """First shipped layered brain pipeline for one Stark user turn."""

    def __init__(
        self,
        *,
        memory_store: Optional[Any] = None,
        goal_store: Optional[Any] = None,
        spike_queue: Optional[BrainSpikeQueue] = None,
    ) -> None:
        self.memory_adapter = SummarizedMemoryAdapter(memory_store)
        self.goal_bridge = GoalManagerBridge(goal_store)
        self.spike_detector = BrainSpikeDetector()
        self.spike_queue = spike_queue or default_spike_queue()

    def process_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        event_id_user: Optional[str],
        user_message: str,
        ui_meta: Optional[Dict[str, Any]] = None,
    ) -> ThoughtState:
        ui = dict(ui_meta or {})
        timestamp = utc_now_iso()
        input_state = self._normalize_input(
            session_id=session_id,
            turn_id=turn_id,
            event_id_user=event_id_user,
            user_message=user_message,
            ui_meta=ui,
        )
        active_goals = self.goal_bridge.active_goals(session_id=session_id)
        fast_pass = self._fast_pass(input_state.normalized_text)
        meaning = self._meaning_parse(input_state.normalized_text, fast_pass=fast_pass)
        preliminary_goal_matches = self.goal_bridge.match_relevant_goals(
            normalized_text=input_state.normalized_text,
            active_goals=active_goals,
        )
        memory_gate = self._memory_gate(
            input_state=input_state,
            fast_pass=fast_pass,
            meaning=meaning,
            relevant_goals=preliminary_goal_matches,
        )
        retrieved = (
            self.memory_adapter.retrieve(
                query_text=input_state.normalized_text,
                memory_types=memory_gate.memory_types,
                related_goal_ids=[g.goal_id for g in preliminary_goal_matches],
                limit=5,
            )
            if memory_gate.should_retrieve
            else []
        )
        memory_state = self._memory_state(retrieved, memory_gate=memory_gate)
        memory_state = self._refine_memory_state(
            input_state=input_state,
            meaning=meaning,
            memory_gate=memory_gate,
            memory_state=memory_state,
        )
        reasoning = self._select_reasoning(
            fast_pass=fast_pass,
            meaning=meaning,
            memory_gate=memory_gate,
            memory_state=memory_state,
            relevant_goals=preliminary_goal_matches,
        )
        curiosity = self._curiosity_state(
            input_state=input_state,
            fast_pass=fast_pass,
            meaning=meaning,
            memory_gate=memory_gate,
            memory_state=memory_state,
            reasoning=reasoning,
            relevant_goals=preliminary_goal_matches,
        )
        response_plan = self._response_plan(
            input_state=input_state,
            fast_pass=fast_pass,
            meaning=meaning,
            reasoning=reasoning,
            memory_gate=memory_gate,
            memory_state=memory_state,
            curiosity=curiosity,
        )
        action_decision = self._action_decision(
            fast_pass=fast_pass,
            meaning=meaning,
            reasoning=reasoning,
            memory_gate=memory_gate,
            response_plan=response_plan,
        )
        memory_update = self._memory_update_proposal(
            timestamp=timestamp,
            turn_id=turn_id,
            event_id_user=event_id_user,
            input_state=input_state,
            fast_pass=fast_pass,
            meaning=meaning,
            relevant_goals=preliminary_goal_matches,
        )
        goal_state = self.goal_bridge.reflect(
            normalized_text=input_state.normalized_text,
            active_goals=active_goals,
            memory_writeback_hint=bool(memory_update.should_store),
        )
        if goal_state.ui_sync_required and not response_plan.memory_writeback and memory_update.should_store:
            response_plan = response_plan.model_copy(update={"memory_writeback": True})
        thought_state = ThoughtState(
            turn_id=str(turn_id),
            timestamp=timestamp,
            input=input_state,
            fast_pass=fast_pass,
            meaning=meaning,
            memory_gate=memory_gate,
            memory=memory_state,
            goal_state=goal_state,
            reasoning=reasoning,
            curiosity=curiosity,
            response_plan=response_plan,
            action_decision=action_decision,
            output=BrainOutputState(draft=self._draft_handoff_note(reasoning=reasoning, response_plan=response_plan), final=None),
            memory_update=memory_update,
        )
        brain_spikes = self.spike_detector.detect(thought_state)
        queued_spikes = self.spike_queue.push_many(brain_spikes)
        thought_state = thought_state.model_copy(update={"brain_spikes": queued_spikes})
        living_state = compute_living_state(thought_state)
        thought_state = thought_state.model_copy(update={"living_state": living_state})
        instinct_state = compute_instinct_state(thought_state)
        thought_state = thought_state.model_copy(update={"instinct_state": instinct_state})
        reflection_state = compute_reflection_state(thought_state)
        thought_state = thought_state.model_copy(update={"reflection_state": reflection_state})
        goal_pressure_state = compute_goal_pressure_state(thought_state)
        thought_state = thought_state.model_copy(update={"goal_pressure_state": goal_pressure_state})
        autonomy_state = compute_autonomy_state(thought_state)
        return thought_state.model_copy(update={"autonomy_state": autonomy_state})

    def _normalize_input(
        self,
        *,
        session_id: str,
        turn_id: str,
        event_id_user: Optional[str],
        user_message: str,
        ui_meta: Dict[str, Any],
    ) -> BrainInputState:
        raw = str(user_message or "")
        normalized = _clean_ws(raw)
        attachments = list(ui_meta.get("attachments") or []) if isinstance(ui_meta.get("attachments"), list) else []
        modalities: List[str] = ["text"]
        if ui_meta.get("voice") or ui_meta.get("audio") or ui_meta.get("transcript"):
            modalities.append("voice")
        if any(str((att or {}).get("kind") or (att or {}).get("type") or "").lower().startswith("image") for att in attachments if isinstance(att, dict)):
            modalities.append("image")
        if attachments and "image" not in modalities:
            modalities.append("file")
        source_refs = [f"session:{session_id}", f"turn:{turn_id}"]
        if event_id_user:
            source_refs.append(f"sqlite:events/{event_id_user}")
        return BrainInputState(
            modalities=modalities,
            raw_text=raw,
            normalized_text=normalized,
            source_refs=source_refs,
            source_metadata={
                "channel": str(ui_meta.get("channel") or "chat"),
                "client_msg_id": str(ui_meta.get("client_msg_id") or "") or None,
                "request_id": str(ui_meta.get("request_id") or "") or None,
                "attachment_count": len(attachments),
            },
            voice={"status": "placeholder", "transcript": str(ui_meta.get("transcript") or "").strip() or None} if "voice" in modalities else {"status": "not_provided"},
            image={"status": "placeholder", "items": [a for a in attachments if isinstance(a, dict) and str(a.get("kind") or a.get("type") or "").lower().startswith("image")][:3]} if "image" in modalities else {"status": "not_provided"},
            file={"status": "placeholder", "items": attachments[:3]} if "file" in modalities else {"status": "not_provided"},
        )

    def _fast_pass(self, normalized_text: str) -> FastPassState:
        text = str(normalized_text or "")
        lower = text.lower()
        words = re.findall(r"[A-Za-z0-9_]+", lower)
        emotions = detect_affect_tags(text)
        needs_tools = _contains_any(
            lower,
            [
                "open ",
                "run ",
                "execute",
                "write ",
                "read ",
                "browse",
                "search",
                "look up",
                "screenshot",
                "click",
                "install",
                "build",
                "fix ",
                "test ",
                "deploy",
                "create file",
            ],
        )
        needs_memory = _contains_any(
            lower,
            [
                "remember",
                "recall",
                "previous",
                "prior",
                "last time",
                "again",
                "continue",
                "resume",
                "as before",
                "same as",
                "preference",
                "i prefer",
                "we decided",
                "unresolved",
                "goal",
                "task",
                "todo",
                "project",
                "milestone",
            ],
        )
        urgency = "normal"
        if any(tag in emotions for tag in ("urgent", "scared", "angry")):
            urgency = "high"
        if _contains_any(lower, ["emergency", "critical outage", "right now"]):
            urgency = "critical"
        if _contains_any(lower, ["hello", "hi", "hey"]) and len(words) <= 6:
            intent = "greeting"
        elif _contains_any(lower, ["remember", "recall", "what do you know", "what do you remember"]):
            intent = "memory_query"
        elif lower.endswith("?") or lower.startswith(("what ", "why ", "how ", "when ", "where ", "who ")):
            intent = "question"
        elif _contains_any(lower, ["goal", "task", "todo", "complete", "done", "blocked"]):
            intent = "goal_update"
        elif needs_tools:
            intent = "tool_request"
        else:
            intent = "instruction"
        domain = "general"
        if _contains_any(lower, ["stark", "repo", "code", "test", "script", "project", "release"]):
            domain = "project"
        if _contains_any(lower, ["memory", "remember", "previous"]):
            domain = "memory"
        if _contains_any(lower, ["goal", "task", "todo", "milestone"]):
            domain = "goals"
        low_emotion = not any(tag in emotions for tag in ("urgent", "unresolved", "frustrated", "scared", "angry", "stressed", "disappointed"))
        can_direct = bool(text and len(words) <= 18 and not needs_tools and not needs_memory and low_emotion)
        confidence = 0.86 if can_direct else 0.58
        if needs_tools or needs_memory:
            confidence = 0.64
        if urgency in {"high", "critical"}:
            confidence = min(confidence, 0.55)
        return FastPassState(
            intent_guess=intent,
            domain_guess=domain,
            urgency=urgency,  # type: ignore[arg-type]
            emotion_detected=emotions,  # type: ignore[arg-type]
            needs_memory=bool(needs_memory),
            needs_tools=bool(needs_tools),
            can_answer_directly=bool(can_direct),
            confidence=_score_clamp(confidence),
        )

    def _meaning_parse(self, normalized_text: str, *, fast_pass: FastPassState) -> MeaningState:
        text = str(normalized_text or "")
        lower = text.lower()
        raw_entities = list(dict.fromkeys(re.findall(r'"([^"]+)"|`([^`]+)`', text)))
        entity_flat: List[str] = []
        for item in raw_entities:
            if isinstance(item, tuple):
                entity_flat.extend([str(x).strip() for x in item if str(x).strip()])
            elif str(item).strip():
                entity_flat.append(str(item).strip())
        for match in re.finditer(r"\b([A-Z][A-Za-z0-9_]+(?:\s+[A-Z][A-Za-z0-9_]+){0,3})\b", text):
            value = match.group(1).strip()
            if value.lower() not in {"i"} and value not in entity_flat:
                entity_flat.append(value)
        constraints: List[str] = []
        for marker in ("do not", "don't", "only", "must", "without", "avoid", "preserve", "keep", "bounded", "safe", "no "):
            if marker in lower:
                constraints.append(marker.strip())
        references: List[str] = []
        for ref in ("this", "that", "it", "again", "previous", "prior", "last time", "continue", "resume"):
            if re.search(rf"\b{re.escape(ref)}\b", lower):
                references.append(ref)
        ambiguities: List[str] = []
        if any(ref in references for ref in ("this", "that", "it")) and len(entity_flat) == 0:
            ambiguities.append("deictic_reference_without_explicit_target")
        if _contains_any(lower, ["something", "stuff", "whatever", "soon", "later"]):
            ambiguities.append("underspecified_request")
        if fast_pass.intent_guess == "memory_query" and not references:
            references.append("direct_memory_query")
        user_goal = _bounded_summary(text, max_len=180) if text else None
        assistant_goal = "answer directly" if fast_pass.can_answer_directly else "route through Stark's existing runtime with structured context"
        return MeaningState(
            intent=fast_pass.intent_guess,
            entities=entity_flat[:12],
            relationships=[],
            constraints=list(dict.fromkeys(constraints)),
            references=list(dict.fromkeys(references)),
            ambiguities=list(dict.fromkeys(ambiguities)),
            user_goal=user_goal,
            assistant_goal=assistant_goal,
        )

    def _memory_gate(
        self,
        *,
        input_state: BrainInputState,
        fast_pass: FastPassState,
        meaning: MeaningState,
        relevant_goals: Sequence[GoalSnapshot],
    ) -> MemoryGateState:
        text = input_state.normalized_text.lower()
        memory_types: List[str] = []
        reasons: List[str] = []

        if fast_pass.needs_memory:
            reasons.append("continuity_or_memory_keyword")
        if relevant_goals:
            reasons.append("active_goal_context")
            memory_types.append("project")
        if meaning.references:
            reasons.append("prior_context_reference")
        if any(tag in fast_pass.emotion_detected for tag in ("frustrated", "scared", "angry", "stressed", "disappointed", "unresolved")):
            reasons.append("affective_continuity")
        direct_memory_query = _contains_any(
            text,
            ["what do you remember", "what do you know", "do you remember", "recall", "memory titled", "who am i", "what am i"],
        )
        direct_user_memory_query = bool(
            direct_memory_query and _contains_any(text, ["about me", "my name", "who am i", "what am i", "relationship"])
        )
        if _contains_any(text, ["what do you remember", "do you remember", "recall", "memory titled", "remember that", "save this"]):
            reasons.append("direct_memory_request")
        if direct_memory_query:
            memory_types.extend(["fact", "preference", "project", "rule", "research", "error"])
        if direct_user_memory_query:
            memory_types.extend(["fact", "preference"])
        if _contains_any(text, ["i prefer", "my preference", "call me", "remember i"]):
            memory_types.append("preference")
        if _contains_any(text, ["project", "goal", "milestone", "release", "task", "todo", "stark"]):
            memory_types.append("project")
        if _contains_any(text, ["bug", "error", "failed", "failure", "crash", "regression", "broken"]):
            memory_types.append("error")
        if _contains_any(text, ["procedure", "checklist", "how to", "steps", "workflow"]):
            memory_types.append("research")
        if _contains_any(text, ["rule", "policy", "always", "never"]):
            memory_types.append("rule")
        if not memory_types and reasons:
            memory_types.extend(["project", "preference", "fact", "research", "error"])

        should = bool(reasons)
        if fast_pass.can_answer_directly and not relevant_goals and not meaning.references and not _contains_any(text, ["remember", "preference"]):
            should = False
            reasons = ["low_risk_isolated_turn"]
            memory_types = []
        return MemoryGateState(
            should_retrieve=bool(should),
            memory_types=[m for m in list(dict.fromkeys(memory_types)) if m in _MEMORY_TYPES],  # type: ignore[list-item]
            reason="; ".join(reasons) if reasons else "no_memory_relevance_detected",
        )

    def _memory_state(
        self,
        retrieved: Sequence[SummarizedMemoryObject],
        *,
        memory_gate: MemoryGateState,
    ) -> BrainMemoryState:
        items = list(retrieved or [])
        relevance = max([float(item.relevance_score) for item in items], default=0.0)
        active_summary = None
        if items:
            active_summary = _bounded_summary(" | ".join(item.summary for item in items[:3]), max_len=360)
        conflicts: List[str] = []
        unresolved_types = {item.memory_type for item in items if item.unresolved}
        if len(unresolved_types) > 1:
            conflicts.append("multiple_unresolved_memory_types")
        source_breakdown: Dict[str, int] = {}
        for item in items:
            source = str(item.source_kind or "unknown").strip() or "unknown"
            source_breakdown[source] = source_breakdown.get(source, 0) + 1
        return BrainMemoryState(
            retrieved=items,
            active_summary=active_summary,
            conflicts=conflicts,
            relevance_score=_score_clamp(relevance),
            retrieval_reason=memory_gate.reason,
            source_breakdown=source_breakdown,
            usage_hints=self._memory_usage_hints(items),
            obsidian_status=self.memory_adapter.obsidian_status(),
        )

    def _memory_usage_hints(self, items: Sequence[SummarizedMemoryObject]) -> List[str]:
        hints: List[str] = []
        for item in items:
            kind = str(item.memory_type or "").strip().lower()
            if kind == "preference":
                hints.append("adapt response style")
                hints.append("adapt wording, workflow, or defaults")
            elif kind == "rule":
                hints.append("treat as constraint")
                hints.append("must be treated as a constraint unless contradicted by current user instruction")
            elif kind == "project":
                hints.append("preserve project continuity")
                hints.append("preserve continuity with current project state")
            elif kind == "error":
                hints.append("avoid repeating known failure")
            elif kind in {"research", "fact"}:
                hints.append("use as supporting context")
            if item.source_kind == "obsidian_vault":
                hints.append("treat Obsidian note as imported reference")
                hints.append("treat as vault reference; verify against current user instruction before applying")
        return list(dict.fromkeys(hints))

    def _refine_memory_state(
        self,
        *,
        input_state: BrainInputState,
        meaning: MeaningState,
        memory_gate: MemoryGateState,
        memory_state: BrainMemoryState,
    ) -> BrainMemoryState:
        lower = str(input_state.normalized_text or "").lower()
        conflicts = list(memory_state.conflicts or [])
        if _contains_any(lower, ["do not use memory", "don't use memory", "without using memory"]):
            if "user_requested_no_memory" not in conflicts:
                conflicts.append("user_requested_no_memory")
            return memory_state.model_copy(
                update={
                    "retrieved": [],
                    "active_summary": None,
                    "relevance_score": 0.0,
                    "source_breakdown": {},
                    "usage_hints": [],
                    "conflicts": conflicts,
                }
            )

        items: List[SummarizedMemoryObject] = []
        seen: set[str] = set()
        for item in list(memory_state.retrieved or []):
            key = _clean_ws(f"{item.memory_type}:{item.source_kind}:{item.summary}").lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

        if _contains_any(lower, ["ignore previous", "ignore prior", "override previous"]):
            if "user_requested_memory_override" not in conflicts:
                conflicts.append("user_requested_memory_override")

        explicit_title = ""
        title_match = re.search(r"\bmemory\s+titled\s+(.+?)(?:[.?]|$)", str(input_state.normalized_text or ""), re.IGNORECASE)
        if title_match:
            explicit_title = _clean_ws(title_match.group(1)).lower()

        def _memory_rank_key(item: SummarizedMemoryObject) -> Tuple[int, float, float, float]:
            summary = _clean_ws(item.summary).lower()
            title_match_rank = 1 if explicit_title and explicit_title in summary else 0
            return (title_match_rank, float(item.importance), float(item.relevance_score), 1.0 if item.unresolved else 0.0)

        high_value = sorted(
            items,
            key=_memory_rank_key,
            reverse=True,
        )
        active_summary = None
        if high_value:
            active_summary = _bounded_summary(
                " | ".join(f"{item.memory_type}/{item.source_kind}: {item.summary}" for item in high_value[:4]),
                max_len=520,
            )
        source_breakdown: Dict[str, int] = {}
        for item in items:
            source = str(item.source_kind or "unknown").strip() or "unknown"
            source_breakdown[source] = source_breakdown.get(source, 0) + 1
        relevance = max([float(item.relevance_score) for item in items], default=0.0)
        usage_hints = self._memory_usage_hints(items)
        return memory_state.model_copy(
            update={
                "retrieved": items,
                "active_summary": active_summary,
                "conflicts": conflicts,
                "relevance_score": _score_clamp(relevance),
                "source_breakdown": source_breakdown,
                "usage_hints": usage_hints,
            }
        )

    def _select_reasoning(
        self,
        *,
        fast_pass: FastPassState,
        meaning: MeaningState,
        memory_gate: MemoryGateState,
        memory_state: BrainMemoryState,
        relevant_goals: Sequence[GoalSnapshot],
    ) -> BrainReasoningState:
        slow_reasons = []
        if memory_gate.should_retrieve:
            slow_reasons.append("memory_relevance")
        if fast_pass.needs_tools:
            slow_reasons.append("tool_or_action_need")
        if meaning.ambiguities:
            slow_reasons.append("ambiguity")
        if relevant_goals:
            slow_reasons.append("active_goal")
        if memory_state.conflicts:
            slow_reasons.append("memory_conflict")
        if fast_pass.urgency in {"high", "critical"}:
            slow_reasons.append("heightened_urgency")
        mode = "slow" if slow_reasons or not fast_pass.can_answer_directly else "fast"
        uncertainty = _score_clamp((1.0 - float(fast_pass.confidence)) + (0.12 if meaning.ambiguities else 0.0) + (0.1 if memory_state.conflicts else 0.0))
        options = ["direct_answer", "ask_clarifying_question", "handoff_to_existing_reply_generation"]
        if memory_gate.should_retrieve:
            options.append("consult_summarized_memory")
        if fast_pass.needs_tools:
            options.append("use_existing_tool_runtime")
        strategy = "direct_answer" if mode == "fast" else "contextual_slow_handoff"
        safety_flags: List[str] = []
        if fast_pass.needs_tools:
            safety_flags.append("tool_policy_required")
        if fast_pass.urgency in {"high", "critical"}:
            safety_flags.append("heightened_urgency")
        return BrainReasoningState(
            mode=mode,  # type: ignore[arg-type]
            problem_type=f"{fast_pass.domain_guess}:{fast_pass.intent_guess}",
            options=list(dict.fromkeys(options)),
            chosen_strategy=strategy,
            uncertainty=uncertainty,
            safety_flags=safety_flags,
        )

    def _response_plan(
        self,
        *,
        input_state: BrainInputState,
        fast_pass: FastPassState,
        meaning: MeaningState,
        reasoning: BrainReasoningState,
        memory_gate: MemoryGateState,
        memory_state: BrainMemoryState,
        curiosity: Optional[BrainCuriosityState] = None,
    ) -> BrainResponsePlan:
        ask_question = bool(meaning.ambiguities and reasoning.mode == "slow" and float(reasoning.uncertainty) >= 0.55)
        tool_actions: List[Dict[str, Any]] = []
        if fast_pass.needs_tools:
            tool_actions.append({"action": "handoff_to_existing_tool_runtime", "status": "planned_by_orchestrator"})
        target = "simple direct answer" if reasoning.mode == "fast" else "context-aware Stark reply"
        depth = "brief" if reasoning.mode == "fast" else "normal"
        if _contains_any(input_state.normalized_text.lower(), ["detail", "full analysis", "explain deeply"]):
            depth = "detailed"
        if curiosity and curiosity.should_think_now and depth == "brief":
            depth = "normal"
        tone = "calm"
        if any(tag in fast_pass.emotion_detected for tag in ("frustrated", "scared", "stressed", "disappointed")):
            tone = "steady"
        elif "excited" in fast_pass.emotion_detected:
            tone = "upbeat"
        elif curiosity and (curiosity.should_think_now or curiosity.curiosity_score >= 0.45):
            tone = "curious"
        return BrainResponsePlan(
            target_outcome=target,
            tone=tone,
            depth=depth,
            format="prose",
            ask_question=ask_question,
            tool_actions=tool_actions,
            memory_writeback=self._should_propose_memory_writeback(
                input_state=input_state,
                fast_pass=fast_pass,
                meaning=meaning,
                memory_gate=memory_gate,
                memory_state=memory_state,
            ),
        )

    def _action_decision(
        self,
        *,
        fast_pass: FastPassState,
        meaning: MeaningState,
        reasoning: BrainReasoningState,
        memory_gate: MemoryGateState,
        response_plan: BrainResponsePlan,
    ) -> BrainActionDecision:
        if response_plan.ask_question:
            return BrainActionDecision(
                next_action="ask_user",
                requires_orchestrator=True,
                reason="meaning_parse_found_ambiguity",
                safety_flags=list(reasoning.safety_flags or []),
                evidence_needed=["user_clarification"],
            )
        if fast_pass.needs_tools or list(response_plan.tool_actions or []):
            return BrainActionDecision(
                next_action="call_tool",
                body_subsystem="tool_runtime",
                requires_orchestrator=True,
                reason="tool_intent_detected",
                safety_flags=list(dict.fromkeys([*(reasoning.safety_flags or []), "tool_policy_required"])),
                evidence_needed=["tool_result", "review_status"],
            )
        if memory_gate.should_retrieve and reasoning.mode == "slow":
            return BrainActionDecision(
                next_action="retrieve_more_context",
                requires_orchestrator=True,
                reason=str(memory_gate.reason or "memory_relevance"),
                safety_flags=list(reasoning.safety_flags or []),
                evidence_needed=["memory_context"],
            )
        if response_plan.memory_writeback:
            return BrainActionDecision(
                next_action="propose_memory",
                requires_orchestrator=True,
                reason="memory_writeback_candidate",
                safety_flags=list(reasoning.safety_flags or []),
                evidence_needed=["memory_approval"],
            )
        return BrainActionDecision(
            next_action="answer_directly",
            requires_orchestrator=False,
            reason="fast_or_contextual_reply_ready",
            safety_flags=list(reasoning.safety_flags or []),
            evidence_needed=[],
        )

    def _should_propose_memory_writeback(
        self,
        *,
        input_state: BrainInputState,
        fast_pass: FastPassState,
        meaning: MeaningState,
        memory_gate: MemoryGateState,
        memory_state: BrainMemoryState,
    ) -> bool:
        lower = str(input_state.normalized_text or "").lower()
        if _contains_any(lower, ["do not remember", "don't remember", "do not store", "don't store", "not to remember"]):
            return False
        if _contains_any(lower, ["what do you remember", "do you remember", "recall", "remind me"]):
            return False
        if len(_token_set(lower)) <= 3 and fast_pass.can_answer_directly:
            return False
        explicit = _contains_any(lower, ["remember that", "remember this", "save this", "store this", "note this"])
        durable_preference = _contains_any(lower, ["i prefer", "my preference", "call me"])
        durable_project = _contains_any(lower, ["decided", "decision", "project will", "milestone is", "release is", "task is blocked"])
        recurring_error = _contains_any(lower, ["recurring error", "keeps failing", "failed again", "same crash", "regression"])
        if explicit or durable_preference or durable_project or recurring_error:
            return True
        if memory_state.source_breakdown and set(memory_state.source_breakdown) == {"obsidian_vault"}:
            return False
        return False

    def _curiosity_state(
        self,
        *,
        input_state: BrainInputState,
        fast_pass: FastPassState,
        meaning: MeaningState,
        memory_gate: MemoryGateState,
        memory_state: BrainMemoryState,
        reasoning: BrainReasoningState,
        relevant_goals: Sequence[GoalSnapshot],
    ) -> BrainCuriosityState:
        text = str(input_state.normalized_text or "")
        lower = text.lower()
        triggers: List[str] = []

        def add_trigger(trigger: str) -> None:
            if trigger and trigger not in triggers:
                triggers.append(trigger)

        if "curious" in fast_pass.emotion_detected or _contains_any(
            lower,
            ["why", "how does", "how can", "what if", "wonder", "curious", "explore", "understand", "learn"],
        ):
            add_trigger("user_curiosity")
        if _contains_any(lower, ["think", "reason", "reflect", "deeply", "figure out", "investigate"]):
            add_trigger("thinking_requested")
        if meaning.ambiguities:
            add_trigger("ambiguity")
        if memory_gate.should_retrieve:
            add_trigger("memory_continuity")
        if memory_state.conflicts:
            add_trigger("memory_conflict")
        if relevant_goals:
            add_trigger("active_goal")
        if any(item.unresolved for item in memory_state.retrieved) or "unresolved" in fast_pass.emotion_detected:
            add_trigger("unresolved_thread")
        if _contains_any(lower, ["improve", "smarter", "deepen", "harder", "architecture", "design", "system"]):
            add_trigger("improvement_question")
        if fast_pass.needs_tools:
            add_trigger("tool_implication")

        question_candidates: List[str] = []
        if "ambiguity" in triggers:
            question_candidates.append("What missing constraint would change the best answer?")
        if "memory_continuity" in triggers:
            question_candidates.append("Which summarized memory matters most for this turn?")
        if "active_goal" in triggers:
            question_candidates.append("How does this turn move the active goal forward?")
        if "improvement_question" in triggers:
            question_candidates.append("What small architecture change would make Stark more useful without widening scope?")
        if "user_curiosity" in triggers:
            question_candidates.append("What explanation would give the user the most leverage?")
        if "thinking_requested" in triggers:
            question_candidates.append("What should Stark think through before answering?")
        if "tool_implication" in triggers:
            question_candidates.append("What evidence is needed before taking action?")

        questions = list(dict.fromkeys(question_candidates))[:3]
        learning_value = 0.0
        if triggers:
            learning_value = 0.28
            if any(t in triggers for t in ("memory_continuity", "memory_conflict", "unresolved_thread")):
                learning_value += 0.22
            if any(t in triggers for t in ("active_goal", "improvement_question", "thinking_requested")):
                learning_value += 0.2
            if "tool_implication" in triggers:
                learning_value += 0.1

        user_relevance = 0.0
        if triggers:
            user_relevance = 0.34
            if any(t in triggers for t in ("user_curiosity", "thinking_requested", "improvement_question")):
                user_relevance += 0.25
            if relevant_goals or memory_gate.should_retrieve:
                user_relevance += 0.22
            if fast_pass.domain_guess in {"project", "goals", "memory"}:
                user_relevance += 0.12

        trigger_weight = min(0.48, 0.12 * len(triggers))
        score = _score_clamp(0.04 + trigger_weight + (0.2 * _score_clamp(learning_value)) + (0.16 * _score_clamp(user_relevance)))
        if fast_pass.can_answer_directly and not any(t in triggers for t in ("user_curiosity", "thinking_requested", "improvement_question")):
            score = min(score, 0.22)

        think_mode = "none"
        should_think_now = False
        defer_reason: Optional[str] = None
        if score >= 0.3 and user_relevance >= 0.25:
            if reasoning.mode == "slow" or fast_pass.needs_tools or memory_gate.should_retrieve or float(reasoning.uncertainty) >= 0.55:
                think_mode = "deep_deferred"
                defer_reason = "preserve_user_task_flow"
            else:
                think_mode = "quick"
                should_think_now = True

        if think_mode == "deep_deferred" and _contains_any(lower, ["think now", "quickly think", "one quick thought"]):
            think_mode = "quick"
            should_think_now = True
            defer_reason = None

        safety_limits = [
            "stay_attached_to_user_goal",
            "do_not_browse_or_use_tools_from_curiosity_alone",
            "do_not_store_curiosity_as_memory_without_writeback_gate",
            "keep_questions_bounded",
        ]
        next_probe = questions[0] if questions else None
        reflection_seed = None
        if questions and score >= 0.4:
            reflection_seed = _bounded_summary(" ".join(questions), max_len=220)
        return BrainCuriosityState(
            curiosity_score=_score_clamp(score),
            triggers=triggers,
            questions=questions,
            think_mode=think_mode,  # type: ignore[arg-type]
            should_think_now=bool(should_think_now),
            learning_value=_score_clamp(learning_value),
            user_relevance=_score_clamp(user_relevance),
            next_probe=next_probe,
            defer_reason=defer_reason,
            safety_limits=safety_limits,
            reflection_seed=reflection_seed,
        )

    def _memory_update_proposal(
        self,
        *,
        timestamp: str,
        turn_id: str,
        event_id_user: Optional[str],
        input_state: BrainInputState,
        fast_pass: FastPassState,
        meaning: MeaningState,
        relevant_goals: Sequence[GoalSnapshot],
    ) -> MemoryWritebackProposal:
        text = input_state.normalized_text
        lower = text.lower()
        tags = list(fast_pass.emotion_detected or ["neutral"])
        should_store = False
        memory_class: Optional[str] = None
        if _contains_any(lower, ["do not remember", "don't remember", "do not store", "don't store", "not to remember"]):
            return MemoryWritebackProposal(
                should_store=False,
                memory_class=None,
                summary_object=None,
                importance=0.0,
                emotion_tags=tags,  # type: ignore[arg-type]
                expiry_policy="no_store",
                graph_links=[],
            )
        if _contains_any(lower, ["what do you remember", "do you remember", "recall", "remind me"]):
            return MemoryWritebackProposal(
                should_store=False,
                memory_class=None,
                summary_object=None,
                importance=0.0,
                emotion_tags=tags,  # type: ignore[arg-type]
                expiry_policy="no_store",
                graph_links=[],
            )
        if _contains_any(lower, ["remember", "save this", "i prefer", "my preference", "call me"]):
            should_store = True
            memory_class = "preference" if _contains_any(lower, ["prefer", "preference", "call me"]) else "fact"
        if _contains_any(lower, ["goal", "task", "todo", "project", "milestone", "release", "unresolved", "blocked"]):
            should_store = True
            memory_class = memory_class or "project"
        if _contains_any(lower, ["bug", "error", "failure", "failed", "crash", "regression", "broken"]):
            should_store = True
            memory_class = "error"
        if _contains_any(lower, ["procedure", "workflow", "checklist", "steps to"]):
            should_store = True
            memory_class = memory_class or "research"
        if any(tag in tags for tag in ("urgent", "important", "unresolved", "frustrated", "stressed", "scared", "angry")):
            should_store = True
            memory_class = memory_class or "project"
        if fast_pass.can_answer_directly and not _contains_any(lower, ["remember", "prefer", "goal", "task"]):
            should_store = False
            memory_class = None

        importance = 0.0
        if should_store:
            importance = 0.58
            if any(tag in tags for tag in ("important", "urgent", "unresolved")):
                importance += 0.2
            if relevant_goals:
                importance += 0.12
            if memory_class in {"error", "rule", "preference"}:
                importance += 0.08
        importance = _score_clamp(importance)

        summary_obj: Optional[SummarizedMemoryObject] = None
        if should_store and memory_class in MEMORY_KINDS:
            goal_ids = [g.goal_id for g in relevant_goals]
            source_refs = list(input_state.source_refs)
            if event_id_user:
                source_refs.append(f"sqlite:events/{event_id_user}")
            summary_obj = SummarizedMemoryObject(
                memory_id=f"proposal:{turn_id}",
                memory_type=memory_class,  # type: ignore[arg-type]
                summary=_bounded_summary(str(meaning.user_goal or text), max_len=280),
                source_kind="memory_writeback_proposal",
                source_refs=list(dict.fromkeys(source_refs)),
                importance=importance,
                emotion_tags=tags,  # type: ignore[arg-type]
                created_at=timestamp,
                updated_at=timestamp,
                related_goal_ids=goal_ids,
                relevance_score=1.0,
                unresolved=("unresolved" in tags or _contains_any(lower, ["unresolved", "blocked", "todo"])),
                expiry_policy="review_for_promotion",
            )
        return MemoryWritebackProposal(
            should_store=bool(should_store),
            memory_class=(memory_class if memory_class in MEMORY_KINDS else None),  # type: ignore[arg-type]
            summary_object=summary_obj,
            importance=importance,
            emotion_tags=tags,  # type: ignore[arg-type]
            expiry_policy=("review_for_promotion" if should_store else "no_store"),
            graph_links=[{"kind": "related_goal", "target_id": g.goal_id} for g in relevant_goals],
        )

    def _draft_handoff_note(self, *, reasoning: BrainReasoningState, response_plan: BrainResponsePlan) -> str:
        return _bounded_summary(
            f"Brain M1 selected {reasoning.mode} reasoning and prepared a {response_plan.depth} {response_plan.format} reply handoff.",
            max_len=160,
        )
