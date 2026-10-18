"""List, prepare, validate, or evaluate the fixed PatchTrail Python benchmark."""

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from patchtrail.agents.config import AgentBudget, ModelSettings
from patchtrail.evaluation.dataset import load_dataset, materialize, select_cases
from patchtrail.evaluation.runner import run_experiment
from patchtrail.verification.service import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "prepare", "validate", "run"))
    parser.add_argument("--case", action="append", default=[], dest="cases")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--image", default="patchtrail-pytest:0.1")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--max-total-model-calls", type=int)
    parser.add_argument("--max-model-calls", type=int, default=8)
    parser.add_argument("--max-candidates", type=int, default=2)
    args = parser.parse_args()
    try:
        dataset, sha = load_dataset()
        cases = select_cases(dataset, args.cases)
        if args.command == "list":
            print(json.dumps({"dataset_id": dataset["dataset_id"], "sha256": sha,
                              "cases": [{k: case[k] for k in ("id", "category", "expectation", "request")}
                                        for case in cases]}, ensure_ascii=False, indent=2))
            return
        budget = AgentBudget(max_model_calls=args.max_model_calls, max_candidates=args.max_candidates)
        budget.validate()
        factory = None
        if args.command == "run":
            if not args.allow_model_calls or args.max_total_model_calls is None:
                parser.error("run requires --allow-model-calls and --max-total-model-calls (1..1000).")
            if not 1 <= args.max_total_model_calls <= 1000:
                parser.error("--max-total-model-calls must be between 1 and 1000.")
            # Validate local configuration before allocating output. No paid call here.
            settings = ModelSettings.from_env(env_file=args.env_file)
            from patchtrail.agents.model import LangChainModel
            factory = lambda: LangChainModel(settings)
        output = args.output or ROOT / ".patchtrail/evaluations" / ("eval-" + uuid.uuid4().hex)
        if args.command == "prepare":
            output.mkdir(parents=True, exist_ok=False)
            write_json(output / "dataset.snapshot.json", dataset)
            write_json(output / "selection.json", {"dataset_id": dataset["dataset_id"],
                                                   "dataset_sha256": sha,
                                                   "cases": [case["id"] for case in cases]})
            for case in cases:
                materialize(case, output / case["id"], args.image)
            print(json.dumps({"output": str(output.resolve()), "dataset_sha256": sha,
                              "cases": [case["id"] for case in cases]}, indent=2))
            return
        report = run_experiment(dataset, sha, cases, output,
                                mode="real_model" if args.command == "run" else "fixture_validation",
                                image=args.image, budget=budget, model_factory=factory,
                                total_model_calls=args.max_total_model_calls or 0)
        print(json.dumps({"report": str(output.resolve() / "report.json"),
                          "summary": report["summary"], "stop_reason": report["stop_reason"]}, indent=2))
        acceptable = {"fixture_valid", "repaired", "correct_rejection"}
        raise SystemExit(0 if report["completed"] and all(r.get("outcome") in acceptable for r in report["cases"]) else 1)
    except Exception as exc:
        # Avoid echoing model configuration or credential-bearing provider messages.
        parser.exit(2, f"Evaluation failed ({type(exc).__name__}); check configuration and use a fresh output directory.\n")


if __name__ == "__main__":
    main()
