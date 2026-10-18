"""Transport, persistence, and read-only inspection tests. No paid model calls."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from patchtrail.web.server import LocalServer
from patchtrail.web.service import RequestError, Workbench, inspect_repository
from patchtrail.web.store import Store


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True).stdout.decode().strip()


def make_repository(path):
    path.mkdir()
    git(path, "init")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "-c", "user.name=PatchTrail Test", "-c", "user.email=test@localhost",
        "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
    return path


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = make_repository(Path(self.temp.name) / "sample")

    def test_inspection_reads_real_metadata_and_does_not_change_repository(self):
        before = {p.name: p.read_bytes() for p in self.repository.glob("*.py")}
        result = inspect_repository(str(self.repository))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["commit"], git(self.repository, "rev-parse", "HEAD"))
        self.assertEqual(result["python_files"], 2)
        self.assertEqual(result["test_files"], 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.repository.glob("*.py")})
        self.assertEqual(git(self.repository, "status", "--porcelain"), "")

    def test_dirty_repository_is_reported_without_resetting(self):
        (self.repository / "calc.py").write_text("changed\n", encoding="utf-8")
        result = inspect_repository(str(self.repository))
        self.assertEqual(result["status"], "dirty")
        self.assertTrue(result["dirty"])
        self.assertEqual((self.repository / "calc.py").read_text(), "changed\n")

    def test_invalid_relative_non_repository_and_subdirectory(self):
        self.assertEqual(inspect_repository("relative/path")["status"], "invalid")
        self.assertEqual(inspect_repository(str(self.repository.parent))["status"], "invalid")
        self.assertEqual(inspect_repository(str(self.repository / "missing"))["status"], "invalid")
        child = self.repository / "child"
        child.mkdir()
        self.assertIn("根目录", inspect_repository(str(child))["error"])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "workbench.sqlite3")
        self.service = Workbench(self.store)
        self.item = self.store.create()

    def send(self, **changes):
        return self.service.send(self.item["id"],
                                 {"content": "修复退款金额", "request_id": "request-1", **changes})

    def test_messages_persist_restart_and_never_claim_model_execution(self):
        result = self.send()
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["title"], "修复退款金额")
        extra = result["messages"][1]["metadata"]
        self.assertFalse(extra["agent_connected"])
        self.assertFalse(extra["tests_executed"])
        restarted = Store(self.store.path)
        self.assertEqual(restarted.get(self.item["id"]), result)

    def test_idempotent_submission_and_conflicting_reuse(self):
        result = self.send()
        self.assertEqual(self.send(), result)
        with self.assertRaises(RequestError) as caught:
            self.send(content="different")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store.get(self.item["id"])["messages"]), 2)

    def test_validation_and_safe_rename(self):
        for content in (" ", 3, "x" * 12001):
            with self.assertRaises(RequestError):
                self.send(content=content)
        with self.assertRaises(RequestError):
            self.send(mode="repair")
        with self.assertRaises(RequestError):
            self.service.update(self.item["id"], {"id": "injected"})
        updated = self.service.update(self.item["id"], {"title": "退款修复"})
        self.assertEqual(updated["title"], "退款修复")

    def test_conversations_are_isolated_and_delete_cascades(self):
        other = self.store.create()
        self.send(mode="note")
        self.assertEqual(self.store.get(other["id"])["messages"], [])
        self.service.delete(self.item["id"])
        with self.assertRaises(KeyError):
            self.store.get(self.item["id"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)

    def test_busy_conversation_rejects_duplicate_send_mutation_and_delete(self):
        entered, release = threading.Event(), threading.Event()
        failures = []

        def slow_answer(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test timed out")
            return "真实检查完成", {"agent_connected": False}

        def worker():
            try:
                self.send()
            except Exception as exc:
                failures.append(exc)

        with patch("patchtrail.web.service.prepare_answer", slow_answer):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                for operation in (lambda: self.send(request_id="second"),
                                  lambda: self.service.update(self.item["id"], {"title": "changed"}),
                                  lambda: self.service.delete(self.item["id"])):
                    with self.assertRaises(RequestError) as caught:
                        operation()
                    self.assertEqual(caught.exception.status, 409)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(failures)
        self.assertEqual(len(self.store.get(self.item["id"])["messages"]), 2)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = LocalServer(("127.0.0.1", 0), Store(Path(cls.temp.name) / "web.sqlite3"))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.temp.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        defaults = {"Content-Type": "application/json", "X-PatchTrail": "1"}
        defaults.update(headers or {})
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, method=method, data=payload,
                                         headers=defaults)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            content = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            return response.status, content, response.headers

    def test_static_assets_health_and_no_arbitrary_file_access(self):
        for path in ("/", "/app.js", "/styles.css", "/favicon.svg"):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertTrue(self.request("/api/healthz")[1]["agent_connected"])
        for path in ("/.git/config", "/Docs/PROJECT_DESIGN.md", "/../app.py"):
            self.assertEqual(self.request(path)[0], 404)

    def test_http_conversation_round_trip_and_invalid_messages(self):
        status, item, _ = self.request("/api/conversations", "POST", {})
        self.assertEqual(status, 201)
        path = "/api/conversations/" + item["id"]
        self.assertEqual(self.request(path, "PATCH", {"title": "测试任务"})[1]["title"], "测试任务")
        response = self.request(path + "/messages", "POST", {
            "content": "<script>alert(1)</script>", "mode": "note", "request_id": "http1"})
        self.assertEqual(response[0], 200)
        self.assertEqual(len(response[1]["messages"]), 2)
        self.assertEqual(self.request(path + "/messages", "POST", {})[0], 400)
        self.assertEqual(self.request(path, "DELETE")[0], 200)
        self.assertEqual(self.request(path)[0], 404)

    def test_cross_site_and_dns_rebinding_requests_rejected(self):
        self.assertEqual(self.request("/api/conversations", "POST", {},
                                      {"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.request("/api/healthz", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/conversations", "POST", {},
                                      {"X-PatchTrail": ""})[0], 403)
        job = "job-" + "a" * 32
        self.assertEqual(self.request(f"/api/jobs/{job}/actions", "POST", {"action": "apply"},
                                     {"Origin": "https://untrusted.example"})[0], 403)

    def test_task_configuration_job_lists_and_unknown_job_are_guarded(self):
        _, item, _ = self.request("/api/conversations", "POST", {})
        path = "/api/conversations/" + item["id"]
        self.assertEqual(self.request(path + "/jobs")[1], {"jobs": []})
        self.assertEqual(self.request(path + "/task", "POST", {"task_source": "demo"})[1]["task_source"], "demo")
        self.assertEqual(self.request(path + "/task", "POST", {"task_source": "file", "task_path": "relative.json"})[0], 400)
        self.assertEqual(self.request("/api/jobs/job-" + "b" * 32)[0], 404)

    def test_http_repair_retry_is_idempotent_without_starting_a_paid_worker(self):
        _, item, _ = self.request("/api/conversations", "POST", {})
        path = "/api/conversations/" + item["id"]
        jobs = self.server.jobs
        with patch.object(jobs, "require_model", False), patch.object(jobs, "launcher") as launch:
            payload = {"mode": "repair", "content": "Fix refund", "request_id": "repair-http"}
            first = self.request(path + "/messages", "POST", payload)
            second = self.request(path + "/messages", "POST", payload)
            self.assertEqual(first[0], 200)
            self.assertEqual(first[1], second[1])
            self.assertEqual(launch.call_count, 1)
            identity = first[1]["messages"][-1]["metadata"]["job_id"]
            self.assertEqual(self.request(f"/api/jobs/{identity}/diff")[0], 409)
            # Leave this shared HTTP test server without an active task.
            row = jobs.row(identity)
            jobs.finish(identity, row["attempt"], "FAILED")

    def test_non_object_and_oversized_input(self):
        self.assertEqual(self.request("/api/conversations", "POST", [1, 2])[0], 400)
        self.assertEqual(self.request("/api/conversations", "POST", {"large": "x" * 70000})[0], 413)


if __name__ == "__main__":
    unittest.main()
