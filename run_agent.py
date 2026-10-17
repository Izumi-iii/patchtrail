"""Run the bounded DeepSeek/LangChain repair graph with independent verification."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from patchtrail.agents.config import AgentBudget, ModelSettings
from patchtrail.agents.model import LangChainModel
from patchtrail.agents.runner import AgentRunner
from patchtrail.domain.task import TaskSpec, VerificationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--runs-dir", type=Path, default=Path(".patchtrail/agent-runs"))
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--max-model-calls", type=int, default=12)
    parser.add_argument("--max-candidates", type=int, default=3)
    args = parser.parse_args()
    try:
        task = TaskSpec.load(args.task)
        budget = AgentBudget(max_model_calls=args.max_model_calls, max_candidates=args.max_candidates)
        budget.validate()
        model = LangChainModel(ModelSettings.from_env(env_file=args.env_file))
        result = AgentRunner(args.runs_dir, model, budget=budget).run(task)
    except VerificationError as exc:
        result = {"status": exc.status, "reason_code": exc.reason, "message": str(exc)}
    except OSError as exc:
        result = {"status": "SYSTEM_ERROR", "reason_code": "io_error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASSED" else 1)


if __name__ == "__main__":
    main()
