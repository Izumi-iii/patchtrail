"""Evaluation contracts and accounting; never use provider credentials."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patchtrail.agents.config import AgentBudget
from patchtrail.domain.task import VerificationError
from patchtrail.evaluation.dataset import (assert_fixture_unchanged, load_dataset,
                                           materialize, select_cases)
from patchtrail.evaluation.runner import audit_success, run_experiment, summarize, token_usage, validate_case
from patchtrail.verification.service import Verifier
from patchtrail.workspace.snapshot import apply_candidate, export_commit
from test_verification import FakeBackend


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.sha = load_dataset()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_all_reference_patches_replay_and_commits_are_reproducible(self):
        self.assertEqual(len(self.dataset["cases"]), 8)
        self.assertEqual(sum(c["expectation"] == "repair" for c in self.dataset["cases"]), 7)
        for case in self.dataset["cases"]:
            with self.subTest(case=case["id"]):
                first = self.root / case["id"]
                task = materialize(case, first, "unused")
                again = materialize(case, self.root / (case["id"] + "-again"), "unused")
                self.assertEqual(task.base_commit, again.base_commit)
                assert_fixture_unchanged(first)
                if case["expectation"] == "repair":
                    base, candidate = first / "base", first / "candidate"
                    export_commit(task, base)
                    apply_candidate(base, candidate, first / "corrected.patch", task)
                    for name, code in case["corrected"].items():
                        self.assertEqual((candidate / name).read_text(), code)
                with self.assertRaises(FileExistsError):
                    materialize(case, first, "unused")

    def test_reference_and_holdout_never_live_in_agent_repository(self):
        case = self.dataset["cases"][0]
        task = materialize(case, self.root / "case", "unused")
        paths = sorted(p.name for p in Path(task.repository_path).iterdir())
        self.assertEqual(paths, [".git", "refund.py"])
        self.assertNotIn("corrected", task.request)

    def test_tampered_source_and_added_suite_are_detected(self):
        for change in ("source", "extra_test", "extra_source"):
            folder = self.root / change
            task = materialize(self.dataset["cases"][0], folder, "unused")
            if change == "source":
                (Path(task.repository_path) / "refund.py").write_text("pass\n")
            elif change == "extra_test":
                (Path(task.holdout_dir) / "test_extra.py").write_text("pass\n")
            else:
                (Path(task.repository_path) / "extra.py").write_text("pass\n")
            with self.assertRaises(VerificationError):
                assert_fixture_unchanged(folder)

    def test_selection_rejects_typos_and_duplicates(self):
        with self.assertRaises(ValueError):
            select_cases(self.dataset, ["typo"])
        with self.assertRaises(ValueError):
            select_cases(self.dataset, ["cache-none", "cache-none"])
        self.assertEqual([c["id"] for c in select_cases(self.dataset, ["cache-none", "refund-discount"])],
                         ["refund-discount", "cache-none"])


class MetricsTests(unittest.TestCase):
    def test_unknown_usage_is_not_zero_or_complete(self):
        usage = token_usage({"model_calls": 3, "calls": [
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}, None]})
        self.assertEqual(usage["known_total_tokens"], 12)
        self.assertEqual(usage["unknown_usage_calls"], 2)
        self.assertEqual(usage["usage_status"], "partial")

    def test_controls_and_unrun_cases_cannot_inflate_success_rate(self):
        rows = [{"expectation": "repair", "status": "PASSED", "outcome": "repaired"},
                {"expectation": "repair", "status": "NOT_RUN"},
                {"expectation": "preflight_reject", "status": "CONFIG_ERROR", "outcome": "correct_rejection"}]
        summary = summarize({"mode": "real_model", "cases": rows})
        self.assertEqual(summary["repair_success_rate"], 0.5)
        self.assertEqual(summary["correct_preflight_rejections"], 1)
        self.assertIsNone(summary["cost_usd"])
        offline = summarize({"mode": "fixture_validation", "cases": rows})
        self.assertIsNone(offline["repair_success_rate"])
        self.assertIsNone(offline["repaired_cases"])

    def test_status_without_independent_evidence_cannot_pass(self):
        self.assertFalse(audit_success({"status": "PASSED"}))

    def test_actual_verifier_evidence_is_required_and_tampering_invalidates_it(self):
        dataset, _ = load_dataset()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = materialize(dataset["cases"][0], root / "fixture", "test-only")
            final = Verifier(root / "runs", FakeBackend()).verify(task, root / "fixture/corrected.patch")
            (root / "changes.patch").write_bytes((root / "fixture/corrected.patch").read_bytes())
            result = {"status": "PASSED", "task_id": task.task_id, "base_commit": task.base_commit,
                      "final_verification_ref": final["artifact_directory"], "artifact_directory": str(root),
                      "candidates": [{"tree_sha256": final["candidate_tree_sha256"],
                                      "patch_sha256": final["patch_sha256"]}]}
            self.assertTrue(audit_success(result))
            (root / "changes.patch").write_text("tampered")
            self.assertFalse(audit_success(result))
            (root / "changes.patch").write_bytes((root / "fixture/corrected.patch").read_bytes())
            (Path(final["artifact_directory"]) / "result.json").write_text('{}')
            self.assertFalse(audit_success(result))


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.sha = load_dataset()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "experiment"

    def factory(self):
        return Mock(is_real_model=True, public=lambda: {"model": "unit-test-only"})

    def result(self, used=2):
        return {"status": "REPAIR_FAILED", "reason_code": "no_progress", "model_mode": "real_model", "usage": {
            "model_calls": used, "tool_calls": 3, "candidate_count": 1, "calls": [None] * used},
            "artifact_directory": "unused-test-only"}

    def test_total_budget_caps_each_runner_and_preserves_not_run(self):
        with patch("patchtrail.agents.runner.AgentRunner") as runner:
            runner.return_value.run.return_value = self.result()
            report = run_experiment(self.dataset, self.sha, self.dataset["cases"][:3], self.output,
                                    mode="real_model", total_model_calls=3,
                                    budget=AgentBudget(max_model_calls=2), model_factory=self.factory)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.kwargs["budget"].max_model_calls, 1)
        # Second fake runner violates its one-call allocation: stop and conservatively keep it reserved.
        self.assertEqual(report["model_calls_reserved_or_consumed"], 3)
        self.assertEqual(report["cases"][1]["status"], "SYSTEM_ERROR")
        self.assertEqual(report["cases"][2]["status"], "NOT_RUN")
        self.assertTrue((self.output / "report.md").is_file())

    def test_exhausted_budget_does_not_start_next_case(self):
        with patch("patchtrail.agents.runner.AgentRunner") as runner:
            runner.return_value.run.return_value = self.result()
            report = run_experiment(self.dataset, self.sha, self.dataset["cases"][:2], self.output,
                                    mode="real_model", total_model_calls=2, model_factory=self.factory)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(report["cases"][1]["status"], "NOT_RUN")
        self.assertEqual(report["stop_reason"], "total_model_call_limit")

    def test_exception_reserves_calls_and_redacts_exception_message(self):
        with patch("patchtrail.agents.runner.AgentRunner") as runner:
            runner.return_value.run.side_effect = RuntimeError("SECRET-MUST-NOT-APPEAR")
            report = run_experiment(self.dataset, self.sha, self.dataset["cases"][:2], self.output,
                                    mode="real_model", total_model_calls=4, model_factory=self.factory)
        self.assertEqual(report["model_calls_reserved_or_consumed"], 4)
        self.assertEqual(report["summary"]["model_calls_status"], "partial")
        self.assertEqual(report["summary"]["unaccounted_reserved_calls"], 4)
        self.assertNotIn("SECRET-MUST-NOT-APPEAR", (self.output / "report.json").read_text())
        self.assertEqual(report["cases"][1]["status"], "NOT_RUN")

    def test_control_requires_zero_model_calls_and_exact_reason(self):
        result = self.result(0)
        result.update(status="CONFIG_ERROR", reason_code="baseline_regression_failed")
        with patch("patchtrail.agents.runner.AgentRunner") as runner:
            runner.return_value.run.return_value = result
            report = run_experiment(self.dataset, self.sha, self.dataset["cases"][-1:], self.output,
                                    mode="real_model", total_model_calls=2, model_factory=self.factory)
        self.assertEqual(report["cases"][0]["outcome"], "correct_rejection")
        self.assertEqual(report["summary"]["repair_cases"], 0)

    def test_fixture_environment_error_remains_an_environment_error(self):
        case = self.dataset["cases"][0]
        with patch("patchtrail.evaluation.runner.Verifier") as verifier:
            verifier.return_value.verify.return_value = {
                "status": "ENVIRONMENT_ERROR", "reason_code": "docker_unavailable", "artifact_directory": "test-only"}
            row = validate_case(None, case, Path(self.temp.name))
        self.assertEqual(row["status"], "ENVIRONMENT_ERROR")
        self.assertEqual(row["reason_code"], "docker_unavailable")
        self.assertEqual(verifier.return_value.verify.call_count, 1)

    def test_scripted_result_cannot_count_as_real_model_evaluation(self):
        result = self.result()
        result["model_mode"] = "scripted_test"
        with patch("patchtrail.agents.runner.AgentRunner") as runner:
            runner.return_value.run.return_value = result
            report = run_experiment(self.dataset, self.sha, self.dataset["cases"][:1], self.output,
                                    mode="real_model", total_model_calls=2, model_factory=self.factory)
        self.assertEqual(report["cases"][0]["status"], "SYSTEM_ERROR")
        self.assertEqual(report["summary"]["repaired_cases"], 0)

    def test_cli_refuses_paid_run_without_explicit_opt_in(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "evaluate.py"), "run", "--output", str(self.output)],
                                env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"--allow-model-calls", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
