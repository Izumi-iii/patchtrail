import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from examples.create_demo import create_demo
from patchtrail.domain.task import TaskSpec, VerificationError, safe_relative
from patchtrail.sandbox.docker import DockerBackend
from patchtrail.verification.service import Verifier
from patchtrail.verification.verdict import compare_collection, summarize
from patchtrail.workspace.snapshot import apply_candidate, export_commit, manifest, register_path, validate_patch


def report(status="passed", *, exit_code=None, nodes=None):
    nodes = nodes if nodes is not None else ["test_suite.py::test_behavior"]
    phases = [{"when": when, "outcome": "passed", "wasxfail": False}
              for when in ("setup", "call", "teardown")]
    if status == "failed":
        phases[1]["outcome"] = "failed"
    elif status == "error":
        phases = [{"when": "setup", "outcome": "failed", "wasxfail": False}]
    elif status == "skipped":
        phases[1]["outcome"] = "skipped"
    elif status == "incomplete":
        phases = phases[:1]
    return {"schema_version": 1, "exit_code": (0 if status == "passed" else 1) if exit_code is None else exit_code,
            "collected": nodes, "outcomes": {node: {"status": status, "reports": copy.deepcopy(phases)} for node in nodes},
            "collection_errors": []}


class ContractAndVerdictTests(unittest.TestCase):
    def test_unsafe_cross_platform_paths_rejected(self):
        for path in ("../secret", "/root", "a/../../b", "a\\b", "a/.git/config", "NUL.py",
                     "shop/refund.py.", "shop//refund.py", "a:b", "a/./b", ".", "a/.GIT/config"):
            with self.subTest(path=path), self.assertRaises(VerificationError):
                safe_relative(path)
        self.assertEqual(safe_relative("源码/refund.py"), "源码/refund.py")

    def test_case_colliding_parent_directories_are_rejected(self):
        seen = {}
        register_path("shop/refund.py", seen)
        with self.assertRaises(VerificationError):
            register_path("Shop/models.py", seen)

    def test_only_complete_normal_reports_can_pass(self):
        self.assertTrue(summarize(report())["passed"])
        for value in (report("skipped"), report("error"), report("incomplete"),
                      report(nodes=[], exit_code=5), report(exit_code=9),
                      report("failed", exit_code=0), report(exit_code=1)):
            self.assertFalse(summarize(value)["passed"])
        self.assertTrue(summarize(report("failed"))["valid"])
        self.assertFalse(summarize(report("failed"))["passed"])

    def test_missing_duplicate_and_mismatched_reports_are_rejected(self):
        contradictory = report()
        next(iter(contradictory["outcomes"].values()))["reports"][1]["outcome"] = "failed"
        for value in ({}, {**report(), "collected": ["a", "a"]},
                      {**report(), "outcomes": {}}, {**report(), "exit_code": True}, contradictory):
            with self.subTest(value=value), self.assertRaises(VerificationError):
                summarize(value)
        with self.assertRaises(VerificationError):
            compare_collection(summarize(report(nodes=["a"])), summarize(report(nodes=["b"])))


class FixtureMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = create_demo(self.root / "demo")
        self.task = TaskSpec.from_dict(self.raw)
        self.patch = self.root / "demo" / "correct.patch"


class WorkspaceTests(FixtureMixin, unittest.TestCase):
    def test_strict_task_fields_and_commit(self):
        for changes in ({"base_commit": "HEAD"}, {"image": "--privileged"},
                        {"request": ""}, {"request": 1},
                        {"timeout_seconds": True}, {"schema_version": 2},
                        {"unknown": 1}, {"editable_paths": ["../escape"]},
                        {"import_modules": []}):
            with self.subTest(changes=changes), self.assertRaises(VerificationError):
                TaskSpec.from_dict({**self.raw, **changes})

    def test_fixed_commit_replay_and_original_not_modified(self):
        source = Path(self.task.repository_path)
        before = manifest(source)
        base, candidate = self.root / "base", self.root / "candidate"
        exported = export_commit(self.task, base)
        self.assertEqual(exported, before)
        actual, policy = apply_candidate(base, candidate, self.patch, self.task)
        self.assertNotEqual(actual, exported)
        self.assertEqual(policy["paths"], ["shop/refund.py"])
        self.assertIn("original - discount", (candidate / "shop/refund.py").read_text())
        self.assertEqual(manifest(source), before)
        self.assertEqual(manifest(base), exported)

    def test_dirty_repository_and_test_patch_rejected(self):
        (Path(self.task.repository_path) / "untracked.txt").write_text("keep me")
        with self.assertRaises(VerificationError) as caught:
            export_commit(self.task, self.root / "base")
        self.assertEqual(caught.exception.reason, "dirty_repository")
        data = self.patch.read_bytes().replace(b"shop/refund.py", b"shop/test_refund.py")
        with self.assertRaises(VerificationError) as caught:
            validate_patch(data, self.task)
        self.assertEqual(caught.exception.status, "POLICY_REJECTED")

    def test_patch_escape_modes_limits_and_invalid_hunks(self):
        original = self.patch.read_bytes()
        patches = [original.replace(b"shop/refund.py", b"../escape.py"),
                   original + b"new mode 120000\n",
                   original.replace(b"b/shop/refund.py", b"b/shop/other.py", 1),
                   b"not a patch", b"\x00binary"]
        for data in patches:
            with self.subTest(data=data[:50]), self.assertRaises(VerificationError):
                validate_patch(data, self.task)
        tight = TaskSpec.from_dict({**self.raw, "max_changed_lines": 1})
        with self.assertRaises(VerificationError):
            validate_patch(original, tight)

    def test_new_file_is_in_manifest_and_bad_context_fails(self):
        data = ("diff --git a/shop/helper.py b/shop/helper.py\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/shop/helper.py\n@@ -0,0 +1 @@\n+VALUE = 1\n")
        new_patch = self.root / "new.patch"
        new_patch.write_text(data, newline="\n")
        base = self.root / "base"
        export_commit(self.task, base)
        files, _ = apply_candidate(base, self.root / "candidate", new_patch, self.task)
        self.assertIn("shop/helper.py", files)
        bad = self.root / "bad.patch"
        bad.write_bytes(self.patch.read_bytes().replace(b"return original\n", b"return missing\n"))
        with self.assertRaises(VerificationError) as caught:
            apply_candidate(base, self.root / "bad-candidate", bad, self.task)
        self.assertEqual(caught.exception.reason, "patch_does_not_apply")

    def test_crlf_transport_patch_is_replayed_without_mutating_input(self):
        original = self.patch.read_bytes().replace(b"\n", b"\r\n")
        crlf = self.root / "crlf.patch"
        crlf.write_bytes(original)
        base = self.root / "base"
        export_commit(self.task, base)
        files, _ = apply_candidate(base, self.root / "candidate", crlf, self.task)
        self.assertIn("shop/refund.py", files)
        self.assertEqual(crlf.read_bytes(), original)


class FakeBackend:
    """State routing only, never presented as Docker/model repair results."""
    def __init__(self, overrides=None):
        self.overrides = overrides or {}

    def resolve_image(self, reference):
        return {"reference": reference, "image_id": "test-only-image"}

    def run(self, source, suite, task, run_id, phase, artifact_dir):
        value = self.overrides.get(phase, report("failed" if phase == "baseline_feedback" else "passed"))
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class PipelineTests(FixtureMixin, unittest.TestCase):
    def verify(self, overrides=None):
        return Verifier(self.root / "runs", FakeBackend(overrides)).verify(self.task, self.patch)

    def test_pipeline_success_and_evidence_manifest(self):
        result = self.verify()
        self.assertEqual(result["status"], "PASSED", result)
        output = Path(result["artifact_directory"])
        self.assertTrue((output / "changes.patch").is_file())
        evidence = json.loads((output / "manifest.json").read_text())
        self.assertIn("result.json", evidence)
        self.assertIn("candidate-tree.json", evidence)
        self.assertEqual(len(result["stages"]), 6)

    def test_baseline_and_feedback_purposes_do_not_run_holdout_or_claim_full_success(self):
        protected = {name: AssertionError("Holdout must not run in this purpose")
                     for name in ("baseline_holdout", "candidate_holdout")}
        verifier = Verifier(self.root / "purpose-runs", FakeBackend(protected))
        baseline = verifier.verify(self.task, purpose="baseline")
        self.assertEqual(baseline["status"], "READY", baseline)
        self.assertEqual(len(baseline["stages"]), 2)
        feedback = verifier.verify(self.task, self.patch, purpose="feedback")
        self.assertEqual(feedback["status"], "FEEDBACK_PASSED", feedback)
        self.assertEqual(len(feedback["stages"]), 4)
        self.assertFalse(any("holdout" in phase for phase in feedback["stages"]))

    def test_candidate_failures_cannot_be_overridden_by_successful_other_stage(self):
        for phase in ("candidate_feedback", "candidate_regression", "candidate_holdout"):
            with self.subTest(phase=phase):
                result = self.verify({phase: report("failed")})
                self.assertEqual(result["status"], "REPAIR_FAILED", result)

    def test_empty_skip_and_bad_baseline_cannot_pass(self):
        for overrides in ({"baseline_feedback": report()},
                          {"baseline_regression": report("failed")},
                          {"candidate_feedback": report(nodes=[], exit_code=5)},
                          {"candidate_feedback": report("skipped")},
                          {"candidate_feedback": report(nodes=["changed"])}):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self.verify(overrides)["status"], "PASSED")

    def test_environment_and_candidate_import_errors_are_distinct(self):
        missing = {"schema_version": 1, "error": "pytest_missing"}
        result = self.verify({"baseline_regression": missing})
        self.assertEqual(result["status"], "ENVIRONMENT_ERROR")
        invalid_source = {"schema_version": 1, "error": "source_import_error"}
        result = self.verify({"candidate_regression": invalid_source})
        self.assertEqual(result["status"], "REPAIR_FAILED")


@unittest.skipUnless(os.environ.get("PATCHTRAIL_DOCKER_TESTS") == "1",
                     "Docker integration requires explicit PATCHTRAIL_DOCKER_TESTS=1")
class RealDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.raw = create_demo(cls.root / "demo", os.environ.get("PATCHTRAIL_TEST_IMAGE", "patchtrail-pytest:0.1"))
        cls.task = TaskSpec.from_dict(cls.raw)
        cls.results = []

    @classmethod
    def tearDownClass(cls):
        output = ROOT / ".test-artifacts" / "verification"
        output.mkdir(parents=True, exist_ok=True)
        (output / "docker-results.json").write_text(json.dumps(cls.results, indent=2), encoding="utf-8")
        cls.temp.cleanup()

    def verify(self, patch="correct", task=None):
        result = Verifier(ROOT / ".test-artifacts" / "verification" / "runs").verify(
            task or self.task, self.root / "demo" / f"{patch}.patch")
        self.results.append(result)
        return result

    def test_correct_patch_passes_in_real_isolated_containers(self):
        before = manifest(self.task.repository_path)
        result = self.verify()
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(len(result["stages"]), 6)
        self.assertTrue(result["environment"]["image_id"].startswith("sha256:"))
        self.assertEqual(manifest(self.task.repository_path), before)
        runtime = json.loads((Path(result["artifact_directory"]) / "verification" /
                              "candidate_feedback" / "runtime-policy.json").read_text())
        self.assertEqual(runtime["network_mode"], "none")
        self.assertTrue(runtime["readonly_rootfs"])
        self.assertTrue(all(mount["read_only"] for mount in runtime["mounts"]))

    def test_wrong_and_regression_patches_fail(self):
        for name in ("wrong", "regression"):
            with self.subTest(patch=name):
                result = self.verify(name)
                self.assertEqual(result["status"], "REPAIR_FAILED", result)

    def test_timeout_is_not_success_and_owned_container_is_removed(self):
        feedback = self.root / "timeout-feedback"
        feedback.mkdir()
        (feedback / "test_timeout.py").write_text("import time\ndef test_timeout():\n    time.sleep(20)\n")
        task = TaskSpec.from_dict({**self.raw, "feedback_dir": str(feedback), "timeout_seconds": 3})
        result = self.verify(task=task)
        self.assertEqual(result["status"], "ENVIRONMENT_ERROR", result)
        self.assertEqual(result["reason_code"], "timeout", result)
        found = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=io.patchtrail.run={result['run_id']}"],
                               capture_output=True, check=True)
        self.assertFalse(found.stdout.strip())

    def test_missing_pytest_does_not_silently_use_host(self):
        image = os.environ.get("PATCHTRAIL_NO_PYTEST_IMAGE")
        if not image:
            self.skipTest("Set PATCHTRAIL_NO_PYTEST_IMAGE to an existing image without pytest")
        task = TaskSpec.from_dict({**self.raw, "image": image})
        result = self.verify(task=task)
        self.assertEqual(result["status"], "ENVIRONMENT_ERROR", result)
        self.assertEqual(result["reason_code"], "pytest_missing", result)

    def test_empty_and_skipped_suites_do_not_pass(self):
        for name, code in (("empty", "VALUE = 1\n"),
                           ("skipped", "import pytest\n@pytest.mark.skip\ndef test_skip():\n    pass\n")):
            with self.subTest(name=name):
                suite = self.root / name
                suite.mkdir()
                (suite / "test_invalid.py").write_text(code, encoding="utf-8", newline="\n")
                task = TaskSpec.from_dict({**self.raw, "regression_dir": str(suite)})
                result = self.verify(task=task)
                self.assertEqual(result["status"], "CONFIG_ERROR", result)
                self.assertEqual(result["reason_code"], "baseline_regression_failed", result)

    def test_missing_report_does_not_pass(self):
        suite = self.root / "missing-report"
        suite.mkdir()
        (suite / "test_exit.py").write_text("import os\nos._exit(0)\n", newline="\n")
        task = TaskSpec.from_dict({**self.raw, "regression_dir": str(suite)})
        result = self.verify(task=task)
        self.assertEqual(result["status"], "ENVIRONMENT_ERROR", result)
        self.assertEqual(result["reason_code"], "report_missing", result)


if __name__ == "__main__":
    unittest.main()
