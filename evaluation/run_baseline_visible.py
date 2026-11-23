
import argparse
import json
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from statistics import mean

from baseline.runner import BaselineRunner
from harness.scenario import ScenarioRunner
from harness.contracts import MultiStepScenario, RunConfig, RunResult


def adapt_case(case):
    """Преобразуем scenario_steps в события, не меняя eval-set."""
    steps = case.get("scenario_steps", [])

    if len(steps) <= 1:
        return case

    if case.get("events"):
        return case

    start = datetime.fromisoformat(case["fixture"]["now"])

    events = []

    for index, step in enumerate(steps):
        incoming = step["incoming"]
        base_input = case.get("input", {}) if index == 0 else {}
        base_input = base_input if isinstance(base_input, dict) else {}
        step_input = step.get("input", {})
        event_input = {**base_input, **step_input, "text": incoming}
        supplier_statement = case["fixture"].get("supplier_message")
        if index == 0 and isinstance(supplier_statement, str) and supplier_statement in incoming:
            event_input["supplier_statement"] = supplier_statement

        events.append({
            "event_id": f"turn-{index + 1}",
            "at": (
                start + timedelta(seconds=index)
            ).isoformat(),
            "kind": "user_message",
            "input": {key: value for key, value in event_input.items() if key != "kind"},
            "conversation_id": f"{case['id']}-conversation",
            "user_id": "eval-user",
        })

    return MultiStepScenario.model_validate({
        "scenario_id": case["id"],
        "fixture": case["fixture"],
        "events": events,
    })


class ControlledBudgetRunner:
    """Deterministic harness contract test; no business tools or external writes.

    Selected only by an explicit scenario runner_profile, not by case ID.
    Each event consumes an actual ToolGateway reasoning step, so a shared
    scenario_max_steps=1 is exhausted by the second event.
    """
    def run(self, request, context):
        context.step()
        return RunResult(action="clarify", text="Проверка общего лимита шагов")


def configure_case_run(case, config, runner):
    case_id = case["id"]
    limits = case.get("run_limits")
    if limits is None:
        limits = {}
    allowed_limits = {"scenario_max_steps", "scenario_max_tool_calls", "max_steps", "max_tool_calls"}
    if not isinstance(limits, dict) or not set(limits) <= allowed_limits:
        raise ValueError(f"{case_id}: invalid run_limits keys")
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise ValueError(f"{case_id}: run_limits must contain positive integers")

    profile = case.get("runner_profile", "baseline")
    if profile == "controlled_budget":
        if not limits:
            raise ValueError(f"{case_id}: controlled_budget requires explicit run_limits")
        return config.model_copy(update={**limits, "runner_name": profile}), ControlledBudgetRunner()
    if profile != "baseline":
        raise ValueError(f"{case_id}: unsupported runner_profile={profile!r}")
    return (config.model_copy(update=limits) if limits else config), runner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--eval",
        default="eval_visible.json",
    )
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    dataset = Path(args.eval).read_bytes()
    cases = json.loads(dataset.decode("utf-8-sig"))
    if not cases:
        parser.error("Набор сценариев пуст")

    if args.only:
        cases = [
            case for case in cases
            if case["id"] == args.only
        ]
        if not cases:
            raise ValueError("Сценарий не найден")

    output_dir = Path("harness_runs")
    output_dir.mkdir(exist_ok=True)

    config = RunConfig(
        eval_version=f"sha256:{sha256(dataset).hexdigest()}",
        runner_name="baseline",
        runner_version="baseline-v1",
        output_dir=output_dir,
        capture_response_text=True,
    )

    runner = ScenarioRunner()
    baseline = BaselineRunner()
    results = []

    for i, case in enumerate(cases, 1):
        case_id = case["id"]
        case_config, scenario_runner = configure_case_run(case, config, baseline)
        scenario = adapt_case(case)

        expected_events = (
            len(scenario.events)
            if isinstance(scenario, MultiStepScenario)
            else len(scenario.get("events", [])) or 1
        )

        print(f"[{i}/{len(cases)}] {case_id}", flush=True)

        try:
            record = runner.run(
                scenario=scenario,
                config=case_config,
                runner=scenario_runner,
            )

            steps = record.get("step_results", [])

            completed = (
                len(steps) == expected_events
                and not record.get("technical_errors")
                and not record.get("tool_errors")
                and all(
                    step.get("result", {}).get(
                        "technical_status"
                    ) == "completed"
                    for step in steps
                )
            )

            result = {
                "id": case_id,
                "expected_events": expected_events,
                "executed_events": len(steps),
                "technical_ok": completed,
                "scenario_status": record["scenario_status"],
                "actions": [
                    step.get("result", {}).get("action")
                    for step in steps
                ],
                "tool_calls": record["tool_calls"],
                "duration_seconds": record["duration_seconds"],
                "technical_errors": record["technical_errors"],
                "tool_errors": record["tool_errors"],
                "result_path": record["result_path"],
                "evaluator_path": record["evaluator_path"],
            }

        except Exception as error:
            result = {
                "id": case_id,
                "expected_events": expected_events,
                "executed_events": 0,
                "technical_ok": False,
                "error": f"{type(error).__name__}: {error}",
            }

        results.append(result)

        print(
            "  ",
            "OK" if result["technical_ok"] else "ERROR",
            "| events:",
            f"{result['executed_events']}/{expected_events}",
            "| actions:",
            result.get("actions", []),
            flush=True,
        )

    durations = [
        r["duration_seconds"]
        for r in results
        if "duration_seconds" in r
    ]

    summary = {
        "total": len(results),
        "technical_success": sum(
            r["technical_ok"] for r in results
        ),
        "technical_failure": sum(
            not r["technical_ok"] for r in results
        ),
        "total_tool_calls": sum(
            r.get("tool_calls", 0) for r in results
        ),
        "mean_duration_seconds": (
            mean(durations) if durations else None
        ),
        "results": results,
    }

    output = output_dir / "baseline_visible_summary.json"
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== SUMMARY ===")
    print("Total:", summary["total"])
    print("Successful:", summary["technical_success"])
    print("Failed:", summary["technical_failure"])
    print("Tool calls:", summary["total_tool_calls"])
    print("Mean duration:", summary["mean_duration_seconds"])
    print("Saved:", output)


if __name__ == "__main__":
    main()
