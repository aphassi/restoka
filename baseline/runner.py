"""Public deterministic baseline entry point."""

from dataclasses import dataclass

from harness.contracts import RunResult
from .parsing import normalize_request, parse_command
from .templates import clarify_intent
from .workflows import BaselineWorkflows


@dataclass(frozen=True)
class BaselineRunner:
    planning_horizon_days: int = 14

    def __post_init__(self):
        if type(self.planning_horizon_days) is not int or self.planning_horizon_days < 1:
            raise ValueError("planning_horizon_days must be a positive integer")

    def run(self, request, context) -> RunResult:
        normalized = normalize_request(request, context.event_id)
        parsed = parse_command(normalized, context.now, context.timezone)
        workflows = BaselineWorkflows(context, normalized,
                                      horizon_days=self.planning_horizon_days)

        if normalized.kind == "manager_decision":
            return workflows.manager_decision()
        if normalized.kind == "external_result":
            return workflows.external_result()
        if parsed.intent == "inventory":
            return workflows.inventory()
        if parsed.intent == "material_need":
            return workflows.material_need(parsed)
        if parsed.intent == "compare_suppliers":
            return workflows.compare_suppliers(parsed)
        if parsed.intent == "purchase":
            return workflows.purchase(parsed)
        if parsed.intent == "supplier_reply":
            return workflows.supplier_reply()
        if parsed.intent == "supplier_delay":
            return workflows.supplier_delay(parsed)
        if parsed.intent == "order_status":
            return workflows.order_status()
        if parsed.intent == "daily_review":
            return workflows.daily_review(parsed)
        if parsed.intent == "unsupported":
            return workflows.unsupported()
        if parsed.intent == "no_purchase":
            return workflows.no_purchase()
        if parsed.intent == "unsupported_table":
            return workflows.unsupported_table()
        return RunResult(action="clarify", text=clarify_intent(parsed.ambiguous_intents))
