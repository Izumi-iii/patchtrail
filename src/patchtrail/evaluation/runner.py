"""Sequential, bounded experiments; reference validation never counts as model success."""

import json
import platform
import statistics
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from patchtrail.agents.config import AgentBudget
from patchtrail.domain.task import VerificationError
from patchtrail.verification.service import Verifier, write_json
from patchtrail.workspace.snapshot import digest
from .dataset import assert_fixture_unchanged, materialize


def audit_success(result):
    """Count only a full independent verdict bound to the selected candidate and patch."""
    try:
        final_dir = Path(result["final_verification_ref"])
        final = json.loads((final_dir / "result.json").read_text(encoding="utf-8"))
        run_dir = Path(result["artifact_directory"])
        candidate = result["candidates"][-1]
        evidence = json.loads((final_dir / "manifest.json").read_text(encoding="utf-8"))
        for name in ("result.json", "changes.patch", "candidate-tree.json", "suites.json"):
            if digest((final_dir / name).read_bytes()) != evidence[name]["sha256"]:
                return False
        return (result["status"] == final["status"] == "PASSED"
                and final["validation_level"] == "feedback_and_holdout"
                and final["purpose"] == "full"
                and final["task_id"] == result["task_id"]
                and final["base_commit"] == result["base_commit"]
                and all(final["stages"][name]["passed"] and final["stages"][name]["valid"]
                        for name in ("baseline_regression", "candidate_regression", "candidate_feedback", "candidate_holdout"))
                and final["candidate_tree_sha256"] == candidate["tree_sha256"]
                and final["patch_sha256"] == candidate["patch_sha256"]
                == digest((run_dir / "changes.patch").read_bytes()))
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False


def token_usage(usage):
    calls = usage.get("calls", [])
    output = {"known_input_tokens": 0, "known_output_tokens": 0, "known_total_tokens": 0,
              "unknown_usage_calls": 0}
    for index in range(usage.get("model_calls", 0)):
        item = calls[index] if index < len(calls) else None
        valid = isinstance(item, dict) and all(type(item.get(k)) is int and item[k] >= 0
                                              for k in ("input_tokens", "output_tokens", "total_tokens"))
        if not valid:
            output["unknown_usage_calls"] += 1
            continue
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            output["known_" + key] += item[key]
    output["usage_status"] = "partial" if output["unknown_usage_calls"] else "complete"
    return output


def summarize(report):
    rows = report["cases"]
    repair = [row for row in rows if row["expectation"] == "repair"]
    control = [row for row in rows if row["expectation"] == "preflight_reject"]
    executed = [row for row in rows if row["status"] != "NOT_RUN"]
    real = report["mode"] == "real_model"
    successes = sum(row.get("outcome") == "repaired" for row in repair)
    durations = [row["elapsed_seconds"] for row in executed if "elapsed_seconds" in row]
    tokens = {key: sum(row.get("tokens", {}).get(key, 0) for row in rows)
              for key in ("known_input_tokens", "known_output_tokens", "known_total_tokens", "unknown_usage_calls")}
    tokens["usage_status"] = "partial" if tokens["unknown_usage_calls"] else "complete"
    return {"selected_cases": len(rows), "executed_cases": len(executed),
            "repair_cases": len(repair), "repaired_cases": successes if real else None,
            "repair_success_rate": successes / len(repair) if real and repair else None,
            "control_cases": len(control),
            "correct_preflight_rejections": sum(row.get("outcome") == "correct_rejection" for row in control),
            "fixture_validation_passed": sum(row.get("outcome") == "fixture_valid" for row in rows),
            "status_counts": dict(Counter(row["status"] for row in rows)),
            "failure_reasons": dict(Counter(row.get("reason_code", "unknown") for row in executed
                                            if row.get("outcome") not in ("repaired", "correct_rejection", "fixture_valid"))),
            "model_calls": sum(row.get("model_calls") or 0 for row in rows),
            "model_calls_status": "partial" if any(row.get("model_calls") is None and row.get("allocated_model_calls") for row in rows) else "complete",
            "unaccounted_reserved_calls": sum(row.get("allocated_model_calls", 0) for row in rows
                                               if row.get("model_calls") is None),
            "tool_calls": sum(row.get("tool_calls", 0) for row in rows),
            "candidate_count": sum(row.get("candidate_count", 0) for row in rows),
            "repaired_after_retry": sum(row.get("outcome") == "repaired" and row.get("candidate_count", 0) > 1 for row in rows) if real else None,
            "median_case_seconds": statistics.median(durations) if durations else None,
            "tokens": tokens, "cost_usd": None, "cost_status": "unknown"}


def render_report(report):
    summary = report["summary"]
    lines = ["# PatchTrail evaluation", "", f"Mode: `{report['mode']}`",
             f"Dataset: `{report['dataset_id']}` / `{report['dataset_sha256']}`", "",
             "Reference validation is not a model repair evaluation. This is a small synthetic public dataset.",
             "No general repository success rate is established. Unknown cost is not zero.", "",
             f"Repair success: {summary['repaired_cases']} / {summary['repair_cases']} (real-model mode only).",
             f"Correct preflight rejections: {summary['correct_preflight_rejections']} / {summary['control_cases']}.",
             f"Known model calls: {summary['model_calls']} ({summary['model_calls_status']}); cost: unknown.",
             f"Unaccounted reserved calls: {summary['unaccounted_reserved_calls']}.", "",
             "| Case | Expectation | Status | Outcome | Reason | Calls | Candidates | Seconds |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["cases"]:
        values = [row["case_id"], row["expectation"], row["status"], row.get("outcome", ""),
                  row.get("reason_code", ""), row.get("model_calls", 0), row.get("candidate_count", 0),
                  row.get("elapsed_seconds", "")]
        lines.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |")
    lines += ["", f"Stop reason: {report.get('stop_reason') or 'completed'}", "",
              "See report.json and per-case artifact references for evidence. Failed and NOT_RUN cases remain in the denominator.", ""]
    return "\n".join(lines)


def save_report(directory, report):
    report["summary"] = summarize(report)
    write_json(directory / "report.json", report)
    (directory / "report.md").write_text(render_report(report), encoding="utf-8")


def implementation_identity():
    root = Path(__file__).resolve().parents[3]
    paths = [*sorted((root / "src/patchtrail").rglob("*.py")), root / "evaluate.py",
             root / "requirements-agent.lock.txt"]
    files = {p.relative_to(root).as_posix(): digest(p.read_bytes()) for p in paths if p.is_file()}
    packages = {}
    for name in ("langchain", "langchain-openai", "langgraph"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": platform.python_version(), "platform": platform.platform(),
            "packages": packages, "file_sha256": files}


def validate_case(task, case, directory):
    verifier = Verifier(directory / "verification-runs")
    baseline = verifier.verify(task, purpose="baseline")
    refs = {"baseline": baseline["artifact_directory"]}
    checks = {"baseline": baseline}
    if case["expectation"] == "preflight_reject":
        valid = (baseline["status"] == case["expected_status"]
                 and baseline["reason_code"] == case["expected_reason"])
    else:
        valid = baseline["status"] == "READY"
        if valid:
            correct = verifier.verify(task, directory / "fixture/corrected.patch")
            refs["corrected"] = correct["artifact_directory"]
            checks["corrected"] = correct
            valid = correct["status"] == "PASSED"
        if valid and case.get("overfit"):
            overfit = verifier.verify(task, directory / "fixture/overfit.patch")
            refs["overfit"] = overfit["artifact_directory"]
            checks["overfit"] = overfit
            valid = overfit["status"] == "REPAIR_FAILED" and overfit["reason_code"] == "holdout_failed"
    error = next((item for item in checks.values()
                  if item["status"] in ("ENVIRONMENT_ERROR", "SYSTEM_ERROR", "CANCELLED")), None)
    return {"status": error["status"] if error else "VALID" if valid else "INVALID",
            "outcome": "fixture_valid" if valid else "fixture_invalid",
            "reason_code": error["reason_code"] if error else "fixture_contract_verified" if valid else "fixture_contract_failed",
            "validation_checks": {name: {"status": item["status"], "reason_code": item["reason_code"]}
                                  for name, item in checks.items()},
            "verification_refs": refs, "model_calls": 0}


def run_experiment(dataset, dataset_sha, cases, output, *, mode, image="patchtrail-pytest:0.1",
                   budget=None, total_model_calls=0, model_factory=None):
    if mode not in ("fixture_validation", "real_model"):
        raise ValueError("Invalid experiment mode.")
    budget = budget or AgentBudget(max_model_calls=8, max_candidates=2)
    budget.validate()
    if mode == "real_model" and (type(total_model_calls) is not int or not 1 <= total_model_calls <= 1000 or model_factory is None):
        raise ValueError("Real model evaluation needs a factory and a total call budget (1..1000).")
    directory = Path(output).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "dataset.snapshot.json", dataset)
    report = {"schema_version": 1, "dataset_id": dataset["dataset_id"], "dataset_sha256": dataset_sha,
              "dataset_snapshot_sha256": digest((directory / "dataset.snapshot.json").read_bytes()),
              "mode": mode, "created_at": datetime.now(timezone.utc).isoformat(), "image": image,
              "implementation": implementation_identity(),
              "budget_per_case": asdict(budget), "total_model_call_limit": total_model_calls,
              "model_calls_reserved_or_consumed": 0, "stop_reason": None,
              "cases": [{"case_id": case["id"], "category": case["category"],
                         "expectation": case["expectation"], "status": "NOT_RUN"} for case in cases]}
    save_report(directory, report)
    for case, row in zip(cases, report["cases"]):
        remaining = total_model_calls - report["model_calls_reserved_or_consumed"]
        if mode == "real_model" and remaining <= 0:
            report["stop_reason"] = "total_model_call_limit"
            break
        started = time.monotonic()
        case_dir = directory / case["id"]
        allocated = min(budget.max_model_calls, remaining) if mode == "real_model" else 0
        try:
            task = materialize(case, case_dir / "fixture", image)
            if mode == "fixture_validation":
                row.update(validate_case(task, case, case_dir))
            else:
                from patchtrail.agents.runner import AgentRunner
                model = model_factory()
                if not getattr(model, "is_real_model", False):
                    raise ValueError("Only a real model adapter is allowed in real_model mode.")
                report["model"] = model.public()
                # Reserve before execution; unknown or interrupted calls cannot release this allocation.
                report["model_calls_reserved_or_consumed"] += allocated
                row.update(status="RUNNING", allocated_model_calls=allocated, model_calls=None)
                save_report(directory, report)
                result = AgentRunner(case_dir / "agent-runs", model,
                                     budget=replace(budget, max_model_calls=allocated)).run(task)
                if result.get("model_mode") != "real_model":
                    raise ValueError("Scripted results cannot enter real-model metrics.")
                usage = result["usage"]
                used = usage["model_calls"]
                if type(used) is not int or not 0 <= used <= allocated:
                    raise ValueError("Invalid model call accounting.")
                report["model_calls_reserved_or_consumed"] -= allocated - used
                row.update(status=result["status"], reason_code=result["reason_code"],
                           model_calls=used, tool_calls=usage["tool_calls"], candidate_count=usage["candidate_count"],
                           tokens=token_usage(usage), artifact_directory=result["artifact_directory"],
                           final_verification_ref=result.get("final_verification_ref"))
                if case["expectation"] == "repair":
                    row["outcome"] = "repaired" if audit_success(result) else "repair_failed"
                    if result["status"] == "PASSED" and row["outcome"] != "repaired":
                        row.update(status="SYSTEM_ERROR", reason_code="evidence_audit_failed")
                else:
                    rejected = (result["status"] == case["expected_status"]
                                and result["reason_code"] == case["expected_reason"] and used == 0)
                    row["outcome"] = "correct_rejection" if rejected else "control_failed"
            assert_fixture_unchanged(case_dir / "fixture")
        except KeyboardInterrupt:
            row.update(status="CANCELLED", outcome="interrupted", reason_code="user_cancelled")
            report["stop_reason"] = "user_cancelled"
        except Exception as exc:
            # Never serialize provider exception text or model credentials.
            row.update(status=exc.status if isinstance(exc, VerificationError) else "SYSTEM_ERROR",
                       reason_code=exc.reason if isinstance(exc, VerificationError) else "evaluation_case_error",
                       outcome="evaluation_error", exception_type=type(exc).__name__)
        row["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save_report(directory, report)
        if report["stop_reason"]:
            break
        if row["status"] in ("ENVIRONMENT_ERROR", "SYSTEM_ERROR", "CANCELLED"):
            report["stop_reason"] = "execution_error"
            break
    report["completed"] = all(row["status"] != "NOT_RUN" for row in report["cases"])
    save_report(directory, report)
    return report
