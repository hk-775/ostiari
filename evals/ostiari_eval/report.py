"""Summarize Inspect logs and optionally enforce a minimum accuracy."""

from __future__ import annotations

import argparse
import json
from typing import Any

from inspect_ai.log import EvalLog, list_eval_logs, read_eval_log


def summarize_log(log: EvalLog) -> dict[str, Any]:
    """Return the stable subset of an Inspect log used for model comparisons."""
    scores: list[dict[str, Any]] = []
    if log.results is not None:
        for score in log.results.scores:
            scores.append(
                {
                    "name": score.name,
                    "scorer": score.scorer,
                    "scored_samples": score.scored_samples,
                    "metrics": {
                        name: metric.value for name, metric in score.metrics.items()
                    },
                }
            )

    return {
        "location": log.location,
        "status": log.status,
        "task": log.eval.task,
        "model": str(log.eval.model),
        "scores": scores,
        "model_usage": {
            model: usage.model_dump(mode="json")
            for model, usage in log.stats.model_usage.items()
        },
    }


def accuracy_values(summary: dict[str, Any]) -> list[float]:
    """Extract aggregate accuracy metrics from a summary."""
    values: list[float] = []
    for score in summary["scores"]:
        value = score["metrics"].get("accuracy")
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _load_logs(paths: list[str], log_dir: str, include_all: bool) -> list[EvalLog]:
    if paths:
        return [read_eval_log(path, header_only=True) for path in paths]

    available = list_eval_logs(log_dir=log_dir, descending=True)
    if not available:
        raise FileNotFoundError(f"No Inspect logs found under {log_dir}")
    selected = available if include_all else available[:1]
    return [read_eval_log(info, header_only=True) for info in selected]


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"{summary['task']} | {summary['model']} | {summary['status']}")
    for score in summary["scores"]:
        metrics = ", ".join(
            f"{name}={value:.4f}" if isinstance(value, float) else f"{name}={value}"
            for name, value in score["metrics"].items()
        )
        print(f"  {score['name']}: {metrics or 'no metrics'}")
    print(f"  log: {summary['location']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="*", help="Inspect .eval or JSON log files")
    parser.add_argument("--log-dir", default="evals/logs")
    parser.add_argument("--all", action="store_true", help="Report every log in --log-dir")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--minimum-accuracy",
        type=float,
        help="Exit non-zero when any reported aggregate accuracy is below this value",
    )
    args = parser.parse_args(argv)

    try:
        logs = _load_logs(args.logs, args.log_dir, args.all)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    summaries = [summarize_log(log) for log in logs]
    if args.json:
        print(json.dumps(summaries, indent=2, sort_keys=True))
    else:
        for index, summary in enumerate(summaries):
            if index:
                print()
            _print_summary(summary)

    failed = any(summary["status"] != "success" for summary in summaries)
    if args.minimum_accuracy is not None:
        for summary in summaries:
            values = accuracy_values(summary)
            if not values or any(value < args.minimum_accuracy for value in values):
                failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
