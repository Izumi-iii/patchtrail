"""Owned background worker for a persisted local web job. No HTTP listener."""

import argparse
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def execute(database, identity, attempt, *, retry=False, model=None, backend=None):
    from patchtrail.agents.config import AgentBudget, ModelSettings
    from patchtrail.agents.model import LangChainModel
    from patchtrail.agents.runner import AgentRunner
    from patchtrail.domain.task import TaskSpec, VerificationError
    from patchtrail.web.jobs import Jobs, execution_lock
    from patchtrail.web.store import Store

    jobs = Jobs(Store(database))
    directory = jobs.directory(identity)
    with execution_lock(directory):
        row = jobs.row(identity)
        if row["attempt"] != attempt or row["state"] not in ("QUEUED", "STOP_REQUESTED"):
            return
        with jobs.store.connect() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN state='STOP_REQUESTED' THEN state ELSE 'RUNNING' END WHERE id=? AND attempt=?",
                       (identity, attempt))
        try:
            class WebRunner(AgentRunner):
                def _check_time(self):
                    super()._check_time()
                    if (directory / "pause.requested").exists():
                        raise KeyboardInterrupt

            task = TaskSpec.load(directory / "task.json")
            if json.loads(json.dumps(task.to_dict())) != json.loads(row["task"]):
                raise VerificationError("RECOVERY_REQUIRED", "task_changed", "任务快照与提交记录不一致。")
            runner = WebRunner(jobs.run_root(identity), model or LangChainModel(ModelSettings.from_env(ROOT / ".env")),
                               budget=AgentBudget(max_model_calls=8, max_candidates=2), backend=backend)
            existing = jobs.run_directory(identity)
            result = (runner.resume(existing, retry_unknown_model_call=retry) if existing else runner.run(task))
            if result["status"] == "PASSED":
                approval = jobs.approvals.request(result["artifact_directory"])
                jobs.finish(identity, attempt, "WAITING_APPROVAL", approval_id=approval["id"])
            else:
                state = result["status"] if result["status"] in ("SUSPENDED", "RECOVERY_REQUIRED") else "FAILED"
                jobs.finish(identity, attempt, state, error_code=result["reason_code"], error=result.get("message"))
        except VerificationError as exc:
            state = "RECOVERY_REQUIRED" if jobs.run_directory(identity) else "FAILED"
            jobs.finish(identity, attempt, state, error_code=exc.reason, error=str(exc))
        except Exception as exc:
            jobs.finish(identity, attempt, "RECOVERY_REQUIRED", error_code="worker_error",
                        error=f"后台任务异常（{type(exc).__name__}）。请检查本机日志后恢复。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--retry-unknown-model-call", action="store_true")
    args = parser.parse_args()
    execute(args.database, args.job, args.attempt, retry=args.retry_unknown_model_call)


if __name__ == "__main__":
    main()
