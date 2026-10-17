"""Human patch -> immutable snapshot -> baseline -> replay -> container verdict."""

import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from patchtrail.domain.task import TaskSpec, VerificationError
from patchtrail.sandbox.docker import DockerBackend
from patchtrail.workspace.snapshot import apply_candidate, digest, export_commit, manifest, tree_hash
from .verdict import compare_collection, summarize


class _BaselineReady(Exception):
    """Internal early completion through the common evidence finalizer."""


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def copy_suite(source, target):
    files = manifest(source)
    if not files:
        raise VerificationError("CONFIG_ERROR", "empty_suite", "Trusted suite contains no files.")
    target.mkdir(parents=True)
    for relative in files:
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source) / relative, output)
    if manifest(target) != files:
        raise VerificationError("CONFIG_ERROR", "suite_changed", "Suite changed while being copied.")
    return {"tree_sha256": tree_hash(files), "files": files}


class Verifier:
    def __init__(self, run_root, backend=None):
        self.run_root = Path(run_root).resolve()
        self.backend = backend if backend is not None else DockerBackend()

    def verify(self, task: TaskSpec, patch_path=None, *, purpose="full"):
        if purpose not in ("full", "feedback", "baseline"):
            raise VerificationError("CONFIG_ERROR", "invalid_verification_purpose", purpose)
        run_id = "verify-" + uuid.uuid4().hex
        directory = self.run_root / run_id
        directory.mkdir(parents=True)
        started = time.monotonic()
        result = {"schema_version": 1, "run_id": run_id, "task_id": task.task_id,
                  "kind": "independent_patch_verification", "status": "RUNNING",
                  "reason_code": None, "base_commit": task.base_commit,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "agent_connected": False, "stages": {}, "artifact_directory": str(directory),
                  "purpose": purpose,
                  "validation_level": "feedback_and_holdout" if task.holdout_dir and purpose == "full" else "feedback_only",
                  "limitations": ["Controlled non-adversarial repositories only.",
                                  "Tests do not prove correctness outside their coverage.",
                                  "Same-process malicious Python can interfere with test evidence."]}
        write_json(directory / "task.json", task.to_dict())
        write_json(directory / "result.json", result)
        sequence = 0

        def event(kind, **extra):
            nonlocal sequence
            sequence += 1
            with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"sequence": sequence, "kind": kind,
                                        "elapsed_seconds": round(time.monotonic() - started, 3),
                                        **extra}, ensure_ascii=False) + "\n")

        def stage(name, source, suite, baseline=False):
            event("stage_started", stage=name)
            output = directory / "verification" / name
            output.mkdir(parents=True)
            try:
                report = self.backend.run(source, suite, task, run_id, name, output)
                summary = summarize(report)
            except VerificationError as exc:
                if not baseline and exc.reason in ("source_import_error", "wrong_import_origin", "source_root_missing"):
                    exc = VerificationError("REPAIR_FAILED", exc.reason, str(exc))
                failure = {"valid": False, "passed": False, "error_code": exc.reason,
                           "status": exc.status, "message": str(exc)}
                result["stages"][name] = failure
                write_json(output / "error.json", failure)
                write_json(directory / "result.json", result)
                event("stage_failed", stage=name, reason_code=exc.reason)
                raise exc
            result["stages"][name] = summary
            write_json(output / "summary.json", summary)
            event("stage_finished", stage=name, passed=summary["passed"], valid=summary["valid"])
            write_json(directory / "result.json", result)
            return summary

        try:
            event("run_started")
            patch = directory / "changes.patch"
            if purpose != "baseline":
                if patch_path is None:
                    raise VerificationError("CONFIG_ERROR", "missing_patch", "Candidate verification needs a patch.")
                input_patch = Path(patch_path)
                if input_patch.stat().st_size > 262144:
                    raise VerificationError("POLICY_REJECTED", "patch_size", "Patch exceeds 256 KiB.")
                raw_patch = input_patch.read_bytes()
                result["submitted_patch_sha256"] = digest(raw_patch)
                normalized_patch = raw_patch.replace(b"\r\n", b"\n")
                result["patch_sha256"] = digest(normalized_patch)
                result["patch_newlines_normalized"] = normalized_patch != raw_patch
                (directory / "submitted.patch").write_bytes(raw_patch)
                patch.write_bytes(normalized_patch)
                from patchtrail.workspace.snapshot import validate_patch
                result["patch_policy"] = validate_patch(raw_patch, task)
            result["environment"] = self.backend.resolve_image(task.image)
            write_json(directory / "environment.json", result["environment"])
            base = directory / "base"
            base_files = export_commit(task, base)
            result["base_tree_sha256"] = tree_hash(base_files)
            write_json(directory / "base-tree.json", base_files)
            suites, suite_info = {}, {}
            for name, source in (("regression", task.regression_dir), ("feedback", task.feedback_dir),
                                 ("holdout", task.holdout_dir if purpose == "full" else None)):
                if source is not None:
                    suites[name] = directory / "suites" / name
                    suite_info[name] = copy_suite(source, suites[name])
            write_json(directory / "suites.json", suite_info)
            result["verification_plan_sha256"] = digest(json.dumps(
                {"suites": suite_info, "task": task.to_dict(), "image": result["environment"]["image_id"]},
                sort_keys=True).encode())
            baseline_regression = stage("baseline_regression", base, suites["regression"], True)
            if not baseline_regression["passed"]:
                raise VerificationError("CONFIG_ERROR", "baseline_regression_failed", "Regression baseline must pass.")
            baseline_feedback = stage("baseline_feedback", base, suites["feedback"], True)
            if not baseline_feedback["valid"] or not baseline_feedback["counts"]["failed"]:
                raise VerificationError("CONFIG_ERROR", "bug_not_reproduced", "Feedback suite must reproduce an assertion failure.")
            if purpose == "baseline":
                raise _BaselineReady()
            baseline_holdout = None
            if "holdout" in suites:
                baseline_holdout = stage("baseline_holdout", base, suites["holdout"], True)
                if not baseline_holdout["valid"]:
                    raise VerificationError("CONFIG_ERROR", "invalid_holdout_baseline", "Holdout baseline must execute valid tests.")
            candidate = directory / "candidate"
            candidate_files, policy = apply_candidate(base, candidate, patch, task)
            result["candidate_tree_sha256"] = tree_hash(candidate_files)
            result["candidate_id"] = "candidate-" + result["candidate_tree_sha256"][:16]
            result["patch_policy"] = policy
            write_json(directory / "candidate-tree.json", candidate_files)
            regression = stage("candidate_regression", candidate, suites["regression"])
            compare_collection(baseline_regression, regression)
            feedback = stage("candidate_feedback", candidate, suites["feedback"])
            compare_collection(baseline_feedback, feedback)
            if not regression["passed"] or not feedback["passed"]:
                raise VerificationError("REPAIR_FAILED", "required_checks_failed", "Candidate failed regression or feedback checks.")
            if baseline_holdout is not None:
                holdout = stage("candidate_holdout", candidate, suites["holdout"])
                compare_collection(baseline_holdout, holdout)
                if not holdout["passed"]:
                    raise VerificationError("REPAIR_FAILED", "holdout_failed", "Final holdout failed. Do not feed it into repair retries.")
            if manifest(base) != base_files or manifest(candidate) != candidate_files:
                raise VerificationError("SYSTEM_ERROR", "workspace_changed", "Immutable workspace contents changed.")
            for name in suites:
                if manifest(suites[name]) != suite_info[name]["files"]:
                    raise VerificationError("SYSTEM_ERROR", "suite_changed", "Trusted suite changed during execution.")
            result.update(status="FEEDBACK_PASSED" if purpose == "feedback" else "PASSED",
                          reason_code="feedback_checks_passed" if purpose == "feedback" else "all_required_checks_passed")
        except _BaselineReady:
            result.update(status="READY", reason_code="baseline_ready", kind="baseline_verification")
        except VerificationError as exc:
            result.update(status=exc.status, reason_code=exc.reason, message=str(exc))
        except (OSError, ValueError) as exc:
            result.update(status="SYSTEM_ERROR", reason_code="io_or_format_error", message=str(exc))
        except KeyboardInterrupt:
            result.update(status="CANCELLED", reason_code="user_cancelled", message="Verification interrupted.")
        except Exception as exc:
            result.update(status="SYSTEM_ERROR", reason_code="unexpected_error", message=f"{type(exc).__name__}: {exc}")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        event("run_finished", status=result["status"], reason_code=result["reason_code"])
        write_json(directory / "result.json", result)
        lines = ["# PatchTrail independent verification", "", f"Status: {result['status']}",
                 f"Reason: {result['reason_code']}", f"Task: {task.task_id}",
                 f"Base commit: {task.base_commit}", f"Validation: {result['validation_level']}",
                 "", "No language model was used in this run.", "", "## Checks", ""]
        for name, summary in result["stages"].items():
            lines.append(f"- {name}: passed={summary['passed']}, valid={summary['valid']}, "
                         f"counts={summary.get('counts', {})}, error={summary.get('error_code', '')}")
        lines += ["", "## Limitations", "", *[f"- {item}" for item in result["limitations"]], ""]
        (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
        evidence = {}
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory).as_posix()
            if path.is_file() and not relative.startswith(("base/", "candidate/", "suites/")):
                evidence[relative] = {"sha256": digest(path.read_bytes()), "size": path.stat().st_size}
        write_json(directory / "manifest.json", evidence)
        return result
