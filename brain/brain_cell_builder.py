from __future__ import annotations

"""Autonomous Markdown/JSON brain-cell builder for Stark.

This module is intentionally stdlib-only so it can run in two modes:

1. inside Stark as ``assistant.brain.brain_cell_builder``; and
2. standalone from the extracted ``brain/`` folder while developing patches.

It does not call tools, browse, execute plans, or mutate Stark runtime state. Its only
side effect is building a local wiki-like brain-cell vault made of Markdown cells and
JSON graph indexes.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = "stark.brain_cells.v1"
DEFAULT_BRAIN_DIR = ".stark_brain_cells"
MAX_SUMMARY_CHARS = 900
MAX_TITLE_CHARS = 88
MAX_CONCEPTS = 18
MAX_LINKS_PER_CELL = 12

STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "all",
    "also",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "for",
    "from",
    "had",
    "has",
    "have",
    "help",
    "her",
    "here",
    "him",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "let",
    "like",
    "make",
    "me",
    "more",
    "my",
    "need",
    "no",
    "not",
    "now",
    "of",
    "on",
    "one",
    "or",
    "our",
    "out",
    "please",
    "run",
    "same",
    "so",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "this",
    "to",
    "too",
    "up",
    "use",
    "using",
    "want",
    "was",
    "we",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "you",
    "your",
}


@dataclass
class BrainCell:
    id: str
    slug: str
    title: str
    kind: str
    summary: str
    concepts: List[str] = field(default_factory=list)
    source_refs: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    activation_count: int = 1
    last_activated_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    markdown_path: str = ""


@dataclass
class BrainLink:
    source_id: str
    target_id: str
    relation: str
    weight: float = 0.5
    evidence: str = ""
    created_at: str = ""
    updated_at: str = ""
    activation_count: int = 1


@dataclass
class BrainCellBuildResult:
    created: List[BrainCell] = field(default_factory=list)
    updated: List[BrainCell] = field(default_factory=list)
    links_created: List[BrainLink] = field(default_factory=list)
    links_strengthened: List[BrainLink] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def bounded(text: Any, *, max_chars: int) -> str:
    value = clean_ws(text)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def slugify(text: str, *, fallback: str = "cell") -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:72].strip("-") or fallback


def stable_cell_id(title: str, kind: str, summary: str) -> str:
    basis = f"{kind}\n{clean_ws(title).lower()}\n{clean_ws(summary).lower()[:360]}"
    return "cell_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def stable_source_ref(path_or_ref: Any) -> str:
    value = str(path_or_ref or "").strip()
    if not value:
        return "manual:unknown"
    if ":" in value and not os.path.exists(value):
        return value
    return f"file:{Path(value).as_posix()}"


def unique_keep_order(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in values:
        value = clean_ws(raw)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def extract_concepts(text: str, extra_terms: Optional[Iterable[Any]] = None, *, limit: int = MAX_CONCEPTS) -> List[str]:
    """Extract small deterministic concepts without an LLM dependency."""

    text = str(text or "")
    candidates: List[str] = []

    # Explicit tags are treated as high-signal concepts.
    candidates.extend(m.group(1).replace("_", " ") for m in re.finditer(r"#([A-Za-z][A-Za-z0-9_\-]{2,})", text))

    # Wiki links from existing Markdown should also become concepts.
    candidates.extend(m.group(1).split("|", 1)[-1] for m in re.finditer(r"\[\[([^\]]+)\]\]", text))

    # Title-case phrases catch project/module names, e.g. Brain Cell Builder.
    phrase_pattern = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,4})\b")
    candidates.extend(m.group(1) for m in phrase_pattern.finditer(text))

    # Frequent meaningful tokens provide low-cost recall anchors.
    counts: Dict[str, int] = {}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text.lower()):
        token = token.strip("_- ")
        if len(token) < 3 or token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    for token, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if count >= 2 or token in {"stark", "brain", "memory", "goal", "module", "cell", "cells", "wiki", "json", "markdown"}:
            candidates.append(token)

    if extra_terms:
        candidates.extend(str(item) for item in extra_terms if str(item or "").strip())

    normalized: List[str] = []
    for candidate in candidates:
        cleaned = clean_ws(candidate).strip("#[](){}.,:;!?")
        if not cleaned:
            continue
        if cleaned.lower() in STOPWORDS:
            continue
        if len(cleaned) > 64:
            cleaned = cleaned[:64].rstrip()
        normalized.append(cleaned)
    return unique_keep_order(normalized)[:limit]


def title_from_text(text: str, *, fallback: str = "Brain Cell") -> str:
    text = str(text or "").strip()
    for line in text.splitlines():
        stripped = line.strip().strip("# ").strip()
        if stripped:
            return bounded(stripped, max_chars=MAX_TITLE_CHARS)
    sentence = re.split(r"(?<=[.!?])\s+", clean_ws(text), maxsplit=1)[0]
    return bounded(sentence or fallback, max_chars=MAX_TITLE_CHARS)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def as_mapping(raw: Any) -> Dict[str, Any]:
    if hasattr(raw, "model_dump"):
        try:
            return dict(raw.model_dump(mode="json"))
        except Exception:
            pass
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def compact_json_summary(value: Any, *, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    try:
        return bounded(json.dumps(value, ensure_ascii=False, sort_keys=True), max_chars=max_chars)
    except Exception:
        return bounded(str(value), max_chars=max_chars)


class BrainCellBuilder:
    """Builds and strengthens a local Markdown/JSON knowledge graph.

    The builder uses a simple Hebbian-style rule: when a new observation shares
    concepts with an existing cell, Stark creates or strengthens a link instead of
    keeping isolated notes. Repeated co-activation raises the link weight and the
    cells' activation counts.
    """

    def __init__(self, brain_dir: str | Path = DEFAULT_BRAIN_DIR) -> None:
        self.root = Path(brain_dir)
        self.cells_dir = self.root / "cells"
        self.graph_dir = self.root / "graph"
        self.inbox_dir = self.root / "inbox"
        self.processed_dir = self.root / "processed"
        self.cells_path = self.graph_dir / "cells.json"
        self.links_path = self.graph_dir / "links.json"
        self.index_path = self.graph_dir / "index.json"
        self._cells: Dict[str, BrainCell] = {}
        self._links: Dict[Tuple[str, str, str], BrainLink] = {}
        self._load()

    def bootstrap(self) -> None:
        self.cells_dir.mkdir(parents=True, exist_ok=True)
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self._save()

    def ingest_text(
        self,
        text: str,
        *,
        title: Optional[str] = None,
        kind: str = "observation",
        source_refs: Optional[Iterable[Any]] = None,
        concepts: Optional[Iterable[Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> BrainCellBuildResult:
        now = utc_now_iso()
        text = str(text or "").strip()
        result = BrainCellBuildResult()
        if not text:
            result.skipped.append("empty_text")
            return result

        clean_kind = slugify(kind, fallback="observation").replace("-", "_")
        summary = bounded(text, max_chars=MAX_SUMMARY_CHARS)
        cell_title = title_from_text(title or text, fallback="Brain Cell")
        cell_id = stable_cell_id(cell_title, clean_kind, summary)
        slug = self._unique_slug(slugify(cell_title, fallback=cell_id), cell_id)
        source_list = unique_keep_order(stable_source_ref(ref) for ref in list(source_refs or []))
        extracted_concepts = extract_concepts(text, list(concepts or []))

        existing = self._cells.get(cell_id)
        if existing is None:
            cell = BrainCell(
                id=cell_id,
                slug=slug,
                title=cell_title,
                kind=clean_kind,
                summary=summary,
                concepts=extracted_concepts,
                source_refs=source_list,
                aliases=[],
                created_at=now,
                updated_at=now,
                activation_count=1,
                last_activated_at=now,
                metadata=dict(metadata or {}),
                markdown_path=self._markdown_relpath(slug),
            )
            self._cells[cell.id] = cell
            result.created.append(cell)
        else:
            cell = existing
            cell.summary = self._merge_summary(cell.summary, summary)
            cell.concepts = unique_keep_order([*cell.concepts, *extracted_concepts])[:MAX_CONCEPTS]
            cell.source_refs = unique_keep_order([*cell.source_refs, *source_list])
            cell.updated_at = now
            cell.last_activated_at = now
            cell.activation_count += 1
            cell.metadata = {**cell.metadata, **dict(metadata or {})}
            result.updated.append(cell)

        related = self._find_related_cells(cell, text=text)
        for target, relation, weight, evidence in related:
            link, created = self._upsert_link(
                source_id=cell.id,
                target_id=target.id,
                relation=relation,
                weight=weight,
                evidence=evidence,
                now=now,
            )
            if created:
                result.links_created.append(link)
            else:
                result.links_strengthened.append(link)

        self._save()
        return result

    def ingest_json(
        self,
        payload: Mapping[str, Any],
        *,
        source_ref: str = "json:manual",
    ) -> BrainCellBuildResult:
        data = as_mapping(payload)
        if self._looks_like_thought_state(data):
            return self.ingest_thought_state(data, source_ref=source_ref)
        title = clean_ws(data.get("title") or data.get("name") or data.get("id") or "JSON Observation")
        concepts = []
        for key in ("concepts", "tags", "entities", "keywords"):
            raw = data.get(key)
            if isinstance(raw, list):
                concepts.extend(raw)
        return self.ingest_text(
            compact_json_summary(data),
            title=title,
            kind="json_observation",
            source_refs=[source_ref],
            concepts=concepts,
            metadata={"source_type": "json"},
        )

    def ingest_thought_state(
        self,
        state: Mapping[str, Any],
        *,
        source_ref: str = "thought_state:manual",
    ) -> BrainCellBuildResult:
        data = as_mapping(state)
        now_result = BrainCellBuildResult()
        text_parts: List[str] = []
        concepts: List[str] = []
        source_refs = [source_ref]

        input_state = as_mapping(data.get("input"))
        meaning = as_mapping(data.get("meaning"))
        curiosity = as_mapping(data.get("curiosity"))
        memory_update = as_mapping(data.get("memory_update"))
        reflection = as_mapping(data.get("reflection_state"))
        goal_state = as_mapping(data.get("goal_state"))

        raw_text = clean_ws(input_state.get("normalized_text") or input_state.get("raw_text"))
        if raw_text:
            text_parts.append(f"Input: {raw_text}")
        if meaning.get("user_goal"):
            text_parts.append(f"User goal: {meaning.get('user_goal')}")
        if meaning.get("assistant_goal"):
            text_parts.append(f"Assistant goal: {meaning.get('assistant_goal')}")
        if meaning.get("entities"):
            concepts.extend(list(meaning.get("entities") or []))
            text_parts.append("Entities: " + ", ".join(str(v) for v in meaning.get("entities") or []))
        if curiosity.get("questions"):
            text_parts.append("Curiosity questions: " + "; ".join(str(v) for v in curiosity.get("questions") or []))
        if reflection.get("next_best_step"):
            text_parts.append(f"Reflection next step: {reflection.get('next_best_step')}")
        if goal_state.get("current_goal"):
            concepts.append(str(goal_state.get("current_goal")))
            text_parts.append(f"Current goal: {goal_state.get('current_goal')}")

        summary_object = as_mapping(memory_update.get("summary_object"))
        if summary_object.get("summary"):
            text_parts.append(f"Memory candidate: {summary_object.get('summary')}")
        if summary_object.get("memory_type"):
            concepts.append(str(summary_object.get("memory_type")))

        for ref in input_state.get("source_refs") or []:
            source_refs.append(str(ref))

        title = clean_ws(meaning.get("user_goal") or raw_text or data.get("turn_id") or "Thought State")
        text = "\n".join(part for part in text_parts if clean_ws(part)) or compact_json_summary(data)
        result = self.ingest_text(
            text,
            title=title,
            kind="thought",
            source_refs=source_refs,
            concepts=concepts,
            metadata={
                "source_type": "thought_state",
                "turn_id": data.get("turn_id"),
                "schema_version": data.get("schema_version"),
                "memory_writeback": bool(memory_update.get("should_store")),
            },
        )
        now_result.created.extend(result.created)
        now_result.updated.extend(result.updated)
        now_result.links_created.extend(result.links_created)
        now_result.links_strengthened.extend(result.links_strengthened)
        now_result.skipped.extend(result.skipped)

        # Reflection/memory candidates become separate cells so Stark can grow a
        # graph rather than one giant turn log.
        candidate_items: List[Any] = []
        if summary_object.get("summary"):
            candidate_items.append(summary_object)
        for item in reflection.get("memory_candidates") or []:
            candidate_items.append(item)

        for idx, candidate in enumerate(candidate_items):
            cand = as_mapping(candidate)
            cand_text = clean_ws(cand.get("summary") or cand.get("text") or cand.get("content"))
            if not cand_text:
                continue
            cand_title = title_from_text(cand.get("title") or cand_text, fallback=f"Memory Candidate {idx + 1}")
            cand_kind = clean_ws(cand.get("memory_type") or cand.get("kind") or "memory_candidate")
            candidate_result = self.ingest_text(
                cand_text,
                title=cand_title,
                kind=cand_kind,
                source_refs=source_refs,
                concepts=[*concepts, cand_kind],
                metadata={"source_type": "thought_state_candidate", "turn_id": data.get("turn_id")},
            )
            now_result.created.extend(candidate_result.created)
            now_result.updated.extend(candidate_result.updated)
            now_result.links_created.extend(candidate_result.links_created)
            now_result.links_strengthened.extend(candidate_result.links_strengthened)
            now_result.skipped.extend(candidate_result.skipped)
        return now_result

    def process_inbox(self, *, move_processed: bool = True) -> BrainCellBuildResult:
        self.bootstrap()
        result = BrainCellBuildResult()
        files = sorted(
            p for p in self.inbox_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".txt", ".md", ".json"}
        )
        if not files:
            result.skipped.append("inbox_empty")
            return result
        for path in files:
            try:
                if path.suffix.lower() == ".json":
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    file_result = self.ingest_json(payload if isinstance(payload, Mapping) else {"payload": payload}, source_ref=stable_source_ref(path))
                else:
                    file_result = self.ingest_text(
                        path.read_text(encoding="utf-8"),
                        title=path.stem.replace("_", " ").replace("-", " "),
                        kind="markdown_note" if path.suffix.lower() == ".md" else "text_note",
                        source_refs=[stable_source_ref(path)],
                        metadata={"source_type": "inbox_file", "filename": path.name},
                    )
                result.created.extend(file_result.created)
                result.updated.extend(file_result.updated)
                result.links_created.extend(file_result.links_created)
                result.links_strengthened.extend(file_result.links_strengthened)
                result.skipped.extend(file_result.skipped)
                if move_processed:
                    self._move_processed(path)
            except Exception as exc:  # pragma: no cover - defensive CLI path
                result.skipped.append(f"{path.name}: {exc}")
        return result

    def _load(self) -> None:
        cells_payload = read_json(self.cells_path, {"cells": []})
        links_payload = read_json(self.links_path, {"links": []})
        self._cells = {}
        for raw in cells_payload.get("cells", []) if isinstance(cells_payload, Mapping) else []:
            if not isinstance(raw, Mapping):
                continue
            try:
                cell = BrainCell(**{**asdict(BrainCell(id="", slug="", title="", kind="", summary="")), **dict(raw)})
            except TypeError:
                continue
            if cell.id:
                self._cells[cell.id] = cell
        self._links = {}
        for raw in links_payload.get("links", []) if isinstance(links_payload, Mapping) else []:
            if not isinstance(raw, Mapping):
                continue
            try:
                link = BrainLink(**{**asdict(BrainLink(source_id="", target_id="", relation="")), **dict(raw)})
            except TypeError:
                continue
            if link.source_id and link.target_id and link.source_id != link.target_id:
                self._links[(link.source_id, link.target_id, link.relation)] = link

    def _save(self) -> None:
        self.bootstrap_dirs_only()
        cells = sorted(self._cells.values(), key=lambda cell: (cell.kind, cell.title.lower(), cell.id))
        links = sorted(self._links.values(), key=lambda link: (link.source_id, link.target_id, link.relation))
        atomic_write_json(
            self.cells_path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now_iso(),
                "cells": [asdict(cell) for cell in cells],
            },
        )
        atomic_write_json(
            self.links_path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now_iso(),
                "links": [asdict(link) for link in links],
            },
        )
        atomic_write_json(self.index_path, self._build_index(cells, links))
        self._write_markdown_cells(cells, links)

    def bootstrap_dirs_only(self) -> None:
        self.cells_dir.mkdir(parents=True, exist_ok=True)
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

    def _build_index(self, cells: Sequence[BrainCell], links: Sequence[BrainLink]) -> Dict[str, Any]:
        concept_index: Dict[str, List[str]] = {}
        slug_index: Dict[str, str] = {}
        title_index: Dict[str, str] = {}
        for cell in cells:
            slug_index[cell.slug] = cell.id
            title_index[cell.title.lower()] = cell.id
            for concept in cell.concepts:
                key = concept.lower()
                concept_index.setdefault(key, [])
                if cell.id not in concept_index[key]:
                    concept_index[key].append(cell.id)
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "cell_count": len(cells),
            "link_count": len(links),
            "slug_index": slug_index,
            "title_index": title_index,
            "concept_index": concept_index,
        }

    def _write_markdown_cells(self, cells: Sequence[BrainCell], links: Sequence[BrainLink]) -> None:
        by_id = {cell.id: cell for cell in cells}
        outgoing: Dict[str, List[BrainLink]] = {}
        incoming: Dict[str, List[BrainLink]] = {}
        for link in links:
            outgoing.setdefault(link.source_id, []).append(link)
            incoming.setdefault(link.target_id, []).append(link)
        for cell in cells:
            content = self._render_cell_markdown(
                cell,
                outgoing=outgoing.get(cell.id, []),
                incoming=incoming.get(cell.id, []),
                by_id=by_id,
            )
            atomic_write_text(self.root / cell.markdown_path, content)

    def _render_cell_markdown(
        self,
        cell: BrainCell,
        *,
        outgoing: Sequence[BrainLink],
        incoming: Sequence[BrainLink],
        by_id: Mapping[str, BrainCell],
    ) -> str:
        frontmatter = {
            "id": cell.id,
            "schema_version": SCHEMA_VERSION,
            "kind": cell.kind,
            "title": cell.title,
            "slug": cell.slug,
            "created_at": cell.created_at,
            "updated_at": cell.updated_at,
            "activation_count": cell.activation_count,
            "last_activated_at": cell.last_activated_at,
            "concepts": cell.concepts,
            "source_refs": cell.source_refs,
        }
        lines: List[str] = ["---"]
        for key, value in frontmatter.items():
            if isinstance(value, list):
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {json.dumps(item, ensure_ascii=False)}")
            else:
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        lines.extend(["---", "", f"# {cell.title}", "", cell.summary, ""])
        if cell.concepts:
            lines.extend(["## Concepts", ""])
            lines.append(", ".join(f"`{concept}`" for concept in cell.concepts))
            lines.append("")
        if outgoing:
            lines.extend(["## Links", ""])
            for link in sorted(outgoing, key=lambda item: (-item.weight, item.relation, item.target_id))[:MAX_LINKS_PER_CELL]:
                target = by_id.get(link.target_id)
                if not target:
                    continue
                lines.append(f"- [[{target.slug}|{target.title}]] — {link.relation}, weight `{link.weight:.2f}`")
            lines.append("")
        if incoming:
            lines.extend(["## Backlinks", ""])
            for link in sorted(incoming, key=lambda item: (-item.weight, item.relation, item.source_id))[:MAX_LINKS_PER_CELL]:
                source = by_id.get(link.source_id)
                if not source:
                    continue
                lines.append(f"- [[{source.slug}|{source.title}]] — {link.relation}, weight `{link.weight:.2f}`")
            lines.append("")
        lines.extend(
            [
                "## Source refs",
                "",
                *(f"- `{ref}`" for ref in cell.source_refs),
                "",
                "<!-- Generated by brain.brain_cell_builder. Edit carefully; graph JSON is canonical. -->",
                "",
            ]
        )
        return "\n".join(lines)

    def _find_related_cells(self, cell: BrainCell, *, text: str) -> List[Tuple[BrainCell, str, float, str]]:
        related: List[Tuple[BrainCell, str, float, str]] = []
        cell_concepts = {concept.lower() for concept in cell.concepts}
        text_lower = text.lower()
        for other in self._cells.values():
            if other.id == cell.id:
                continue
            other_concepts = {concept.lower() for concept in other.concepts}
            shared = sorted(cell_concepts & other_concepts)
            exact_title = other.title.lower() in text_lower or other.slug.replace("-", " ") in text_lower
            if not shared and not exact_title:
                continue
            relation = "mentions" if exact_title else "related_to"
            if shared and len(shared) >= 3:
                relation = "co_activates"
            weight = 0.44 + min(0.36, 0.09 * len(shared)) + (0.16 if exact_title else 0.0)
            evidence = ", ".join(shared[:5]) or other.title
            related.append((other, relation, min(1.0, weight), evidence))
        related.sort(key=lambda item: (-item[2], item[0].title.lower()))
        return related[:MAX_LINKS_PER_CELL]

    def _upsert_link(
        self,
        *,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float,
        evidence: str,
        now: str,
    ) -> Tuple[BrainLink, bool]:
        if source_id == target_id:
            raise ValueError("brain links cannot point to the same cell")
        key = (source_id, target_id, relation)
        existing = self._links.get(key)
        if existing is None:
            link = BrainLink(
                source_id=source_id,
                target_id=target_id,
                relation=relation,
                weight=round(max(0.0, min(1.0, weight)), 3),
                evidence=bounded(evidence, max_chars=220),
                created_at=now,
                updated_at=now,
                activation_count=1,
            )
            self._links[key] = link
            self._activate_linked_cells(source_id, target_id, now=now)
            return link, True
        existing.weight = round(min(1.0, max(existing.weight, weight) + 0.05), 3)
        existing.evidence = self._merge_summary(existing.evidence, evidence, max_chars=220)
        existing.updated_at = now
        existing.activation_count += 1
        self._activate_linked_cells(source_id, target_id, now=now)
        return existing, False

    def _activate_linked_cells(self, source_id: str, target_id: str, *, now: str) -> None:
        # The source cell is already activated by the ingest path. The target
        # cell is activated here because Stark recalled it through association.
        for cell_id in (source_id, target_id):
            cell = self._cells.get(cell_id)
            if cell is None:
                continue
            if cell_id == target_id:
                cell.activation_count += 1
            cell.last_activated_at = now
            cell.updated_at = now

    def _merge_summary(self, old: str, new: str, *, max_chars: int = MAX_SUMMARY_CHARS) -> str:
        old_clean = clean_ws(old)
        new_clean = clean_ws(new)
        if not old_clean:
            return bounded(new_clean, max_chars=max_chars)
        if not new_clean or new_clean.lower() in old_clean.lower():
            return bounded(old_clean, max_chars=max_chars)
        merged = f"{old_clean}\n\nNew activation: {new_clean}"
        return bounded(merged, max_chars=max_chars)

    def _unique_slug(self, base: str, cell_id: str) -> str:
        existing = {cell.slug: cell.id for cell in self._cells.values()}
        if base not in existing or existing.get(base) == cell_id:
            return base
        suffix = cell_id.replace("cell_", "")[:6]
        return f"{base}-{suffix}"

    def _markdown_relpath(self, slug: str) -> str:
        return f"cells/{slug}.md"

    def _move_processed(self, path: Path) -> None:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        target = self.processed_dir / path.name
        if target.exists():
            target = self.processed_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
        shutil.move(str(path), str(target))

    def _looks_like_thought_state(self, data: Mapping[str, Any]) -> bool:
        if "input" in data and ("meaning" in data or "memory_update" in data or "curiosity" in data):
            return True
        return str(data.get("schema_version") or "").startswith("layered_brain")


def build_cells_from_thought_state(
    thought_state: Any,
    *,
    brain_dir: str | Path = DEFAULT_BRAIN_DIR,
    source_ref: str = "thought_state:runtime",
) -> BrainCellBuildResult:
    """Runtime-friendly helper for Stark's brain pipeline or session gateway."""

    builder = BrainCellBuilder(brain_dir)
    builder.bootstrap()
    return builder.ingest_thought_state(as_mapping(thought_state), source_ref=source_ref)


def build_cells_from_text(
    text: str,
    *,
    brain_dir: str | Path = DEFAULT_BRAIN_DIR,
    title: Optional[str] = None,
    kind: str = "observation",
    source_ref: str = "manual:text",
) -> BrainCellBuildResult:
    builder = BrainCellBuilder(brain_dir)
    builder.bootstrap()
    return builder.ingest_text(text, title=title, kind=kind, source_refs=[source_ref])


def result_to_summary(result: BrainCellBuildResult) -> Dict[str, Any]:
    return {
        "created": [cell.id for cell in result.created],
        "updated": [cell.id for cell in result.updated],
        "links_created": [asdict(link) for link in result.links_created],
        "links_strengthened": [asdict(link) for link in result.links_strengthened],
        "skipped": result.skipped,
    }


def _load_input_file(path: Path) -> Tuple[str, Any]:
    if path.suffix.lower() == ".json":
        return "json", json.loads(path.read_text(encoding="utf-8"))
    return "text", path.read_text(encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Stark brain cells as Markdown files plus JSON graph indexes.")
    parser.add_argument("--brain-dir", default=DEFAULT_BRAIN_DIR, help="Brain-cell vault directory to create/update.")
    parser.add_argument("--ingest-text", default=None, help="Text to turn into a brain cell.")
    parser.add_argument("--ingest-file", default=None, help="Path to .txt, .md, or .json input to ingest.")
    parser.add_argument("--title", default=None, help="Optional cell title for text/file ingestion.")
    parser.add_argument("--kind", default="observation", help="Cell kind, e.g. thought, project, memory_candidate.")
    parser.add_argument("--source-ref", default="manual:cli", help="Source reference stored in the cell metadata.")
    parser.add_argument("--process-inbox", action="store_true", help="Ingest files from <brain-dir>/inbox once.")
    parser.add_argument("--watch", action="store_true", help="Keep processing the inbox until interrupted.")
    parser.add_argument("--interval", type=float, default=5.0, help="Watch-mode polling interval in seconds.")
    parser.add_argument("--no-move-processed", action="store_true", help="Leave inbox files in place after ingestion.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    builder = BrainCellBuilder(args.brain_dir)
    builder.bootstrap()

    def emit(result: BrainCellBuildResult) -> None:
        print(json.dumps(result_to_summary(result), ensure_ascii=False, indent=2, sort_keys=True))

    if args.ingest_text is not None:
        emit(
            builder.ingest_text(
                args.ingest_text,
                title=args.title,
                kind=args.kind,
                source_refs=[args.source_ref],
                metadata={"source_type": "cli_text"},
            )
        )
        return 0

    if args.ingest_file:
        path = Path(args.ingest_file)
        kind, payload = _load_input_file(path)
        if kind == "json":
            emit(builder.ingest_json(payload if isinstance(payload, Mapping) else {"payload": payload}, source_ref=stable_source_ref(path)))
        else:
            emit(
                builder.ingest_text(
                    str(payload),
                    title=args.title or path.stem.replace("_", " ").replace("-", " "),
                    kind=args.kind if args.kind != "observation" else ("markdown_note" if path.suffix.lower() == ".md" else "text_note"),
                    source_refs=[stable_source_ref(path)],
                    metadata={"source_type": "cli_file", "filename": path.name},
                )
            )
        return 0

    if args.watch:
        try:
            while True:
                result = builder.process_inbox(move_processed=not args.no_move_processed)
                if result.created or result.updated or result.links_created or result.links_strengthened or result.skipped != ["inbox_empty"]:
                    emit(result)
                time.sleep(max(0.5, float(args.interval)))
        except KeyboardInterrupt:
            return 0

    if args.process_inbox:
        emit(builder.process_inbox(move_processed=not args.no_move_processed))
        return 0

    print(
        "Brain cell vault initialized. Add .txt/.md/.json files to "
        f"{builder.inbox_dir.as_posix()} or pass --ingest-text / --ingest-file.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
