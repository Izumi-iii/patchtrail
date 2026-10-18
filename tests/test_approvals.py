"""Approval effects use real temporary Git repositories; no provider calls.

FakeBackend supplies verifier stage reports only. These are approval tests,
not claims of model or Docker repair success.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from patchtrail.approvals.service import ApprovalService, repository_lock
from patchtrail.domain.task import VerificationError
from patchtrail.evaluation.dataset import load_dataset, materialize
from patchtrail.verification.service import Verifier, write_json
from patchtrail.workspace.snapshot import digest, git
from test_verification import FakeBackend


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.case = next(c for c in load_dataset()[0]["cases"] if c["id"] == "checkout-discount")
        self.task = materialize(self.case, self.root / "fixture", "test-only-image")
        self.repo = Path(self.task.repository_path)
        self.run = self.root / "agent-run"
        self.run.mkdir()
        final = Verifier(self.run / "verification-runs", FakeBackend()).verify(
            self.task, self.root / "fixture/corrected.patch")
        self.assertEqual(final["status"], "PASSED", final)
        self.final = Path(final["artifact_directory"])
        write_json(self.run / "task.json", self.task.to_dict())
        (self.run / "changes.patch").write_bytes((self.final / "changes.patch").read_bytes())
        write_json(self.run / "result.json", {
            "run_id": "test-agent-run", "task_id": self.task.task_id, "status": "PASSED",
            "base_commit": self.task.base_commit, "model_mode": "scripted_test",
            "final_verification_ref": str(self.final), "candidates": [{
                "patch_sha256": final["patch_sha256"], "tree_sha256": final["candidate_tree_sha256"]}]})
        self.refresh_manifest(self.run)
        self.now = 1_800_000_000
        self.store = self.root / "approvals"
        self.service = ApprovalService(self.store, clock=lambda: self.now)

    @staticmethod
    def refresh_manifest(folder):
        # Agent manifest deliberately excludes nested verifier directories.
        write_json(folder / "manifest.json", {p.name: {"sha256": digest(p.read_bytes())}
                   for p in folder.iterdir() if p.is_file() and p.name != "manifest.json"})

    def request(self):
        self.record = self.service.request(self.run)
        self.identity, self.binding = self.record["id"], self.record["binding_sha256"]
        return self.record

    def approve(self):
        self.request()
        return self.service.decide(self.identity, self.binding, "approve")

    def assert_error(self, reason, method, *args, **kwargs):
        with self.assertRaises(VerificationError) as caught:
            method(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)

    def assert_baseline(self):
        for name, code in self.case["source"].items():
            self.assertEqual((self.repo / name).read_bytes(), code.encode())

    def assert_candidate(self):
        for name, code in self.case["corrected"].items():
            self.assertEqual((self.repo / name).read_bytes(), code.encode())

    def interrupt(self, *, all_files=False):
        self.approve()
        original = self.service._replace_file
        calls = []

        def crash(*args):
            original(*args)
            calls.append(args[2])
            if not all_files or len(calls) == len(self.case["corrected"]):
                raise SystemExit("simulated abrupt process interruption")

        with patch.object(self.service, "_replace_file", side_effect=crash), self.assertRaises(SystemExit):
            self.service.apply(self.identity, self.binding)
        self.assertEqual(self.service.get(self.identity)["state"], "APPLYING")

    def test_freeze_review_and_duplicate_request_do_not_modify_repository(self):
        record = self.request()
        self.assertEqual(record["state"], "WAITING_APPROVAL")
        self.assertEqual(self.service.request(self.run)["id"], self.identity)
        shown = self.service.get(self.identity, include_diff=True)
        self.assertIn("price - discount", shown["diff"])
        self.assertEqual(len(shown["changes"]), 2)
        self.assert_baseline()

    def test_approval_then_apply_and_idempotency(self):
        self.approve()
        self.assert_baseline()
        applied = self.service.apply(self.identity, self.binding)
        self.assertEqual(applied["state"], "APPLIED")
        self.assert_candidate()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.decode().strip(), self.task.base_commit)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only").stdout, b"")
        with patch.object(self.service, "_replace_file", side_effect=AssertionError("must not write")):
            self.assertEqual(self.service.apply(self.identity, self.binding)["state"], "APPLIED")
        self.assertEqual(sum(e["kind"] == "applied" for e in self.service.get(self.identity)["events"]), 1)

    def test_reject_and_conflicting_decision(self):
        self.request()
        self.assertEqual(self.service.decide(self.identity, self.binding, "reject")["state"], "REJECTED")
        self.service.decide(self.identity, self.binding, "reject")
        self.assert_error("decision_conflict", self.service.decide, self.identity, self.binding, "approve")
        self.assert_error("not_approved", self.service.apply, self.identity, self.binding)
        self.assert_baseline()

    def test_apply_requires_explicit_approval_and_current_binding(self):
        self.request()
        self.assert_error("not_approved", self.service.apply, self.identity, self.binding)
        self.assert_error("stale_binding", self.service.decide, self.identity, "0" * 64, "approve")
        self.assertEqual(self.service.get(self.identity)["state"], "WAITING_APPROVAL")

    def test_duplicate_approval_has_one_decision_event(self):
        self.approve()
        self.service.decide(self.identity, self.binding, "approve")
        self.assertEqual(sum(e["kind"] == "decided" for e in self.service.get(self.identity)["events"]), 1)

    def test_expired_waiting_and_approved_records_cannot_write(self):
        self.request()
        self.now += 86400
        self.assertEqual(self.service.get(self.identity)["state"], "EXPIRED")
        self.assert_error("approval_expired", self.service.decide, self.identity, self.binding, "approve")
        self.assert_baseline()

    def test_expired_after_approval_cannot_apply(self):
        self.approve()
        self.now += 86400
        self.assert_error("approval_expired", self.service.apply, self.identity, self.binding)
        self.assertEqual(self.service.get(self.identity)["state"], "EXPIRED")
        self.assert_baseline()

    def test_changed_bundle_invalidates_approval(self):
        self.approve()
        (self.store / self.identity / "candidate/pricing.py").write_bytes(b"unreviewed\n")
        self.assert_error("artifact_changed", self.service.apply, self.identity, self.binding)
        self.assertEqual(self.service.get(self.identity)["state"], "INVALIDATED")
        self.assert_baseline()

    def test_changed_record_file_hash_is_bound(self):
        self.approve()
        record = self.service._load(self.identity)
        record["changes"]["pricing.py"]["after"] = "0" * 64
        self.service._save(record, "test_tampering")
        self.assert_error("binding_changed", self.service.apply, self.identity, self.binding)
        self.assert_baseline()

    def test_unrelated_untracked_file_blocks_approval(self):
        self.request()
        (self.repo / "notes.txt").write_text("user data")
        self.assert_error("workspace_changed", self.service.decide, self.identity, self.binding, "approve")
        self.assertEqual((self.repo / "notes.txt").read_text(), "user data")
        self.assertEqual(self.service.get(self.identity)["state"], "INVALIDATED")

    def test_index_changes_block_apply_even_when_worktree_restored(self):
        self.approve()
        target = self.repo / "pricing.py"
        original = target.read_bytes()
        target.write_bytes(b"staged user change\n")
        git(self.repo, "add", "--", "pricing.py")
        target.write_bytes(original)
        self.assert_error("workspace_changed", self.service.apply, self.identity, self.binding)
        self.assert_baseline()

    def test_head_change_blocks_apply(self):
        self.approve()
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "External commit")
        self.assert_error("workspace_changed", self.service.apply, self.identity, self.binding)
        self.assert_baseline()

    def test_changed_target_blocks_apply_without_overwriting(self):
        self.approve()
        target = self.repo / "pricing.py"
        target.write_bytes(b"external edit\n")
        self.assert_error("workspace_changed", self.service.apply, self.identity, self.binding)
        self.assertEqual(target.read_bytes(), b"external edit\n")

    def test_restart_and_archived_original_run_use_frozen_bundle(self):
        self.approve()
        self.run.rename(self.root / "archived-agent-run")
        restarted = ApprovalService(self.store, clock=lambda: self.now)
        self.assertIn("net_price", restarted.get(self.identity, include_diff=True)["diff"])
        self.assertEqual(restarted.apply(self.identity, self.binding)["state"], "APPLIED")
        self.assert_candidate()

    def test_partial_apply_recovers_without_rewriting_completed_file(self):
        self.interrupt()
        self.assert_error("recovery_required", self.service.apply, self.identity, self.binding)
        restarted = ApprovalService(self.store, clock=lambda: self.now)
        before = (self.repo / "checkout.py").stat().st_mtime_ns
        self.assertEqual(restarted.apply(self.identity, self.binding, recover=True)["state"], "APPLIED")
        self.assertEqual((self.repo / "checkout.py").stat().st_mtime_ns, before)
        self.assert_candidate()

    def test_recovery_stops_at_external_edit(self):
        self.interrupt()
        (self.repo / "pricing.py").write_bytes(b"external after crash\n")
        self.assert_error("workspace_changed", self.service.apply, self.identity, self.binding, recover=True)
        self.assertEqual(self.service.get(self.identity)["state"], "RECOVERY_REQUIRED")
        self.assertEqual((self.repo / "pricing.py").read_bytes(), b"external after crash\n")

    def test_expired_partial_operation_cannot_write_remaining_file(self):
        self.interrupt()
        self.now += 86400
        self.assert_error("approval_expired", self.service.apply, self.identity, self.binding, recover=True)
        self.assertEqual((self.repo / "pricing.py").read_bytes(), self.case["source"]["pricing.py"].encode())

    def test_expired_complete_operation_can_reconcile_without_writes(self):
        self.interrupt(all_files=True)
        self.now += 86400
        with patch.object(self.service, "_replace_file", side_effect=AssertionError("must not write")):
            self.assertEqual(self.service.apply(self.identity, self.binding, recover=True)["state"], "APPLIED")
        self.assert_candidate()

    def test_expiry_during_multifile_apply_stops_next_write(self):
        self.approve()
        original = self.service._replace_file

        def expire(*args):
            original(*args)
            self.now += 86400

        with patch.object(self.service, "_replace_file", side_effect=expire):
            self.assert_error("approval_expired", self.service.apply, self.identity, self.binding)
        self.assertEqual(self.service.get(self.identity)["state"], "RECOVERY_REQUIRED")
        self.assertEqual((self.repo / "pricing.py").read_bytes(), self.case["source"]["pricing.py"].encode())

    def test_changed_temporary_journal_is_rejected(self):
        self.interrupt()
        record = self.service._load(self.identity)
        record["operation"]["temporary_paths"][0] = "outside.py"
        self.service._save(record, "test_tampering")
        self.assert_error("journal_changed", self.service.apply, self.identity, self.binding, recover=True)
        self.assertFalse((self.repo / "outside.py").exists())

    def test_failed_run_and_changed_run_artifacts_are_rejected(self):
        result = json.loads((self.run / "result.json").read_text())
        result["status"] = "REPAIR_FAILED"
        write_json(self.run / "result.json", result)
        self.refresh_manifest(self.run)
        self.assert_error("run_not_passed", self.service.request, self.run)
        (self.run / "changes.patch").write_bytes(b"changed")
        self.assert_error("artifact_changed", self.service.request, self.run)

    def test_policy_version_change_invalidates_prior_approval(self):
        self.approve()
        with patch("patchtrail.approvals.service.POLICY_VERSION", "future-policy"):
            self.assert_error("policy_changed", self.service.apply, self.identity, self.binding)
        self.assert_baseline()

    def test_protected_path_remains_rejected_even_if_run_claims_passed(self):
        patch_file = self.run / "changes.patch"
        altered = patch_file.read_bytes().replace(b"pricing.py", b"test_pricing.py")
        patch_file.write_bytes(altered)
        (self.final / "changes.patch").write_bytes(altered)
        final = json.loads((self.final / "result.json").read_text())
        final["patch_sha256"] = digest(altered)
        write_json(self.final / "result.json", final)
        self.refresh_manifest(self.final)
        result = json.loads((self.run / "result.json").read_text())
        result["candidates"][-1]["patch_sha256"] = digest(altered)
        write_json(self.run / "result.json", result)
        self.refresh_manifest(self.run)
        self.assert_error("protected_path", self.service.request, self.run)
        self.assert_baseline()

    def test_actual_graph_artifacts_and_run_agent_approval_flag(self):
        import contextlib
        import io
        import run_agent
        from examples.create_demo import create_demo
        from patchtrail.agents.runner import AgentRunner
        from patchtrail.domain.task import TaskSpec
        from test_agent import ScriptedModel

        task = TaskSpec.from_dict(create_demo(self.root / "graph-fixture"))
        result = AgentRunner(self.root / "graph-runs", ScriptedModel(), backend=FakeBackend()).run(task)
        self.assertEqual(result["status"], "PASSED", result)
        before = (Path(result["artifact_directory"]) / "manifest.json").read_bytes()
        output = io.StringIO()
        with patch.object(sys, "argv", ["run_agent.py", "--task", str(self.root / "graph-fixture/task.json"),
                                       "--request-approval", "--approvals-dir", str(self.root / "graph-approvals")]), \
                patch.object(run_agent.ModelSettings, "from_env", return_value=object()), \
                patch.object(run_agent, "LangChainModel"), patch.object(run_agent, "AgentRunner") as runner, \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_result:
            runner.return_value.run.return_value = result
            run_agent.main()
        self.assertEqual(exit_result.exception.code, 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["status"], "WAITING_APPROVAL")
        self.assertEqual(record["approval"]["identity"]["model_mode"], "scripted_test")
        self.assertEqual((Path(result["artifact_directory"]) / "manifest.json").read_bytes(), before)
        self.assertIn("return original\n", (Path(task.repository_path) / "shop/refund.py").read_text())

    def test_cli_restarts_and_os_lock_are_enforced_across_processes(self):
        self.request()
        command = [sys.executable, str(ROOT / "approval.py"), "--store", str(self.store)]
        # Use a clock-independent approval expiry for subprocesses.
        record = self.service._load(self.identity)
        record["expires_at"] = 4_000_000_000
        record["binding_sha256"] = self.service._binding(record)
        self.binding = record["binding_sha256"]
        self.service._save(record, "test_clock_adjustment")
        approve = subprocess.run(command + ["approve", "--id", self.identity, "--binding", self.binding],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(approve.returncode, 0, approve.stdout + approve.stderr)
        with repository_lock(self.repo):
            blocked = subprocess.run(command + ["apply", "--id", self.identity, "--binding", self.binding],
                                     capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(blocked.stdout)["reason_code"], "repository_busy")
        crash_code = """
import os, sys
sys.path.insert(0, sys.argv[1])
from patchtrail.approvals.service import ApprovalService
s = ApprovalService(sys.argv[2])
original = s._replace_file
def crash(*args):
    original(*args)
    os._exit(73)
s._replace_file = crash
s.apply(sys.argv[3], sys.argv[4])
"""
        crashed = subprocess.run([sys.executable, "-c", crash_code, str(ROOT / "src"), str(self.store),
                                  self.identity, self.binding], capture_output=True, timeout=30)
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        recovered = subprocess.run(command + ["recover", "--id", self.identity, "--binding", self.binding],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["state"], "APPLIED")
        self.assert_candidate()


if __name__ == "__main__":
    unittest.main()
