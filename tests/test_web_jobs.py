"""Web job boundaries and actual Agent/approval integration, without paid calls."""

import json
import os
import sys
import tempfile
import time
import unittest
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from patchtrail.web.jobs import Jobs, execution_lock
from patchtrail.web.service import Workbench, RequestError
from patchtrail.web.store import Store
from patchtrail.domain.task import VerificationError
from test_checkpoint import StatelessModel
from test_verification import FakeBackend
from web_worker import execute


class WebJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "web.sqlite3")
        self.launches = []
        self.jobs = Jobs(self.store, launcher=lambda *a, **k: self.launches.append((a, k)), require_model=False)
        self.service = Workbench(self.store, self.jobs)
        self.conversation = self.store.create()["id"]

    def submit(self, **changes):
        result = self.service.send(self.conversation, {"content": "修复退款金额，减去优惠并保持无优惠行为。",
                    "mode": "repair", "request_id": "request-1", **changes})
        self.identity = result["messages"][-1]["metadata"]["job_id"]
        return result

    def perform(self, *, backend=None, model=None):
        row = self.jobs.row(self.identity)
        execute(self.store.path, self.identity, row["attempt"], model=model or StatelessModel(self.root / "calls.jsonl"),
                backend=backend or FakeBackend())
        return self.jobs.public(self.identity)

    def test_submission_is_durable_and_duplicate_does_not_launch_again(self):
        first = self.submit()
        self.assertEqual(self.submit(), first)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(first["messages"]), 2)
        restarted = Jobs(Store(self.store.path), launcher=lambda *a, **k: None, require_model=False)
        self.assertEqual(restarted.list(self.conversation)[0]["state"], "QUEUED")
        with self.assertRaises(RequestError):
            self.submit(content="different")

    def test_active_job_blocks_conversation_mutation_and_parallel_task(self):
        self.submit()
        for action in (lambda: self.service.delete(self.conversation),
                       lambda: self.service.update(self.conversation, {"repository_path": "different"}),
                       lambda: self.jobs.configure(self.conversation, {"task_source": "demo"}),
                       lambda: self.submit(request_id="new")):
            with self.assertRaises(RequestError):
                action()
        other = self.store.create()["id"]
        with self.assertRaises(RequestError):
            self.jobs.submit(other, {"content": "other", "request_id": "other"})

    def test_verified_job_diff_approval_and_application_are_separate(self):
        self.submit()
        job = self.perform()
        self.assertEqual(job["state"], "WAITING_APPROVAL", job)
        target = Path(job["repository_path"]) / "shop/refund.py"
        self.assertIn("return original\n", target.read_text())
        diff = self.jobs.diff(self.identity)
        self.assertIn("original - discount", diff["diff"])
        self.assertNotIn("api_key", json.dumps(job).lower())
        with self.assertRaises(VerificationError):
            self.jobs.action(self.identity, {"action": "apply", "binding": diff["binding_sha256"]})
        with self.assertRaises(VerificationError):
            self.jobs.action(self.identity, {"action": "approve", "binding": "0" * 64})
        approved = self.jobs.action(self.identity, {"action": "approve", "binding": diff["binding_sha256"]})
        self.assertEqual(approved["state"], "APPROVED")
        self.assertIn("return original\n", target.read_text())
        applied = self.jobs.action(self.identity, {"action": "apply", "binding": diff["binding_sha256"]})
        self.assertEqual(applied["state"], "APPLIED")
        self.assertIn("return original - discount", target.read_text())
        self.assertEqual(self.jobs.action(self.identity, {"action": "apply", "binding": diff["binding_sha256"]})["state"], "APPLIED")

    def test_pause_and_resume_keep_one_job_and_one_run(self):
        self.submit()
        self.jobs.action(self.identity, {"action": "pause"})
        paused = self.perform()
        self.assertEqual(paused["state"], "SUSPENDED", paused)
        run = self.jobs.run_directory(self.identity)
        self.jobs.action(self.identity, {"action": "resume"})
        finished = self.perform()
        self.assertEqual(finished["state"], "WAITING_APPROVAL", finished)
        self.assertEqual(self.jobs.run_directory(self.identity), run)
        self.assertEqual(len(self.store.get(self.conversation)["messages"]), 2)

    def test_dead_worker_reconciles_but_live_lock_is_respected(self):
        self.submit()
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET queued_at=? WHERE id=?", (time.time() - 30, self.identity))
        with execution_lock(self.jobs.directory(self.identity)):
            self.assertEqual(self.jobs.public(self.identity)["state"], "QUEUED")
        self.assertEqual(self.jobs.public(self.identity)["state"], "INTERRUPTED")
        self.jobs.action(self.identity, {"action": "resume"})
        self.assertEqual(len(self.launches), 2)

    def test_stale_attempt_cannot_execute_after_resume(self):
        self.submit()
        old = self.jobs.row(self.identity)["attempt"]
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET queued_at=? WHERE id=?", (time.time() - 30, self.identity))
        self.jobs.action(self.identity, {"action": "resume"})
        execute(self.store.path, self.identity, old, model=StatelessModel(self.root / "unexpected"), backend=FakeBackend())
        self.assertFalse((self.root / "unexpected").exists())

    def test_unsafe_ids_foreign_approval_and_missing_diff_are_rejected(self):
        self.submit()
        for action in (lambda: self.jobs.public("../../outside"), lambda: self.jobs.diff(self.identity),
                       lambda: self.jobs.action(self.identity, {"action": "approve", "approval_id": "foreign"})):
            with self.assertRaises(RequestError):
                action()

    def test_task_snapshot_changes_fail_before_model_call(self):
        self.submit()
        path = self.jobs.directory(self.identity) / "task.json"
        task = json.loads(path.read_text(encoding="utf-8"))
        task["request"] = "changed"
        path.write_text(json.dumps(task), encoding="utf-8")
        result = self.perform()
        self.assertEqual(result["error_code"], "task_changed")
        self.assertFalse((self.root / "calls.jsonl").exists())

    def test_task_file_configuration_matches_repository_and_can_be_restored(self):
        from examples.create_demo import create_demo
        raw = create_demo(self.root / "custom")
        result = self.jobs.configure(self.conversation, {"task_source": "file", "task_path": str(self.root / "custom/task.json")})
        self.assertEqual(result["repository_path"], raw["repository_path"])
        self.submit()
        self.assertEqual(json.loads(self.jobs.row(self.identity)["task"])["repository_path"], raw["repository_path"])

    def test_demo_creation_ignores_inherited_git_worktree_settings(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "foreign.git"), "GIT_WORK_TREE": str(self.root / "foreign")}):
            self.submit()
        task = json.loads(self.jobs.row(self.identity)["task"])
        self.assertTrue((Path(task["repository_path"]) / ".git/HEAD").is_file())
        self.assertFalse((self.root / "foreign.git").exists())

    def test_public_reads_do_not_expose_private_messages_or_credentials(self):
        self.submit()
        self.perform()
        run = self.jobs.run_directory(self.identity)
        (run / "messages.private.json").write_text('"private-secret-sentinel"')
        self.assertNotIn("private-secret-sentinel", json.dumps(self.jobs.public(self.identity)))

    def test_unknown_model_request_requires_explicit_web_retry(self):
        self.submit()
        attempt = self.jobs.row(self.identity)["attempt"]
        code = """
import os,sys
from pathlib import Path
sys.path[:0] = [str(Path(sys.argv[1])/'tests'),sys.argv[1]]
from test_checkpoint import StatelessModel
from test_verification import FakeBackend
from web_worker import execute
execute(Path(sys.argv[2]), sys.argv[3], sys.argv[4], model=StatelessModel(Path(sys.argv[5]),'unknown-model'),backend=FakeBackend())
"""
        process = subprocess.run([sys.executable, "-c", code, str(ROOT), str(self.store.path), self.identity,
                                  attempt, str(self.root / "calls.jsonl")], capture_output=True, timeout=40)
        self.assertEqual(process.returncode, 74, process.stderr)
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET queued_at=? WHERE id=?", (time.time() - 30, self.identity))
        self.jobs.action(self.identity, {"action": "resume"})
        blocked = self.perform()
        self.assertEqual(blocked["state"], "RECOVERY_REQUIRED", blocked)
        self.assertEqual(blocked["error_code"], "model_outcome_unknown")
        self.assertEqual(len((self.root / "calls.jsonl").read_text().splitlines()), 1)
        self.jobs.action(self.identity, {"action": "retry_unknown"})
        row = self.jobs.row(self.identity)
        execute(self.store.path, self.identity, row["attempt"], retry=True,
                model=StatelessModel(self.root / "calls.jsonl"), backend=FakeBackend())
        result = self.jobs.public(self.identity)
        self.assertEqual(result["state"], "WAITING_APPROVAL", result)
        self.assertEqual(result["usage"]["model_calls"], 4)
        self.assertEqual(self.launches[-1][1], {"retry": True})

    @unittest.skipUnless(os.environ.get("PATCHTRAIL_DOCKER_TESTS") == "1", "Explicit real Docker test")
    def test_web_job_with_real_docker_reaches_waiting_approval(self):
        from patchtrail.sandbox.docker import DockerBackend
        self.submit()
        result = self.perform(backend=DockerBackend())
        self.assertEqual(result["state"], "WAITING_APPROVAL", result)
        self.assertEqual(result["verification"]["status"], "PASSED")
        self.assertEqual(result["usage"]["model_calls"], 3)

    @unittest.skipUnless(os.environ.get("PATCHTRAIL_DOCKER_TESTS") == "1", "Explicit real Docker test")
    def test_production_launcher_uses_real_langchain_transport_and_docker(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest.mock import patch
        import threading
        model = StatelessModel(self.root / "http-calls.jsonl")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                response = model.invoke(payload["messages"], [])
                message = {"role": "assistant", "content": response["content"], "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"])}}
                    for call in response["tool_calls"]]}
                result = {"id": "local-http-test", "object": "chat.completion", "created": 1, "model": "local-protocol-test",
                          "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.jobs.launcher = self.jobs.launch
            with patch.dict(os.environ, {"PATCHTRAIL_API_KEY": "local-only-test", "PATCHTRAIL_MODEL": "local-protocol-test",
                                         "PATCHTRAIL_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1"}):
                self.submit()
                process = self.jobs.processes[self.identity]
                try:
                    process.wait(timeout=100)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
            result = self.jobs.public(self.identity)
            self.assertEqual(result["state"], "WAITING_APPROVAL", result)
            self.assertEqual(result["verification"]["status"], "PASSED")
            self.assertEqual(result["usage"]["model_calls"], 3)
            self.assertEqual(len((self.root / "http-calls.jsonl").read_text(encoding="utf-8").splitlines()), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
