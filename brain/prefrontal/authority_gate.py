from __future__ import annotations

from .initiative_engine import InitiativeDecision


def evaluate_authority(*, action: str, goal_id: str = "ad_hoc") -> InitiativeDecision:
    normalized = str(action or "").strip().lower()
    if normalized in {"observe", "read", "inspect", "analyze", "read_only_diagnosis"}:
        return InitiativeDecision(
            goal_id=goal_id,
            decision="analyze",
            authority_level=2,
            allowed=True,
            requires_user_approval=False,
            reason="Read-only action is allowed.",
        )
    if normalized in {"edit_source_code", "move_file", "install_package", "external_action", "external_network", "send_message"}:
        return InitiativeDecision(
            goal_id=goal_id,
            decision="ask",
            authority_level=6,
            allowed=False,
            requires_user_approval=True,
            reason="Risky or side-effectful action requires explicit user approval.",
            required_backup=normalized in {"edit_source_code", "move_file"},
        )
    if normalized in {"delete_file", "delete_memory", "disable_safety"}:
        return InitiativeDecision(
            goal_id=goal_id,
            decision="block",
            authority_level=7,
            allowed=False,
            requires_user_approval=True,
            reason="Destructive or safety-sensitive action is blocked by default.",
            required_backup=True,
        )
    return InitiativeDecision(
        goal_id=goal_id,
        decision="ask",
        authority_level=6,
        allowed=False,
        requires_user_approval=True,
        reason="Unknown action requires review.",
    )
