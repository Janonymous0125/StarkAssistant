from __future__ import annotations

from typing import Optional

from pydantic import Field, field_validator

from assistant.contracts.base import ContractModel
from assistant.contracts.ids import new_uuid
from assistant.contracts.time import utc_now_iso


class BrainObservationEvent(ContractModel):
    schema_version: str = "brain_observation.v1"
    event_id: str = Field(default_factory=new_uuid)
    source: str
    kind: str
    summary: str
    detected_at: str = Field(default_factory=utc_now_iso)
    path: Optional[str] = None
    importance: float = 0.0
    risk: float = 0.0
    novelty: float = 0.0
    requires_attention: bool = False
    suggested_goal: Optional[str] = None

    @field_validator("schema_version")
    @classmethod
    def _schema_version_supported(cls, value: str) -> str:
        if value != "brain_observation.v1":
            raise ValueError("unsupported brain observation schema_version")
        return value

    @field_validator("kind")
    @classmethod
    def _kind_supported(cls, value: str) -> str:
        allowed = {
            "missing_path",
            "todo_marker",
            "repeated_failure",
            "log_error",
            "log_file",
            "file_changed",
            "memory_health",
            "brain_health",
            "test_summary",
        }
        text = str(value or "").strip()
        if text not in allowed:
            raise ValueError("unsupported observation kind")
        return text

    @field_validator("importance", "risk", "novelty")
    @classmethod
    def _score_range(cls, value: float) -> float:
        score = float(value)
        if score < 0.0:
            return 0.0
        if score > 1.0:
            return 1.0
        return score
