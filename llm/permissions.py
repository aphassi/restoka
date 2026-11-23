"""Ограничивает действия по текущему событию до вызова инструмента."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.gateway import TOOL_KINDS

from .context import _rows


_SUPPLIER_REPLY = frozenset({"receive_supplier_reply", "process_supplier_reply"})
_WRITES = frozenset(name for name, kind in TOOL_KINDS.items() if kind in {"local_write", "external"})
_USER_WRITES = _WRITES - _SUPPLIER_REPLY
_EVENT_WRITES = {
    "supplier_reply": _SUPPLIER_REPLY,
    "supplier_message": _SUPPLIER_REPLY,
    "supplier_event": _SUPPLIER_REPLY,
    "time_tick": frozenset({"send_review"}),
    "scheduled_review": frozenset({"send_review"}),
}

@dataclass(frozen=True)
class ToolPermissions:
    writes: frozenset[str]
    dispatch_ids: frozenset[str]
    approved_order: str | None = None

    def expose(self, name: str, staged_ids: set[str]) -> bool:
        if name == "dispatch_order":
            return bool(self.dispatch_ids or staged_ids)
        return name not in _WRITES or name in self.writes

    def allows(self, name: str, arguments: Any, staged_ids: set[str]) -> bool:
        if not self.expose(name, staged_ids):
            return False
        if name == "dispatch_order":
            submission_id = arguments.get("submission_id") if isinstance(arguments, dict) else None
            return isinstance(submission_id, str) and (
                submission_id in self.dispatch_ids or submission_id in staged_ids)
        if name == "create_order" and self.approved_order is not None:
            return isinstance(arguments, dict) and arguments.get("order_id") == self.approved_order
        return True


def tool_permissions(event: dict[str, Any], snapshot: dict[str, Any], *,
                     recovering_writes: bool = False) -> ToolPermissions:
    kind, data = event["kind"], event["data"]
    data = data if isinstance(data, dict) else {}
    submissions = [row for row in _rows(snapshot, "business_events")
                   if row.get("type") == "order_submission"]
    lookup_ids = frozenset(row["id"] for row in submissions
                           if row.get("id") and (row.get("details") or {}).get("state")
                           in {"unknown", "sending", "sent", "failed", "rejected"})
    if recovering_writes:
        return ToolPermissions(frozenset(), lookup_ids)
    if kind == "manager_decision":
        order = next((row.get("order_id") for row in _rows(snapshot, "approval_requests")
                      if row.get("id") == data.get("approval_id") and row.get("status") == "approved"
                      and row.get("decision") == "approved"), None)
        if order:
            # The approved intent may already be staged, for example after a retry.
            approved_ids = frozenset(row["id"] for row in submissions
                                     if row.get("id") and (row.get("details") or {}).get("order_id") == order)
            return ToolPermissions(frozenset({"create_order"}), lookup_ids | approved_ids, order)
    if kind == "scheduled_wakeup":
        policies = _rows(snapshot, "organization_policies")
        automatic = len(policies) == 1 and policies[0].get("automatic_ordering_enabled") is True
        writes = frozenset({"prepare_draft", "create_order"}) if automatic else frozenset()
        return ToolPermissions(writes, lookup_ids)
    writes = _USER_WRITES if kind == "user_message" else _EVENT_WRITES.get(kind, frozenset())
    return ToolPermissions(writes, lookup_ids)


def may_have_written(name: str) -> bool:
    return name in _WRITES
