
#!/usr/bin/env python3
"""Validate Restoka eval datasets without running agents or modifying data."""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from evaluation.supplier_metrics import FIELDS as SUPPLIER_FIELDS, normalize

ACTIONS = {"act", "clarify", "escalate", "refuse"}

CHECK_OPS = {
    "eq",
    "contains",
    "contains_all",
    "not_contains_any",
    "no_forbidden_calls",
    "sequence_actions",
    "sequence_tool_calls",
    "step_state_eq",
    "step_fact_eq",
    "persistent_offer_update",
    "trace_required",
    "review_events_match",
    "deadline",
    "review_content_matches_log",
    "review_no_invented_events",
    "local_submission_recovery",
    "final_json_recovered",
    "step_comparison_eq",
    "cancellation_text_safety",
    "scenario_budget_reached",
    "scenario_final_state_eq",
    "scenario_business_fact_eq",
    "scenario_forbidden_effect",
    "scenario_observation",
    "scenario_cross_step_relation",
    "final_action_allowed",
    "external_send_attempts_eq",
    "scenario_step_fact_eq",
    "snapshot_row_field_eq",
    "snapshot_row_count_eq",
    "scenario_tool_policy",
    "timeout_recovery_attempt",
    "timeout_no_duplicate_send",
}

REQUIRED_FIELDS = {
    "id", "class", "subclass", "input", "fixture",
    "ground_truth", "checks", "split",
}


def load_json(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list")
    return data


def validate_cases(cases, expected_split, errors):
    ids = []

    for index, case in enumerate(cases):
        loc = f"{expected_split}[{index}]"

        if not isinstance(case, dict):
            errors.append(f"{loc}: scenario must be an object")
            continue

        missing = REQUIRED_FIELDS - case.keys()
        if missing:
            errors.append(f"{loc}: missing fields {sorted(missing)}")

        sid = case.get("id")
        if not isinstance(sid, str) or not sid:
            errors.append(f"{loc}: invalid id")
        else:
            ids.append(sid)
            loc = sid

        if case.get("split") != expected_split:
            errors.append(f"{loc}: incorrect split")

        if not isinstance(case.get("fixture"), dict):
            errors.append(f"{loc}: fixture must be an object")

        gt = case.get("ground_truth")
        if not isinstance(gt, dict):
            errors.append(f"{loc}: ground_truth must be an object")
        else:
            action = gt.get("expected_action")
            if action not in ACTIONS:
                errors.append(f"{loc}: invalid expected_action: {action}")

            for field in ("expected_calls", "forbidden_calls"):
                if not isinstance(gt.get(field, []), list):
                    errors.append(f"{loc}: {field} must be a list")

            for field in ("expected_state", "required_facts"):
                if not isinstance(gt.get(field, {}), dict):
                    errors.append(f"{loc}: {field} must be an object")

            expectations = gt.get("scenario_expectations")
            if expectations is not None:
                if not isinstance(expectations, dict):
                    errors.append(f"{loc}: invalid scenario_expectations")
                elif "events" not in case:
                    errors.append(
                        f"{loc}: scenario_expectations without events"
                    )

        limits = case.get("run_limits")
        if limits is not None:
            if not isinstance(limits, dict) or not limits:
                errors.append(f"{loc}: run_limits must be nonempty object")
            else:
                allowed_limits = {"scenario_max_steps", "scenario_max_tool_calls",
                                  "max_steps", "max_tool_calls"}
                if not set(limits) <= allowed_limits:
                    errors.append(f"{loc}: unsupported run_limits key")
                if any(type(n) is not int or n < 1 for n in limits.values()):
                    errors.append(f"{loc}: run_limits must contain positive integers")

        checks = case.get("checks")
        if not isinstance(checks, list):
            errors.append(f"{loc}: checks must be a list")
        else:
            if not checks:
                errors.append(f"{loc}: empty checks")

            for j, check in enumerate(checks):
                if not isinstance(check, dict):
                    errors.append(f"{loc}: check {j} is not an object")
                    continue

                op = check.get("op")
                if op not in CHECK_OPS:
                    errors.append(f"{loc}: unsupported check op {op}")

                if not isinstance(check.get("path"), str):
                    errors.append(f"{loc}: check {j} missing path")

                OPS_WITHOUT_VALUE = {
                    "review_content_matches_log",
                    "review_no_invented_events",
                }

                if (
                    op not in OPS_WITHOUT_VALUE
                    and "value" not in check
                    and "values" not in check
                ):
                    errors.append(f"{loc}: check {j} missing expected value")

        events = case.get("events")
        if events is not None:
            if not isinstance(events, list) or not events:
                errors.append(f"{loc}: events must be a nonempty list")
            else:
                event_ids = []
                for j, event in enumerate(events):
                    if not isinstance(event, dict):
                        errors.append(f"{loc}: event {j} is not an object")
                        continue
                    for field in ("event_id", "kind", "at"):
                        if not event.get(field):
                            errors.append(
                                f"{loc}: event {j} missing {field}"
                            )
                    if event.get("event_id"):
                        event_ids.append(event["event_id"])

                if len(event_ids) != len(set(event_ids)):
                    errors.append(f"{loc}: duplicate event IDs")

                if isinstance(gt, dict):
                    expectations = gt.get("scenario_expectations", {})
                    if isinstance(expectations, dict):
                        expected_steps = expectations.get("steps", [])
                        if expected_steps and len(expected_steps) != len(events):
                            errors.append(
                                f"{loc}: step expectations count mismatch"
                            )

        supplier_turns = case.get("supplier_evaluation")
        if supplier_turns is not None:
            steps = events or case.get("scenario_steps") or [case.get("input")]
            if not isinstance(supplier_turns, list) or not isinstance(steps, list):
                errors.append(f"{loc}: invalid supplier_evaluation")
                continue
            annotated = set()
            for turn in supplier_turns:
                if not isinstance(turn, dict):
                    errors.append(f"{loc}: invalid supplier turn")
                    continue
                step = turn.get("step")
                facts = turn.get("facts")
                if type(step) is not int or not 1 <= step <= len(steps) or step in annotated:
                    errors.append(f"{loc}: invalid or repeated supplier step {step}")
                    continue
                annotated.add(step)
                if (not isinstance(facts, dict) or not facts.keys() <= SUPPLIER_FIELDS
                        or any(normalize(field, value) == ("invalid",) for field, value in facts.items())):
                    errors.append(f"{loc}: invalid supplier facts at step {step}")

    duplicates = [
        sid for sid, count in Counter(ids).items() if count > 1
    ]
    if duplicates:
        errors.append(
            f"{expected_split}: duplicate IDs {duplicates}"
        )

    return set(ids)


def validate_annotations(path, visible, errors):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        columns = set(reader.fieldnames or [])

    required = {
        "id", "annotator_a_action", "annotator_b_action",
        "adjudicated_action", "notes",
    }
    if not required <= columns:
        errors.append("Annotation CSV: missing required columns")
        return len(rows)

    seen = set()
    for row in rows:
        sid = row["id"]

        if sid in seen:
            errors.append(f"Annotation CSV: duplicate {sid}")
        seen.add(sid)

        if sid not in visible:
            errors.append(f"Annotation CSV: {sid} not in visible set")

        a = row["annotator_a_action"].strip()
        b = row["annotator_b_action"].strip()
        adjudicated = row["adjudicated_action"].strip()

        if a not in ACTIONS:
            errors.append(f"{sid}: invalid annotator_a_action")

        if b not in ACTIONS:
            errors.append(f"{sid}: invalid annotator_b_action")

        if adjudicated:
            if adjudicated not in ACTIONS:
                errors.append(f"{sid}: invalid adjudicated_action")
        else:
            if a != b or a not in ACTIONS:
                errors.append(
                    f"{sid}: missing adjudication for disagreement"
                )
                
        effective_action = adjudicated or (a if a == b else None)
        if sid == "R-144" and effective_action != "act":
            errors.append("R-144: expected adjudicated_action=act")

        if (
            row["annotator_a_action"] != row["annotator_b_action"]
            and not row["notes"].strip()
        ):
            errors.append(f"{sid}: disagreement without notes")

    return len(rows)

def validate_split_isolation(visible, hidden, errors):

    for field in ("seed_group", "template_family"):
        visible_groups = {
            case[field]
            for case in visible
            if isinstance(case, dict)
            and isinstance(case.get(field), str)
            and case[field].strip()
        }

        hidden_groups = {
            case[field]
            for case in hidden
            if isinstance(case, dict)
            and isinstance(case.get(field), str)
            and case[field].strip()
        }

        overlap = visible_groups & hidden_groups

        if overlap:
            errors.append(
                f"{field}: {len(overlap)} groups "
                "appear in both visible and hidden"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--visible", default="eval_visible.json")
    parser.add_argument("--hidden", default="eval_hidden.json")
    parser.add_argument(
        "--annotations", default="double_annotation_50.csv"
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Validate hidden split without printing its contents",
    )
    args = parser.parse_args()

    errors = []
    visible = load_json(args.visible)
    visible_ids = validate_cases(visible, "visible", errors)

    if len(visible) != 130:
        errors.append(f"Expected 130 visible cases, got {len(visible)}")

    annotation_count = validate_annotations(
        args.annotations, visible_ids, errors
    )

    if annotation_count != 50:
        errors.append(
            f"Expected 50 annotated cases, got {annotation_count}"
        )

    print(f"Visible scenarios: {len(visible)}")
    print(f"Double annotations: {annotation_count}")

    if args.include_hidden:
        hidden = load_json(args.hidden)
        hidden_ids = validate_cases(hidden, "hidden", errors)

        if len(hidden) != 38:
            errors.append(
                f"Expected 38 hidden cases, got {len(hidden)}"
            )

        if visible_ids & hidden_ids:
            errors.append("Visible/hidden ID overlap")
        
        validate_split_isolation(visible, hidden, errors)

        print(f"Hidden scenarios: {len(hidden)}")
        print(f"Total scenarios: {len(visible) + len(hidden)}")

    print(f"Validation errors: {len(errors)}")

    if errors:
        for error in errors:
            print("FAIL:", error, file=sys.stderr)
        return 1

    print("Validation: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
