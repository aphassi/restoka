"""Запуск visible-сценариев через LLM-оркестратор и ScenarioRunner.

Нужны PostgreSQL и API-ключ выбранного провайдера. Скрипт сохраняет результат
и трассировку, но не рассчитывает итоговые метрики eval-сета.
Внешние отправки поставщикам имитируются инфраструктурой тестирования.
"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from harness import RunConfig, ScenarioRunner
from llm import ChatCompletionsClient, LLMRunner
from evaluation.model_faults import FinalOutputFault
from evaluation.run_baseline_visible import adapt_case, configure_case_run


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Запустить сценарии Restoka с LLM через общий ScenarioRunner"
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--id", default="R-001", help="ID сценария, например R-001 или MS-V-001")
    selection.add_argument("--all", action="store_true", help="Запустить все сценарии набора")
    parser.add_argument("--model", default=os.environ.get("RESTOKA_LLM_MODEL"))
    parser.add_argument("--model-version", default=None)
    parser.add_argument("--dataset", type=Path, default=Path("eval_visible.json"))
    selection.add_argument(
        "--scenario-file", type=Path, default=None,
        help="Отдельный JSON-файл сценария с массивом events",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--max-rounds", type=int, default=12,
        help="Максимальное число обращений к модели за событие",
    )
    parser.add_argument(
        "--max-request-chars", type=int, default=60000,
        help="Максимальный размер сериализованного входа в символах, не токенах",
    )
    parser.add_argument(
        "--input-price-per-million",
        default=os.environ.get("RESTOKA_LLM_PRICE_INPUT_PER_M"),
        help="Стоимость миллиона входных токенов в USD; можно не указывать",
    )
    parser.add_argument(
        "--output-price-per-million",
        default=os.environ.get("RESTOKA_LLM_PRICE_OUTPUT_PER_M"),
        help="Стоимость миллиона выходных токенов в USD; можно не указывать",
    )
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--max-tool-calls", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, default=Path("harness_runs_llm"))
    args = parser.parse_args()

    if not args.model:
        parser.error("Укажите --model или RESTOKA_LLM_MODEL")

    if args.scenario_file is not None:
        dataset = args.scenario_file.read_bytes()
        case = json.loads(dataset.decode("utf-8-sig"))
        if not isinstance(case, dict) or "events" not in case:
            parser.error("--scenario-file: ожидается JSON сценария с массивом events")
        cases = [case]
    else:
        dataset = args.dataset.read_bytes()
        data = json.loads(dataset.decode("utf-8-sig"))
        cases = data if args.all else [case for case in data if case["id"] == args.id]
        if args.all and (not cases or len({case["id"] for case in cases}) != len(cases)):
            parser.error("Набор должен быть непустым, с уникальными ID сценариев")
        if not args.all and len(cases) != 1:
            parser.error(f"В visible-наборе должен быть один сценарий с ID {args.id}")

    if (args.input_price_per_million is None) != (args.output_price_per_million is None):
        parser.error("Укажите оба тарифа или не указывайте ни одного")
    try:
        input_rate = (
            Decimal(args.input_price_per_million)
            if args.input_price_per_million is not None else None
        )
        output_rate = (
            Decimal(args.output_price_per_million)
            if args.output_price_per_million is not None else None
        )
    except InvalidOperation:
        parser.error("Тарифы должны быть числами")

    client = ChatCompletionsClient.from_env()
    fault_client = FinalOutputFault(client)
    runner = LLMRunner(
        client=fault_client,
        model=args.model,
        model_version=args.model_version or args.model,
        temperature=args.temperature,
        max_rounds=args.max_rounds,
        max_request_chars=args.max_request_chars,
        input_price_per_million=input_rate,
        output_price_per_million=output_rate,
    )
    config = RunConfig(
        eval_version=f"sha256:{sha256(dataset).hexdigest()}",
        runner_name="llm",
        runner_version="langgraph-v1",
        model_version=runner.model_version,
        prompt_version=runner.prompt_version,
        temperature=runner.temperature,
        generation_parameters={
            "max_rounds": runner.max_rounds,
            "max_request_chars": runner.max_request_chars,
            "max_completion_tokens": client.max_completion_tokens,
            "stream": client.stream,
            "model_timeout_seconds": client.timeout_seconds,
            "input_price_per_million": str(input_rate) if input_rate is not None else None,
            "output_price_per_million": str(output_rate) if output_rate is not None else None,
        },
        output_dir=args.output_dir,
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
        seed_support="unknown",
    )
    harness = ScenarioRunner(configure_adapters=fault_client.configure)

    def run_case(case):
        case_config, case_runner = configure_case_run(case, config, runner)
        return harness.run(adapt_case(case), case_config, case_runner)

    if args.all:
        started = perf_counter()
        results = []
        costs = []
        unpriced = 0
        for case in cases:
            record = run_case(case)
            for span in record["llm_usage"]:
                if span.get("cost") is None:
                    unpriced += 1
                else:
                    costs.append(Decimal(str(span["cost"])))
            results.append({"id": case["id"], "result_path": record["result_path"],
                            "runner": record["runner"],
                            "scenario_status": record["scenario_status"],
                            "duration_seconds": record["duration_seconds"]})
            print(f"[{len(results)}/{len(cases)}] {case['id']} {record['scenario_status']}", flush=True)
        summary_path = args.output_dir / "llm_visible_summary.json"
        summary = {
            "config": config.model_dump(mode="json"),
            "duration_seconds": perf_counter() - started,
            "cost_estimate_usd": str(sum(costs, Decimal(0))) if costs else None,
            "llm_calls_without_cost": unpriced,
            "results": results,
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(summary_path)
        return

    record = run_case(cases[0])
    fields = (
        "scenario_id", "runner", "scenario_status", "result", "duration_seconds",
        "tool_calls", "steps", "llm_usage", "technical_errors", "tool_errors",
        "result_path", "evaluator_path", "trace_path",
    )
    print(json.dumps({key: record[key] for key in fields}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
