from __future__ import annotations

"""Import an Obsidian vault into Stark brain cells.

This importer is intentionally stdlib-only and can run in two layouts:

1. standalone from an extracted ``brain/`` folder::

       python brain/import_obsidian_vault.py --vault /path/to/Vault --brain-dir .stark_brain_cells

2. inside Stark as ``assistant.brain.import_obsidian_vault``::

       python -m assistant.brain.import_obsidian_vault --vault /path/to/Vault --brain-dir .stark_brain_cells

It does not alter the source vault. It reads Markdown / JSON-like Obsidian files,
turns them into bounded learning chunks, stores source references, and lets
``BrainCellBuilder`` create Markdown cells plus JSON graph indexes.
"""

import argparse
import os
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
if __package__ and __package__.startswith("assistant.brain"):
    from assistant.brain.brain_cell_builder import BrainCellBuilder, BrainCellBuildResult, stable_source_ref, utc_now_iso
else:  # standalone extracted brain/ folder mode
    from brain_cell_builder import BrainCellBuilder, BrainCellBuildResult, stable_source_ref, utc_now_iso  # type: ignore

SUPPORTED_SUFFIXES = {".md", ".markdown", ".canvas", ".json"}
DEFAULT_IGNORE_DIRS = {
    ".git",
    ".obsidian",
    ".trash",
    ".stark_brain_cells",
    "node_modules",
    "__pycache__",
}
DEFAULT_MAX_CHARS_PER_CHUNK = 6000
DEFAULT_MAX_CHUNKS_PER_FILE = 80
DEFAULT_MAX_FILE_BYTES = 2_000_000


@dataclass
class VaultImportStats:
    vault_path: str
    brain_dir: str
    started_at: str
    finished_at: str = ""
    files_seen: int = 0
    files_imported: int = 0
    files_skipped: int = 0
    chunks_imported: int = 0
    cells_created: int = 0
    cells_updated: int = 0
    links_created: int = 0
    links_strengthened: int = 0
    skipped: List[str] = field(default_factory=list)
    imported_files: List[Dict[str, Any]] = field(default_factory=list)


def _clean_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _strip_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + 4 :].lstrip("\r\n")
    meta: Dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        if key:
            meta[key] = value
    return meta, body


def _title_from_note(path: Path, body: str, frontmatter: Mapping[str, Any], vault_root: Path) -> str:
    for key in ("title", "name", "aliases"):
        raw = str(frontmatter.get(key) or "").strip()
        if raw:
            return raw.strip("[]")[:120]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:120]
    return path.stem.replace("_", " ").replace("-", " ")[:120]


def _folder_concepts(path: Path, vault_root: Path) -> List[str]:
    try:
        rel = path.relative_to(vault_root)
    except ValueError:
        rel = path
    concepts: List[str] = []
    for part in rel.parts[:-1]:
        cleaned = re.sub(r"^\d+\s*-\s*", "", part).replace("_", " ").strip()
        if cleaned:
            concepts.append(cleaned)
    return concepts


def _kind_from_path(path: Path, vault_root: Path) -> str:
    rel = path.relative_to(vault_root).as_posix().lower()
    if "work instruction" in rel or "/wi-" in rel or rel.startswith("02 - work instructions/"):
        return "work_instruction"
    if "/forms/" in rel or rel.startswith("03 - forms/") or "/fm-" in rel:
        return "form_reference"
    if "playbook" in rel:
        return "role_playbook"
    if "checklist" in rel:
        return "checklist"
    if "quick card" in rel:
        return "quick_card"
    if "glossary" in rel:
        return "glossary"
    if "erp mapping" in rel:
        return "erp_mapping"
    if "customer" in rel:
        return "customer_knowledge"
    if "meeting" in rel:
        return "meeting_note"
    if "template" in rel:
        return "template"
    if path.suffix.lower() == ".canvas":
        return "obsidian_canvas"
    if path.suffix.lower() == ".json":
        return "json_note"
    return "obsidian_note"


def _iter_candidate_files(vault_root: Path, ignore_dirs: Iterable[str]) -> Iterator[Path]:
    ignore = set(ignore_dirs)
    candidates: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault_root):
        dirnames[:] = [name for name in dirnames if name not in ignore]
        current = Path(dirpath)
        for filename in filenames:
            path = current / filename
            if path.suffix.lower() in SUPPORTED_SUFFIXES:
                candidates.append(path)
    yield from sorted(candidates, key=lambda item: item.relative_to(vault_root).as_posix().lower())


def _markdown_heading_chunks(body: str, *, max_chars: int, max_chunks: int) -> List[Tuple[str, str]]:
    """Split Markdown into retrieval-friendly chunks.

    Returns ``[(heading, chunk_text)]``. Heading boundaries are respected when
    possible; very large sections are split by paragraph.
    """

    lines = body.splitlines()
    sections: List[Tuple[str, List[str]]] = []
    current_heading = "Overview"
    current_lines: List[str] = []

    heading_re = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
    for line in lines:
        match = heading_re.match(line)
        if match and current_lines:
            sections.append((current_heading, current_lines))
            current_heading = match.group(2).strip()[:120] or "Section"
            current_lines = [line]
        elif match:
            current_heading = match.group(2).strip()[:120] or "Section"
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))

    chunks: List[Tuple[str, str]] = []
    for heading, section_lines in sections or [("Overview", lines)]:
        section = "\n".join(section_lines).strip()
        if not section:
            continue
        if len(section) <= max_chars:
            chunks.append((heading, section))
            if len(chunks) >= max_chunks:
                break
            continue

        paragraphs = re.split(r"\n\s*\n", section)
        current: List[str] = []
        current_len = 0
        part = 1
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(para) > max_chars:
                # Fallback for transcripts or pasted long lines.
                for start in range(0, len(para), max_chars):
                    piece = para[start : start + max_chars].strip()
                    if piece:
                        chunks.append((f"{heading} — part {part}", piece))
                        part += 1
                        if len(chunks) >= max_chunks:
                            return chunks
                continue
            if current and current_len + len(para) + 2 > max_chars:
                chunks.append((f"{heading} — part {part}", "\n\n".join(current).strip()))
                part += 1
                current = [para]
                current_len = len(para)
                if len(chunks) >= max_chunks:
                    return chunks
            else:
                current.append(para)
                current_len += len(para) + 2
        if current:
            chunks.append((f"{heading} — part {part}" if part > 1 else heading, "\n\n".join(current).strip()))
            if len(chunks) >= max_chunks:
                return chunks
    return chunks[:max_chunks]


def _json_to_learning_text(path: Path, text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if path.suffix.lower() == ".canvas" and isinstance(payload, Mapping):
        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        lines = [f"# Obsidian canvas: {path.stem}", ""]
        if isinstance(nodes, list):
            lines.append("## Nodes")
            for node in nodes[:200]:
                if not isinstance(node, Mapping):
                    continue
                label = _clean_ws(node.get("text") or node.get("label") or node.get("file") or node.get("id"))
                if label:
                    lines.append(f"- {label}")
            lines.append("")
        if isinstance(edges, list):
            lines.append("## Edges")
            for edge in edges[:200]:
                if not isinstance(edge, Mapping):
                    continue
                from_node = _clean_ws(edge.get("fromNode") or edge.get("from") or "")
                to_node = _clean_ws(edge.get("toNode") or edge.get("to") or "")
                label = _clean_ws(edge.get("label") or "links_to")
                if from_node or to_node:
                    lines.append(f"- {from_node} --{label}--> {to_node}")
        return "\n".join(lines).strip()
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _merge_result(stats: VaultImportStats, result: BrainCellBuildResult) -> None:
    stats.cells_created += len(result.created)
    stats.cells_updated += len(result.updated)
    stats.links_created += len(result.links_created)
    stats.links_strengthened += len(result.links_strengthened)
    stats.skipped.extend(str(item) for item in result.skipped if str(item).strip())


def import_obsidian_vault(
    *,
    vault_path: str | Path,
    brain_dir: str | Path,
    max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
    max_chunks_per_file: int = DEFAULT_MAX_CHUNKS_PER_FILE,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    include_large_files: bool = False,
    ignore_dirs: Optional[Iterable[str]] = None,
    report_path: str | Path | None = None,
) -> VaultImportStats:
    vault_root = Path(vault_path).expanduser().resolve()
    if not vault_root.exists() or not vault_root.is_dir():
        raise FileNotFoundError(f"vault path is not a directory: {vault_root}")

    brain_root = Path(brain_dir).expanduser().resolve()
    builder = BrainCellBuilder(brain_root)
    builder.bootstrap()
    # BrainCellBuilder persists after each ingest by default. Vault imports can
    # contain hundreds of chunks, so defer graph/Markdown writes until the end
    # while keeping all cell/link updates in memory.
    original_save = builder._save  # type: ignore[attr-defined]
    builder._save = lambda: None  # type: ignore[method-assign]

    stats = VaultImportStats(
        vault_path=vault_root.as_posix(),
        brain_dir=brain_root.as_posix(),
        started_at=utc_now_iso(),
    )
    ignored = set(DEFAULT_IGNORE_DIRS)
    if ignore_dirs:
        ignored.update(str(item).strip() for item in ignore_dirs if str(item).strip())

    try:
        for path in _iter_candidate_files(vault_root, ignored):
            stats.files_seen += 1
            rel = path.relative_to(vault_root).as_posix()
            try:
                size_bytes = path.stat().st_size
                if size_bytes > max_file_bytes and not include_large_files:
                    stats.files_skipped += 1
                    stats.skipped.append(f"{rel}: skipped_large_file_{size_bytes}_bytes")
                    continue

                raw_text = _read_text(path)
                if path.suffix.lower() in {".json", ".canvas"}:
                    raw_text = _json_to_learning_text(path, raw_text)

                frontmatter, body = _strip_frontmatter(raw_text)
                body = body.strip()
                if not body:
                    stats.files_skipped += 1
                    stats.skipped.append(f"{rel}: empty_note")
                    continue

                note_title = _title_from_note(path, body, frontmatter, vault_root)
                note_kind = _kind_from_path(path, vault_root)
                folder_concepts = _folder_concepts(path, vault_root)
                source_ref = f"obsidian:{vault_root.name}/{rel}"
                chunks = _markdown_heading_chunks(body, max_chars=max(1000, max_chars_per_chunk), max_chunks=max(1, max_chunks_per_file))
                if not chunks:
                    stats.files_skipped += 1
                    stats.skipped.append(f"{rel}: no_chunks")
                    continue

                imported_chunks = 0
                for index, (heading, chunk_text) in enumerate(chunks, start=1):
                    chunk_title = note_title if len(chunks) == 1 else f"{note_title} / {heading}"
                    learning_text = (
                        f"# {chunk_title}\n\n"
                        f"Source note: {rel}\n"
                        f"Source folder concepts: {', '.join(folder_concepts) or 'Vault root'}\n"
                        f"Chunk: {index} of {len(chunks)}\n\n"
                        f"{chunk_text}"
                    )
                    result = builder.ingest_text(
                        learning_text,
                        title=chunk_title,
                        kind=note_kind,
                        source_refs=[source_ref, stable_source_ref(path)],
                        concepts=[*folder_concepts, note_kind, path.stem],
                        metadata={
                            "source_type": "obsidian_vault_import",
                            "vault_name": vault_root.name,
                            "relative_path": rel,
                            "chunk_index": index,
                            "chunk_count": len(chunks),
                            "file_size_bytes": size_bytes,
                            "frontmatter": dict(frontmatter),
                        },
                    )
                    _merge_result(stats, result)
                    stats.chunks_imported += 1
                    imported_chunks += 1

                stats.files_imported += 1
                stats.imported_files.append(
                    {
                        "path": rel,
                        "kind": note_kind,
                        "title": note_title,
                        "chunks": imported_chunks,
                        "size_bytes": size_bytes,
                    }
                )
            except Exception as exc:  # defensive importer path
                stats.files_skipped += 1
                stats.skipped.append(f"{rel}: {type(exc).__name__}: {exc}")
    finally:
        builder._save = original_save  # type: ignore[method-assign]
        builder._save()

    stats.finished_at = utc_now_iso()
    report_target = Path(report_path) if report_path else brain_root / "graph" / "obsidian_import_report.json"
    report_target.parent.mkdir(parents=True, exist_ok=True)
    report_target.write_text(json.dumps(asdict(stats), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return stats

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import an Obsidian vault into Stark brain cells.")
    parser.add_argument("--vault", required=True, help="Path to the Obsidian vault folder to import.")
    parser.add_argument("--brain-dir", default=".stark_brain_cells", help="Brain-cell knowledge base directory to create/update.")
    parser.add_argument("--max-chars-per-chunk", type=int, default=DEFAULT_MAX_CHARS_PER_CHUNK)
    parser.add_argument("--max-chunks-per-file", type=int, default=DEFAULT_MAX_CHUNKS_PER_FILE)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--include-large-files", action="store_true", help="Import files larger than --max-file-bytes by chunking them.")
    parser.add_argument("--ignore-dir", action="append", default=[], help="Additional directory name to ignore. May be passed multiple times.")
    parser.add_argument("--report-path", default=None, help="Optional JSON report output path.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    stats = import_obsidian_vault(
        vault_path=args.vault,
        brain_dir=args.brain_dir,
        max_chars_per_chunk=args.max_chars_per_chunk,
        max_chunks_per_file=args.max_chunks_per_file,
        max_file_bytes=args.max_file_bytes,
        include_large_files=args.include_large_files,
        ignore_dirs=args.ignore_dir,
        report_path=args.report_path,
    )
    print(json.dumps(asdict(stats), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
