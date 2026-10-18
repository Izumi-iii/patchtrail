"""Real-process crash/restart tests with a stateless scripted model; no paid calls."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from examples.create_demo import create_demo
from patchtrail.agents.config import AgentBudget
from patchtrail.agents.runner import AgentRunner
from patchtrail.domain.task import TaskSpec, VerificationError
from test_verification import FakeBackend, report


class StatelessModel:
    is_real_model = False

    def __init__(self, log, fault="", wrong=False):
        self.log, self.fault, self.wrong = Path(log), fault, wrong

    def public(self):
        return {"provider": "scripted-test", "model": "stateless-refund", "wrong_first": self.wrong}

    def invoke(self, messages, tools):
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(messages) + "\n")
        if self.fault == "unknown-model":
            os._exit(74)
        if self.fault == "interrupt-model":
            raise KeyboardInterrupt
        number = sum(m["role"] == "assistant" for m in messages) + 1
        last = messages[-1]
        payload = json.loads(last["content"]) if last["role"] == "tool" else {}
        if "file_sha256" in payload:
            retry = any(m["role"] == "user" and "verification_feedback" in m["content"] for m in messages)
            old = "original + discount" if retry and self.wrong else "original"
            new = "original + discount" if self.wrong and not retry else "original - discount"
            name, args = "edit_files", {"expected_tree_hash": payload["tree_hash"], "edits": [{
                "path": "shop/refund.py", "expected_file_sha256": payload["file_sha256"],
                "old_text": "    return " + old + "\n", "new_text": "    return " + new + "\n"}]}
        elif "candidate_tree_sha256" in payload:
            name, args = "submit_candidate", {"candidate_tree_sha256": payload["candidate_tree_sha256"]}
        else:
            name, args = "read_file", {"path": "shop/refund.py"}
        return {"content": "scripted test action", "tool_calls": [{"id": f"call-{number}", "name": name, "args": args}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, "finish_reason": "tool_calls"}


def worker(arguments):
    """Fault injection is test-side only; production has no crash flags or fake fallback."""
    from patchtrail.agents.persistence import LocalCheckpointSaver
    from patchtrail.tools.code import CodeTools
    mode, root_name, fault, variant = arguments
    root = Path(root_name)
    wrong = variant == "wrong"
    model = StatelessModel(root / "model-calls.jsonl", fault, wrong)
    backend = FakeBackend()
    if variant == "docker":
        from patchtrail.sandbox.docker import DockerBackend
        backend = DockerBackend()
        if fault == "docker-create":
            original_command = backend.command

            def command(args, **kwargs):
                result = original_command(args, **kwargs)
                if args[0] == "create" and any("candidate_feedback" in str(arg) for arg in args):
                    (root / "orphan-container.txt").write_text(result.stdout.decode().strip())
                    os._exit(78)
                return result
            backend.command = command
    if wrong:
        original_run = backend.run

        def stage(source, suite, task, run_id, phase, artifact_dir):
            if phase == "candidate_feedback" and b"original + discount" in (source / "shop/refund.py").read_bytes():
                return report("failed")
            return original_run(source, suite, task, run_id, phase, artifact_dir)
        backend.run = stage
    if variant == "holdout-fail":
        backend.overrides["candidate_holdout"] = report("failed")
    if fault.startswith("receipt-") or fault == "completion-record":
        original_save = LocalCheckpointSaver.save_control

        def save(self, key, value):
            original_save(self, key, value)
            if key.startswith("node:") and value.get("phase") == "done" and value["node"] == fault[8:]:
                os._exit(73)
            if fault == "completion-record" and key == "lifecycle" and value["phase"] == "complete":
                os._exit(79)
        LocalCheckpointSaver.save_control = save
    if fault == "during-edit":
        original_publish = CodeTools._publish_patch

        def publish(self, *args, **kwargs):
            original_publish(self, *args, **kwargs)
            os._exit(75)
        CodeTools._publish_patch = publish
    if fault in ("during-prepare", "during-feedback", "during-final_verification"):
        method = {"during-prepare": "_prepare", "during-feedback": "_verify_feedback",
                  "during-final_verification": "_verify_final"}[fault]
        original = getattr(AgentRunner, method)

        def interrupted(self, state):
            original(self, state)
            os._exit(76)
        setattr(AgentRunner, method, interrupted)
    if fault == "terminal-checkpoint":
        original_put = LocalCheckpointSaver.put

        def put(self, config, checkpoint, metadata, versions):
            result = original_put(self, config, checkpoint, metadata, versions)
            if checkpoint["channel_values"].get("status") == "PASSED":
                os._exit(77)
            return result
        LocalCheckpointSaver.put = put
    runner = AgentRunner(root / "runs", model, backend=backend,
                         budget=AgentBudget(max_model_calls=1 if variant == "one-call" else 12))
    try:
        result = (runner.run(TaskSpec.load(root / "demo/task.json")) if mode == "run" else
                  runner.resume(next((root / "runs").glob("agent-*")), retry_unknown_model_call=mode == "retry"))
    except VerificationError as exc:
        print(json.dumps({"status": exc.status, "reason_code": exc.reason}))
        return 2
    print(json.dumps(result))
    return 0


@unittest.skipUnless(importlib.util.find_spec("langgraph") is not None, "Use the project agent .venv")
class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        create_demo(self.root / "demo")

    def process(self, mode="run", fault="", variant="normal", *, expected=0):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker", mode,
                                 str(self.root), fault, variant], capture_output=True, text=True,
                                encoding="utf-8", timeout=80)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def calls(self):
        path = self.root / "model-calls.jsonl"
        return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0

    def directory(self):
        return next((self.root / "runs").glob("agent-*"))

    def test_model_response_receipt_survives_before_graph_checkpoint(self):
        self.process(fault="receipt-model", expected=73)
        self.assertEqual(self.calls(), 1)
        result = self.process("resume")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(self.calls(), 3)
        self.assertEqual(result["usage"]["model_calls"], 3)
        self.assertEqual(result["usage"]["calls"][0]["total_tokens"], 15)

    def test_completed_edit_receipt_and_candidate_files_restore(self):
        # receipt-tools crashes after the first read tool; during-edit crashes
        # after candidate publication but before the node receipt exists.
        self.process(fault="during-edit", expected=75)
        result = self.process("resume")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(self.calls(), 3)
        self.assertEqual(result["usage"]["tool_calls"], 3)
        self.assertTrue((self.directory() / "proposals/proposal-002/source/shop/refund.py").is_file())
        self.assertIn("return original\n", (self.root / "demo/repository/shop/refund.py").read_text())

    def test_prepare_with_partial_files_is_archived_then_retried(self):
        self.process(fault="during-prepare", expected=76)
        self.assertEqual(self.calls(), 0)
        result = self.process("resume")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertTrue(any((self.directory() / "abandoned").iterdir()))

    def test_feedback_reexecution_preserves_candidate_budget(self):
        self.process(fault="during-feedback", expected=76)
        result = self.process("resume")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(result["usage"]["candidate_count"], 1)
        self.assertEqual(self.calls(), 3)

    def test_feedback_receipt_preserves_retry_history_and_seen_candidates(self):
        self.process(fault="receipt-feedback", variant="wrong", expected=73)
        result = self.process("resume", variant="wrong")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(result["usage"]["candidate_count"], 2)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(self.calls(), 6)
        messages = (self.root / "model-calls.jsonl").read_text()
        self.assertNotIn("holdout", messages.lower())

    def test_final_verification_can_retry_but_cannot_reenter_model(self):
        self.process(fault="during-final_verification", expected=76)
        count = self.calls()
        result = self.process("resume")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(self.calls(), count)

    def test_terminal_checkpoint_before_finalizer_is_recoverable(self):
        self.process(fault="terminal-checkpoint", expected=77)
        result = self.process("resume")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(self.calls(), 3)

    def test_complete_marker_never_precedes_sealed_artifacts(self):
        self.process(fault="completion-record", expected=79)
        path = self.directory() / "manifest.json"
        self.assertTrue(path.exists(), "Completion marker must follow artifact sealing")
        before = path.read_bytes()
        result = self.process("resume")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.calls(), 3)

    def test_unknown_request_is_not_repeated_by_default(self):
        self.process(fault="unknown-model", expected=74)
        for _ in range(2):
            result = self.process("resume")
            self.assertEqual(result["status"], "RECOVERY_REQUIRED", result)
            self.assertEqual(result["reason_code"], "model_outcome_unknown")
            self.assertEqual(result["usage"]["model_calls"], 1)
            self.assertEqual(result["usage"]["calls"], [None])
        self.assertEqual(self.calls(), 1)

    def test_explicit_retry_counts_unknown_request_against_original_budget(self):
        self.process(fault="unknown-model", expected=74)
        result = self.process("retry")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(self.calls(), 4)
        self.assertEqual(result["usage"]["model_calls"], 4)
        self.assertIsNone(result["usage"]["calls"][0])

    def test_retry_cannot_reset_exhausted_model_budget(self):
        self.process(fault="unknown-model", variant="one-call", expected=74)
        result = self.process("retry", variant="one-call")
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED", result)
        self.assertEqual(self.calls(), 1)

    def test_ctrl_c_is_suspended_and_unknown_request_stays_guarded(self):
        result = self.process(fault="interrupt-model")
        self.assertEqual(result["status"], "SUSPENDED", result)
        self.assertTrue(result["resume_available"])
        result = self.process("resume")
        self.assertEqual(result["reason_code"], "model_outcome_unknown")
        self.assertEqual(self.calls(), 1)

    def test_completed_resume_does_not_repeat_calls_or_rewrite_artifacts(self):
        result = self.process()
        manifest = (self.directory() / "manifest.json").read_bytes()
        again = self.process("resume")
        self.assertEqual(again, result)
        self.assertEqual(self.calls(), 3)
        self.assertEqual((self.directory() / "manifest.json").read_bytes(), manifest)

    def test_changed_frozen_suite_blocks_before_next_call(self):
        self.process(fault="receipt-model", expected=73)
        path = self.directory() / "trusted-suites/feedback_dir/test_feedback.py"
        path.write_text("changed\n")
        result = self.process("resume", expected=2)
        self.assertEqual(result["reason_code"], "suite_changed")
        self.assertEqual(self.calls(), 1)

    def test_changed_task_and_model_identity_block_recovery(self):
        self.process(fault="receipt-model", expected=73)
        result = self.process("resume", variant="wrong", expected=2)
        self.assertEqual(result["reason_code"], "resume_identity_changed")
        path = self.directory() / "task.json"
        value = json.loads(path.read_text())
        value["request"] += " changed"
        path.write_text(json.dumps(value))
        result = self.process("resume", expected=2)
        self.assertEqual(result["reason_code"], "task_changed")
        self.assertEqual(self.calls(), 1)

    def test_tampered_checkpoint_rejected(self):
        import sqlite3
        self.process(fault="receipt-model", expected=73)
        with sqlite3.connect(self.directory() / "checkpoints.sqlite3") as db:
            db.execute("UPDATE control SET data=? WHERE key='latest_runtime'", (b"{}",))
        db.close()
        result = self.process("resume", expected=2)
        self.assertEqual(result["reason_code"], "checkpoint_changed")
        self.assertEqual(self.calls(), 1)

    def test_execution_lock_prevents_concurrent_resume(self):
        from patchtrail.agents.persistence import run_lock
        self.process(fault="receipt-model", expected=73)
        with run_lock(self.directory()):
            result = self.process("resume", expected=2)
        self.assertEqual(result["reason_code"], "run_busy")
        self.assertEqual(self.calls(), 1)

    def test_candidate_patch_and_repository_changes_block_resume(self):
        # A model node after the uncommitted edit is still replayable, but no
        # model may run against tampered candidate bytes or a new repository.
        self.process(fault="during-feedback", expected=76)
        patch_file = self.directory() / "proposals/proposal-001/changes.patch"
        before = patch_file.read_bytes()
        patch_file.write_bytes(before + b"changed\n")
        result = self.process("resume", expected=2)
        self.assertEqual(result["reason_code"], "candidate_changed")
        patch_file.write_bytes(before)
        (self.root / "demo/repository/user-notes.txt").write_text("unrelated change")
        result = self.process("resume", expected=2)
        self.assertEqual(result["reason_code"], "repository_changed")
        self.assertEqual(self.calls(), 3)

    def test_resumed_holdout_failure_stays_terminal_and_private(self):
        self.process(fault="receipt-feedback", variant="holdout-fail", expected=73)
        result = self.process("resume", variant="holdout-fail")
        self.assertEqual(result["status"], "REPAIR_FAILED", result)
        self.assertEqual(result["reason_code"], "holdout_failed")
        self.assertFalse(result["resume_available"])
        self.assertEqual(self.calls(), 3)
        self.assertNotIn("holdout", (self.root / "model-calls.jsonl").read_text().lower())

    def test_resume_cli_reaches_human_approval_without_applying(self):
        import contextlib
        import io
        from unittest.mock import patch
        import run_agent
        self.process(fault="receipt-model", expected=73)
        output = io.StringIO()

        def runner(root, model, *, budget):
            return AgentRunner(root, model, budget=budget, backend=FakeBackend())

        with patch.object(sys, "argv", ["run_agent.py", "--resume", str(self.directory()), "--request-approval",
                                       "--approvals-dir", str(self.root / "approvals")]), \
                patch.object(run_agent.ModelSettings, "from_env", return_value=object()), \
                patch.object(run_agent, "LangChainModel", return_value=StatelessModel(self.root / "model-calls.jsonl")), \
                patch.object(run_agent, "AgentRunner", side_effect=runner), contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as stopped:
            run_agent.main()
        self.assertEqual(stopped.exception.code, 0, output.getvalue())
        value = json.loads(output.getvalue())
        self.assertEqual(value["status"], "WAITING_APPROVAL")
        self.assertEqual(value["agent_result"]["status"], "PASSED")
        self.assertEqual(self.calls(), 3)
        self.assertIn("return original\n", (self.root / "demo/repository/shop/refund.py").read_text())

        # A completed resume can reuse an existing decision. Its outer status
        # must reflect APPROVED, rather than falsely reverting to waiting.
        from patchtrail.approvals.service import ApprovalService
        approval = value["approval"]
        ApprovalService(self.root / "approvals").decide(approval["id"], approval["binding_sha256"], "approve")
        output = io.StringIO()
        with patch.object(sys, "argv", ["run_agent.py", "--resume", str(self.directory()), "--request-approval",
                                       "--approvals-dir", str(self.root / "approvals")]), \
                patch.object(run_agent.ModelSettings, "from_env", return_value=object()), \
                patch.object(run_agent, "LangChainModel", return_value=StatelessModel(self.root / "model-calls.jsonl")), \
                patch.object(run_agent, "AgentRunner", side_effect=runner), contextlib.redirect_stdout(output), \
                self.assertRaises(SystemExit) as stopped:
            run_agent.main()
        self.assertEqual(stopped.exception.code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "APPROVED")
        self.assertEqual(self.calls(), 3)
        self.assertIn("return original\n", (self.root / "demo/repository/shop/refund.py").read_text())

    def test_cli_rejects_new_budget_even_with_equals_syntax(self):
        import contextlib
        import io
        from unittest.mock import patch
        import run_agent
        with patch.object(sys, "argv", ["run_agent.py", "--resume", str(self.root), "--max-model-calls=100"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            run_agent.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.calls(), 0)

    @unittest.skipUnless(os.environ.get("PATCHTRAIL_DOCKER_TESTS") == "1", "Explicit real Docker test")
    def test_real_docker_orphan_is_cleaned_before_resumed_validation(self):
        from patchtrail.sandbox.docker import DockerBackend
        backend = DockerBackend()
        self.addCleanup(lambda: backend.command(["rm", "--force", (self.root / "orphan-container.txt").read_text()], check=False)
                        if (self.root / "orphan-container.txt").exists() else None)
        self.process(fault="docker-create", variant="docker", expected=78)
        cid = (self.root / "orphan-container.txt").read_text()
        self.assertEqual(backend.command(["inspect", cid], check=False).returncode, 0)
        result = self.process("resume", variant="docker")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertNotEqual(backend.command(["inspect", cid], check=False).returncode, 0)
        self.assertEqual(self.calls(), 3)
        self.assertIn("interrupted_verifier_cleaned", (self.directory() / "events.jsonl").read_text())


@unittest.skipUnless(importlib.util.find_spec("langgraph") is not None, "Use the project agent .venv")
class SaverContractTests(unittest.TestCase):
    def test_pending_writes_versions_namespaces_and_history_survive_reopen(self):
        from patchtrail.agents.persistence import LocalCheckpointSaver
        from langgraph.checkpoint.base import empty_checkpoint
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            saver = LocalCheckpointSaver(path)
            config = {"configurable": {"thread_id": "test", "checkpoint_ns": ""}}
            first = empty_checkpoint()
            first["channel_values"] = {"messages": [{"role": "user", "content": "test"}]}
            first["channel_versions"] = {"messages": 1}
            metadata = {"source": "input", "step": -1, "parents": {}}
            parent = saver.put(config, first, metadata, {"messages": 1})
            saver.put_writes(parent, [("messages", ["original"]), ("__error__", "first-error")], "task")
            saver.put_writes(parent, [("messages", ["duplicate"]), ("__error__", "updated-error")], "task")
            second = empty_checkpoint()
            second["channel_values"] = {"messages": ["next"]}
            second["channel_versions"] = {"messages": saver.get_next_version(1, None)}
            latest = saver.put(parent, second, {**metadata, "source": "loop", "step": 0}, {"messages": 2})
            reopened = LocalCheckpointSaver(path)
            saved = reopened.get_tuple(config)
            self.assertEqual(saved.config, latest)
            self.assertEqual(saved.parent_config, parent)
            self.assertEqual(saved.checkpoint["channel_values"], {"messages": ["next"]})
            previous = reopened.get_tuple(parent)
            self.assertIn(("task", "messages", ["original"]), previous.pending_writes)
            self.assertIn(("task", "__error__", "updated-error"), previous.pending_writes)
            self.assertEqual(len(list(reopened.list(config))), 2)
            self.assertEqual(len(list(reopened.list(config, before=latest))), 1)
            self.assertEqual(len(list(reopened.list(config, filter={"source": "loop"}, limit=1))), 1)
            self.assertIsNone(reopened.get_tuple({"configurable": {"thread_id": "other"}}))

    def test_docker_cleanup_refuses_foreign_container_ownership(self):
        from patchtrail.sandbox.docker import DockerBackend
        from types import SimpleNamespace
        backend = DockerBackend()
        calls = []

        def command(arguments, **kwargs):
            calls.append(arguments)
            if arguments[0] == "ps":
                return SimpleNamespace(stdout=("a" * 64).encode())
            return SimpleNamespace(stdout=json.dumps([{"Id": "a" * 64, "Config": {
                "Labels": {"io.patchtrail.run": "verify-" + "b" * 32}}}]).encode())
        backend.command = command
        with self.assertRaises(VerificationError) as stopped:
            backend.cleanup_run("verify-" + "c" * 32)
        self.assertEqual(stopped.exception.reason, "cleanup_owner_changed")
        self.assertFalse(any(call[0] == "rm" for call in calls))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        raise SystemExit(worker(sys.argv[2:]))
    unittest.main()
