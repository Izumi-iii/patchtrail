"""Framework, tools and local HTTP protocol tests; no real provider calls."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from examples.create_demo import create_demo
from patchtrail.agents.config import AgentBudget, ModelSettings, read_model_env
from patchtrail.agents.runner import AgentRunner
from patchtrail.domain.task import TaskSpec, VerificationError
from patchtrail.tools.code import CodeTools, TOOLS
from patchtrail.verification.service import Verifier
from patchtrail.workspace.snapshot import export_commit, manifest
from test_verification import FakeBackend

HAS_AGENT = importlib.util.find_spec("langgraph") is not None and importlib.util.find_spec("langchain_openai") is not None


class AgentFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = create_demo(self.root / "demo")
        self.task = TaskSpec.from_dict(self.raw)
        self.base = self.root / "base"
        export_commit(self.task, self.base)
        self.correct = (self.root / "demo/correct.patch").read_text(encoding="utf-8")
        self.wrong = (self.root / "demo/wrong.patch").read_text(encoding="utf-8")


class ToolsTests(AgentFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tools = CodeTools(self.base, self.root / "proposals", self.task, allow_legacy_patch=True)

    def test_bounded_file_search_read_and_ast(self):
        listing = self.tools.invoke("list_files", {"limit": 1})
        self.assertTrue(listing["ok"])
        self.assertTrue(listing["truncated"])
        self.assertEqual(len(listing["paths"]), 1)
        self.assertEqual(self.tools.invoke("search_code", {"query": "return original"})["matches"][0]["path"], "shop/refund.py")
        read = self.tools.invoke("read_file", {"path": "shop/refund.py"})
        self.assertIn("2:     return original", read["content"])
        symbol = self.tools.invoke("find_symbol", {"name": "refund_amount"})
        self.assertEqual(symbol["definitions"][0]["line"], 1)
        self.assertEqual(symbol["resolution"], "syntax_only")

    def test_unknown_arguments_escape_and_non_source_reads_are_rejected(self):
        for name, args in (("shell", {"cmd": "whoami"}), ("read_file", {"path": "../secret"}),
                           ("read_file", {"path": ".env"}), ("list_files", {"limit": True}),
                           ("read_file", {"path": "shop/refund.py", "extra": "x"}),
                           ("read_file", {"path": "shop/refund.py", "end_line": 500})):
            with self.subTest(name=name, args=args):
                self.assertFalse(self.tools.invoke(name, args)["ok"])

    def test_candidate_hash_stale_edit_and_original_preservation(self):
        before = manifest(self.base)
        initial = self.tools.tree
        candidate = self.tools.invoke("propose_patch", {"unified_diff": self.correct, "expected_tree_hash": initial})
        self.assertTrue(candidate["ok"])
        self.assertNotEqual(candidate["candidate_tree_sha256"], initial)
        stale = self.tools.invoke("propose_patch", {"unified_diff": self.wrong, "expected_tree_hash": initial})
        self.assertFalse(stale["ok"])
        bad_submission = self.tools.invoke("submit_candidate", {"candidate_tree_sha256": initial})
        self.assertFalse(bad_submission["ok"])
        submitted = self.tools.invoke("submit_candidate", {"candidate_tree_sha256": self.tools.tree})
        self.assertTrue(submitted["submitted"])
        self.assertEqual(before, manifest(self.base))

    def test_protected_patch_is_a_terminal_policy_error(self):
        patch_text = self.correct.replace("shop/refund.py", "shop/test_refund.py")
        with self.assertRaises(VerificationError) as caught:
            self.tools.invoke("propose_patch", {"unified_diff": patch_text, "expected_tree_hash": self.tools.tree})
        self.assertEqual(caught.exception.status, "POLICY_REJECTED")

    def test_plain_unified_diff_is_repairable_but_unsafe_headers_remain_terminal(self):
        plain = self.correct.split("\n", 1)[1]
        output = self.tools.invoke("propose_patch", {"unified_diff": plain, "expected_tree_hash": self.tools.tree})
        self.assertFalse(output["ok"])
        self.assertTrue(output["retryable"])
        self.assertEqual(output["error_code"], "missing_git_header")
        for unsafe in (plain.replace("shop/refund.py", "../escape.py"),
                       plain.replace("shop/refund.py", "tests/test_refund.py")):
            with self.assertRaises(VerificationError) as caught:
                self.tools.invoke("propose_patch", {"unified_diff": unsafe, "expected_tree_hash": self.tools.tree})
            self.assertEqual(caught.exception.status, "POLICY_REJECTED")

    def test_next_patch_is_cumulative_against_original_base(self):
        first = self.tools.invoke("propose_patch", {"unified_diff": self.wrong, "expected_tree_hash": self.tools.tree})
        second = self.tools.invoke("propose_patch", {"unified_diff": self.correct, "expected_tree_hash": first["tree_hash"]})
        self.assertTrue(second["ok"])
        self.assertIn("original - discount", (self.tools.current / "shop/refund.py").read_text())


class ConfigTests(unittest.TestCase):
    def test_project_env_is_explicit_non_executing_and_environment_can_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / ".env"
            env.write_text("# local test\nDEEPSEEK_API_KEY='local-only'\nPATCHTRAIL_MODEL=deepseek-v4-pro\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                settings = ModelSettings.from_env(env_file=env)
                self.assertEqual(settings.api_key, "local-only")
                self.assertEqual(settings.model, "deepseek-v4-pro")
            with patch.dict(os.environ, {"PATCHTRAIL_MODEL": "deepseek-flash"}, clear=True):
                self.assertEqual(ModelSettings.from_env(env_file=env).model, "deepseek-flash")
            env.write_text("DEEPSEEK_API_KEY=first\nDEEPSEEK_API_KEY=second\n")
            with self.assertRaises(VerificationError):
                read_model_env(env)
            env.write_text("RUN_COMMAND=$(echo forbidden)\n")
            with self.assertRaises(VerificationError):
                read_model_env(env)

    def test_deepseek_defaults_and_key_redaction(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "not-a-real-key"}, clear=True):
            settings = ModelSettings.from_env()
        self.assertEqual(settings.model, "deepseek-flash")
        self.assertEqual(settings.base_url, "https://api.deepseek.com")
        self.assertNotIn("not-a-real-key", repr(settings))
        self.assertNotIn("not-a-real-key", json.dumps(settings.public()))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(VerificationError):
            ModelSettings.from_env()

    def test_insecure_urls_and_invalid_budgets_rejected(self):
        for url in ("http://remote.example/v1", "https://user:password@example.com", "https://x.test/?key=secret", "https://[invalid"):
            with self.subTest(url=url), self.assertRaises(VerificationError):
                ModelSettings(api_key="fake", base_url=url).validate()
        with self.assertRaises(VerificationError):
            AgentBudget(max_candidates=True).validate()


class ScriptedModel:
    """Test-only deterministic calls, never available as a production fallback."""
    is_real_model = False

    def __init__(self, replacement="original - discount", wrong_first=None):
        self.replacement = replacement
        self.wrong_first = wrong_first
        self.current_expression = "original"
        self.calls = 0
        self.proposals = 0
        self.seen_messages = []

    def public(self):
        return {"provider": "scripted-test", "model": "deterministic-fixture"}

    def invoke(self, messages, tools):
        assert "edit_files" in {tool["function"]["name"] for tool in tools}
        assert "propose_patch" not in {tool["function"]["name"] for tool in tools}
        self.calls += 1
        self.seen_messages.append(json.loads(json.dumps(messages)))
        if self.calls == 1:
            name, args = "find_symbol", {"name": "refund_amount"}
        elif self.calls == 2:
            name, args = "read_file", {"path": "shop/refund.py"}
        else:
            last = messages[-1]
            payload = json.loads(last["content"])
            if last["role"] == "tool" and "candidate_tree_sha256" in payload:
                self.current_expression = self.pending_expression
                name, args = "submit_candidate", {"candidate_tree_sha256": payload["candidate_tree_sha256"]}
            elif "file_sha256" in payload:
                self.proposals += 1
                self.pending_expression = self.wrong_first if self.proposals == 1 and self.wrong_first else self.replacement
                name, args = "edit_files", {"expected_tree_hash": payload["tree_hash"], "edits": [{
                    "path": "shop/refund.py", "expected_file_sha256": payload["file_sha256"],
                    "old_text": f"    return {self.current_expression}\n",
                    "new_text": f"    return {self.pending_expression}\n"}]}
            else:
                name, args = "read_file", {"path": "shop/refund.py"}
        return {"content": "test fixture action", "tool_calls": [{"id": f"call-{self.calls}", "name": name, "args": args}],
                "usage": None, "finish_reason": "tool_calls"}


@unittest.skipUnless(HAS_AGENT, "Run framework tests with the project agent .venv")
class GraphTests(AgentFixture, unittest.TestCase):
    def run_graph(self, model=None, backend=None, budget=None):
        self.model = model or ScriptedModel()
        return AgentRunner(self.root / "agent-runs", self.model, backend=backend or FakeBackend(), budget=budget).run(self.task)

    def test_graph_success_is_scripted_and_terminal_verifier_decides(self):
        result = self.run_graph()
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(result["model_mode"], "scripted_test")
        self.assertEqual(result["usage"]["candidate_count"], 1)
        self.assertIsNone(result["usage"]["cost_usd"])
        self.assertTrue((Path(result["artifact_directory"]) / "manifest.json").is_file())
        for messages in self.model.seen_messages:
            self.assertNotIn("holdout", json.dumps(messages).lower())

    def test_model_cannot_declare_success_without_a_candidate(self):
        model = ScriptedModel()
        model.invoke = lambda messages, tools: {"content": "PASSED, all tests passed", "tool_calls": []}
        result = self.run_graph(model=model, budget=AgentBudget(max_model_calls=2))
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED", result)
        self.assertEqual(result["usage"]["candidate_count"], 0)
        self.assertEqual(result["usage"]["model_calls"], 2)

    def test_failed_preflight_spends_no_model_calls(self):
        from test_verification import report
        result = self.run_graph(backend=FakeBackend({"baseline_regression": report("failed")}))
        self.assertEqual(result["status"], "CONFIG_ERROR", result)
        self.assertEqual(result["usage"]["model_calls"], 0)

    def test_final_holdout_failure_never_returns_to_model(self):
        from test_verification import report
        result = self.run_graph(backend=FakeBackend({"candidate_holdout": report("failed")}))
        self.assertEqual(result["status"], "REPAIR_FAILED", result)
        self.assertEqual(result["reason_code"], "holdout_failed")
        self.assertEqual(result["usage"]["candidate_count"], 1)
        self.assertEqual(self.model.calls, 4)
        for messages in self.model.seen_messages:
            self.assertNotIn("holdout", json.dumps(messages).lower())

    def test_tool_budget_is_enforced(self):
        result = self.run_graph(budget=AgentBudget(max_tool_calls=1))
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED", result)
        self.assertEqual(result["reason_code"], "tool_calls_limit")


@unittest.skipUnless(HAS_AGENT, "Run adapter tests with agent dependencies")
class AdapterHTTPTests(unittest.TestCase):
    def test_real_langchain_transport_uses_deepseek_body_and_preserves_tool_ids(self):
        from patchtrail.agents.model import LangChainModel
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                first = len(requests) == 1
                message = {"role": "assistant", "content": "" if first else "local transport checked"}
                if first:
                    message["tool_calls"] = [{"id": "transport-1", "type": "function",
                                              "function": {"name": "list_files", "arguments": "{}"}}]
                data = json.dumps({"id": "local-http-test", "object": "chat.completion", "created": 1,
                                   "model": "deepseek-flash", "choices": [{"index": 0, "message": message,
                                    "finish_reason": "tool_calls" if first else "stop"}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            model = LangChainModel(ModelSettings(api_key="local-test-only", base_url=f"http://127.0.0.1:{server.server_port}/v1"))
            messages = [{"role": "user", "content": "List files"}]
            response = model.invoke(messages, TOOLS)
            self.assertEqual(response["tool_calls"][0]["id"], "transport-1")
            self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
            self.assertEqual(requests[0]["max_tokens"], 4096)
            schemas = {t["function"]["name"]: t["function"]["parameters"] for t in requests[0]["tools"]}
            self.assertNotIn("propose_patch", schemas)
            self.assertFalse(schemas["edit_files"]["properties"]["edits"]["items"]["additionalProperties"])
            self.assertEqual(response["usage"]["total_tokens"], 15)
            messages += [{"role": "assistant", "content": "", "tool_calls": response["tool_calls"]},
                         {"role": "tool", "tool_call_id": "transport-1", "content": "{}"}]
            self.assertEqual(model.invoke(messages, TOOLS)["content"], "local transport checked")
            self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "transport-1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


@unittest.skipUnless(HAS_AGENT and os.environ.get("PATCHTRAIL_DOCKER_TESTS") == "1",
                     "Scripted graph + real Docker requires agent dependencies and explicit Docker tests")
class RealDockerGraphTests(AgentFixture, unittest.TestCase):
    def test_wrong_then_correct_candidate_retries_without_holdout_feedback(self):
        model = ScriptedModel(wrong_first="original + discount")
        output = ROOT / ".test-artifacts" / "agent"
        result = AgentRunner(output / "runs", model).run(self.task)
        output.mkdir(parents=True, exist_ok=True)
        (output / "docker-graph-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        self.assertEqual(result["status"], "PASSED", result)
        self.assertEqual(result["model_mode"], "scripted_test")
        self.assertEqual(result["usage"]["candidate_count"], 2)
        self.assertEqual(result["candidates"][0]["status"], "REPAIR_FAILED")
        self.assertEqual(result["candidates"][1]["status"], "FEEDBACK_PASSED")
        for messages in model.seen_messages:
            self.assertNotIn("holdout", json.dumps(messages).lower())
        for candidate in result["candidates"]:
            verification = json.loads((Path(candidate["verification_ref"]) / "result.json").read_text())
            self.assertNotIn("candidate_holdout", verification["stages"])
        processes = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=io.patchtrail.run={result['run_id']}"], capture_output=True, check=True)
        self.assertFalse(processes.stdout.strip())


if __name__ == "__main__":
    unittest.main()
