"""Run the bounded DeepSeek/LangChain repair graph with independent verification."""

import argparse
import json
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from patchtrail.agents.config import AgentBudget, ModelSettings
from patchtrail.agents.model import LangChainModel
from patchtrail.agents.runner import AgentRunner
from patchtrail.approvals.service import ApprovalService
from patchtrail.domain.task import TaskSpec, VerificationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task", type=Path)
    source.add_argument("--resume", type=Path, help="Continue the latest checkpoint in an existing run")
    parser.add_argument("--retry-unknown-model-call", action="store_true",
                        help="Authorize a new request when a previous response was lost; may incur additional charges")
    parser.add_argument("--runs-dir", type=Path, default=Path(".patchtrail/agent-runs"))
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--max-model-calls", type=int, help="New run only; default 12")
    parser.add_argument("--max-candidates", type=int, help="New run only; default 3")
    parser.add_argument("--request-approval", action="store_true", help="Freeze a passed candidate for human approval")
    parser.add_argument("--approvals-dir", type=Path, default=Path(__file__).parent / ".patchtrail/approvals")
    args = parser.parse_args()
    if args.retry_unknown_model_call and not args.resume:
        parser.error("--retry-unknown-model-call requires --resume")
    if args.resume and (args.max_model_calls is not None or args.max_candidates is not None):
        parser.error("Resume uses the original persisted budget; do not supply new limits")
    try:
        budget = AgentBudget(max_model_calls=args.max_model_calls if args.max_model_calls is not None else 12,
                             max_candidates=args.max_candidates if args.max_candidates is not None else 3)
        budget.validate()
        model = LangChainModel(ModelSettings.from_env(env_file=args.env_file))
        runner = AgentRunner(args.runs_dir, model, budget=budget)
        result = (runner.resume(args.resume, retry_unknown_model_call=args.retry_unknown_model_call)
                  if args.resume else runner.run(TaskSpec.load(args.task)))
        if args.request_approval and result["status"] == "PASSED":
            approval = ApprovalService(args.approvals_dir).request(result["artifact_directory"])
            result = {"status": approval["state"], "verification_status": "PASSED",
                      "agent_result": result, "approval": approval}
    except VerificationError as exc:
        result = {"status": exc.status, "reason_code": exc.reason, "message": str(exc)}
    except OSError as exc:
        result = {"status": "SYSTEM_ERROR", "reason_code": "io_error", "message": str(exc)}
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        result = {"status": "RECOVERY_REQUIRED", "reason_code": "checkpoint_io_or_format_error",
                  "message": f"Checkpoint operation failed ({type(exc).__name__})."}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    succeeded = result["status"] == "PASSED" or result.get("verification_status") == "PASSED" and "approval" in result
    raise SystemExit(0 if succeeded else 1)


if __name__ == "__main__":
    main()
