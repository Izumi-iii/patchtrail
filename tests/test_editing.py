"""Structured edits are atomic; generated patches remain independently replayable."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patchtrail.domain.task import VerificationError
from patchtrail.evaluation.dataset import load_dataset, materialize
from patchtrail.tools.code import CodeTools, TOOLS
from patchtrail.verification.service import Verifier
from patchtrail.workspace.snapshot import apply_candidate, digest, export_commit, manifest


class StructuredEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = next(c for c in load_dataset()[0]["cases"] if c["id"] == "checkout-discount")
        self.task = materialize(self.case, self.root / "fixture", "patchtrail-pytest:0.1")
        self.base = self.root / "base"
        export_commit(self.task, self.base)
        self.tools = CodeTools(self.base, self.root / "proposals", self.task)

    def edit(self, name="pricing.py", old="return price", new="return price - discount"):
        return {"path": name, "expected_file_sha256": self.tools.files[name]["sha256"],
                "old_text": old, "new_text": new}

    def invoke(self, edits, **kwargs):
        return self.tools.invoke("edit_files", {"expected_tree_hash": self.tools.tree, "edits": edits, **kwargs})

    def state(self):
        return (self.tools.current, self.tools.tree, self.tools.patch, self.tools.files.copy(),
                manifest(self.tools.current), manifest(self.base))

    def test_multifile_batch_generates_replayable_cumulative_diff_and_audit(self):
        before = manifest(self.base)
        result = self.invoke([self.edit(), self.edit("checkout.py", "net_price(price)", "net_price(price, discount)")])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["changed_paths"], ["checkout.py", "pricing.py"])
        replay = self.root / "replay"
        files, _ = apply_candidate(self.base, replay, self.tools.patch, self.task)
        self.assertEqual(files, self.tools.files)
        for name, code in self.case["corrected"].items():
            self.assertEqual((replay / name).read_text(), code)
        self.assertEqual(before, manifest(self.base))
        self.assertEqual(json.loads((self.tools.patch.parent / "edit-request.json").read_text())["edits"][0]["path"], "pricing.py")

    def test_late_bad_match_and_duplicate_path_leave_previous_candidate_unchanged(self):
        self.assertTrue(self.invoke([self.edit()])["ok"])
        before = self.state()
        first = self.edit("checkout.py", "net_price(price)", "net_price(price, discount)")
        bad = self.edit(old="does not exist", new="new")
        result = self.invoke([first, bad])
        self.assertEqual(result["error_code"], "edit_not_found")
        self.assertEqual(before, self.state())
        result = self.invoke([first, first])
        self.assertEqual(result["error_code"], "duplicate_edit_path")
        self.assertEqual(before, self.state())

    def test_stale_tree_and_file_hash_rejected(self):
        edit = self.edit()
        old_tree = self.tools.tree
        self.assertTrue(self.invoke([edit])["ok"])
        before = self.state()
        self.assertEqual(self.invoke([edit], expected_tree_hash=old_tree)["error_code"], "stale_tree_hash")
        self.assertEqual(self.invoke([edit])["error_code"], "stale_file_hash")
        self.assertEqual(before, self.state())

    def test_duplicate_and_overlapping_matches_are_ambiguous(self):
        for text, old in (("def f():\n    return 'x x'\n", "x"), ("def f():\n    return 'aaa'\n", "aa")):
            with self.subTest(old=old):
                base = self.root / ("base-" + old)
                base.mkdir()
                (base / "pricing.py").write_text(text, encoding="utf-8", newline="\n")
                self.tools = CodeTools(base, self.root / ("out-" + old), self.task)
                before = self.state_without_base()
                result = self.invoke([self.edit(old=old, new="y")])
                self.assertEqual(result["error_code"], "ambiguous_edit")
                self.assertEqual(before, self.state_without_base())

    def state_without_base(self):
        return (self.tools.current, self.tools.tree, self.tools.patch, manifest(self.tools.current))

    def test_protected_and_unsafe_paths_are_terminal_and_atomic(self):
        for name in ("../escape.py", "tests/test_x.py", "conftest.py", ".env", "outside.py"):
            with self.subTest(name=name):
                before = self.state()
                bad = {**self.edit(), "path": name}
                with self.assertRaises(VerificationError) as caught:
                    self.invoke([self.edit(), bad])
                self.assertEqual(caught.exception.status, "POLICY_REJECTED")
                self.assertEqual(before, self.state())

    def test_invalid_nested_arguments_and_empty_noop_edits_do_not_publish(self):
        invalid = [None, {}, [], [None], [self.edit(), None], [{**self.edit(), "extra": 1}],
                   [{**self.edit(), "new_text": 3}], [{**self.edit(), "old_text": ""}],
                   [self.edit(old="return price", new="return price")],
                   [self.edit(new="return price\r\n")], [self.edit(new="return '\x00'")],
                   [self.edit(new="\ud800")]]
        before = self.state()
        for edits in invalid:
            with self.subTest(edits=edits):
                self.assertFalse(self.invoke(edits)["ok"])
                self.assertEqual(before, self.state())

    def test_size_and_change_limits_remain_in_force(self):
        before = self.state()
        result = self.invoke([self.edit(new="x" * 270000)])
        self.assertEqual(result["error_code"], "edit_size")
        self.assertEqual(before, self.state())
        self.tools.task = replace(self.task, max_changed_files=1)
        with self.assertRaises(VerificationError) as caught:
            self.invoke([self.edit(), self.edit("checkout.py", "net_price(price)", "net_price(price, discount)")])
        self.assertEqual(caught.exception.reason, "change_limit")
        self.assertEqual(before, self.state())

    def test_retry_edits_current_text_but_patch_remains_relative_to_original(self):
        first = self.invoke([self.edit(new="return price + discount")])
        self.assertTrue(first["ok"], first)
        second = self.invoke([self.edit(old="return price + discount", new="return price - discount"),
                              self.edit("checkout.py", "net_price(price)", "net_price(price, discount)")])
        self.assertTrue(second["ok"], second)
        text = self.tools.patch.read_text()
        self.assertIn("-    return price\n", text)
        self.assertNotIn("-    return price + discount", text)
        self.assertEqual(second["changed_paths"], ["checkout.py", "pricing.py"])

    def test_frozen_candidate_and_legacy_tool_are_not_model_edit_paths(self):
        names = {tool["function"]["name"] for tool in TOOLS}
        self.assertIn("edit_files", names)
        self.assertNotIn("propose_patch", names)
        self.assertFalse(self.tools.invoke("propose_patch", {"unified_diff": "x", "expected_tree_hash": self.tools.tree})["ok"])
        result = self.invoke([self.edit()])
        submitted = self.tools.invoke("submit_candidate", {"candidate_tree_sha256": result["candidate_tree_sha256"]})
        self.assertTrue(submitted["submitted"])
        before = self.state()
        self.assertEqual(self.invoke([self.edit(old="return price - discount", new="return price")])["error_code"], "candidate_frozen")
        self.assertEqual(before, self.state())

    def test_replay_failure_keeps_previous_candidate(self):
        self.assertTrue(self.invoke([self.edit()])["ok"])
        before = self.state()
        failure = VerificationError("REPAIR_FAILED", "patch_does_not_apply", "test replay failure")
        with patch("patchtrail.tools.code.apply_candidate", side_effect=failure):
            result = self.invoke([self.edit("checkout.py", "net_price(price)", "net_price(price, discount)")])
        self.assertEqual(result["error_code"], "patch_does_not_apply")
        self.assertEqual(before, self.state())

    def test_external_snapshot_mutation_is_a_system_error(self):
        (self.base / "pricing.py").write_text("changed\n")
        with self.assertRaises(VerificationError) as caught:
            self.invoke([self.edit()])
        self.assertEqual(caught.exception.status, "SYSTEM_ERROR")

    def test_lf_no_final_newline_empty_result_and_unicode_roundtrip(self):
        variations = [("def f():\n    return 1", "return 1", "return 2"),
                      ("def f():\n    return 1\n", "def f():\n    return 1\n", ""),
                      ("def f():\n    return '你\u2028好'\n", "你\u2028好", "世\u2028界")]
        for i, (text, old, new) in enumerate(variations):
            with self.subTest(i=i):
                base = self.root / f"eof-{i}"
                base.mkdir()
                (base / "pricing.py").write_text(text, encoding="utf-8", newline="\n")
                self.tools = CodeTools(base, self.root / f"proposals-eof-{i}", self.task)
                result = self.invoke([self.edit(old=old, new=new)])
                self.assertTrue(result["ok"], result)
                self.assertEqual((self.tools.current / "pricing.py").read_bytes(), text.replace(old, new).encode("utf-8"))
                self.assertEqual((base / "pricing.py").read_bytes(), text.encode("utf-8"))


class DatasetGeneratedPatchTests(unittest.TestCase):
    def test_all_repair_references_can_be_expressed_as_structured_edits(self):
        cases = [c for c in load_dataset()[0]["cases"] if c["expectation"] == "repair"]
        with tempfile.TemporaryDirectory() as temp:
            for case in cases:
                with self.subTest(case=case["id"]):
                    root = Path(temp) / case["id"]
                    task = materialize(case, root / "fixture", "patchtrail-pytest:0.1")
                    base = root / "base"
                    export_commit(task, base)
                    tools = CodeTools(base, root / "proposals", task)
                    edits = [{"path": name, "expected_file_sha256": tools.files[name]["sha256"],
                              "old_text": case["source"][name], "new_text": code}
                             for name, code in case["corrected"].items()]
                    result = tools.invoke("edit_files", {"expected_tree_hash": tools.tree, "edits": edits})
                    self.assertTrue(result["ok"], result)
                    for name, code in case["corrected"].items():
                        self.assertEqual((tools.current / name).read_bytes(), code.encode())

    @unittest.skipUnless(os.environ.get("PATCHTRAIL_DOCKER_TESTS") == "1", "Requires explicit Docker tests")
    def test_generated_multifile_patch_passes_real_independent_verification(self):
        case = next(c for c in load_dataset()[0]["cases"] if c["id"] == "checkout-discount")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = materialize(case, root / "fixture", os.environ.get("PATCHTRAIL_TEST_IMAGE", "patchtrail-pytest:0.1"))
            base = root / "base"
            export_commit(task, base)
            tools = CodeTools(base, root / "proposals", task)
            edits = [{"path": name, "expected_file_sha256": tools.files[name]["sha256"],
                      "old_text": case["source"][name], "new_text": code} for name, code in case["corrected"].items()]
            proposed = tools.invoke("edit_files", {"expected_tree_hash": tools.tree, "edits": edits})
            self.assertTrue(proposed["ok"], proposed)
            result = Verifier(ROOT / ".test-artifacts/structured-editing/runs").verify(task, tools.patch)
            self.assertEqual(result["status"], "PASSED", result)
            self.assertEqual(result["patch_sha256"], proposed["patch_sha256"])
            self.assertTrue(result["stages"]["candidate_holdout"]["passed"])
            self.assertEqual(manifest(base), manifest(Path(task.repository_path)))


if __name__ == "__main__":
    unittest.main()
