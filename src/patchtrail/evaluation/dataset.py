"""Materialize trusted benchmark data without executing repository Python on the host."""

import ast
import difflib
import json
import os
import re
import subprocess
from pathlib import Path

from patchtrail.domain.task import TaskSpec, VerificationError, safe_relative
from patchtrail.verification.service import write_json
from patchtrail.workspace.snapshot import digest


DEFAULT_DATASET = Path(__file__).resolve().parents[3] / "benchmarks/v1/cases.json"


def load_dataset(path=DEFAULT_DATASET):
    raw = Path(path).read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if data.get("schema_version") != 1 or data.get("dataset_id") != "patchtrail-python-v1":
        raise ValueError("Unsupported evaluation dataset.")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Dataset must contain cases.")
    seen = set()
    for case in cases:
        identity = case["id"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identity) or identity in seen:
            raise ValueError("Invalid or duplicate case ID.")
        seen.add(identity)
        if case["expectation"] not in ("repair", "preflight_reject"):
            raise ValueError("Unknown expectation.")
        if not case["source"] or not set(case["corrected"]) <= set(case["source"]):
            raise ValueError("Corrected files must be existing source files.")
        if case["expectation"] == "repair" and not case["corrected"]:
            raise ValueError("Repair case needs a reference patch.")
        for mapping in (case["source"], case["corrected"], case.get("overfit", {})):
            if not set(mapping) <= set(case["source"]):
                raise ValueError("Variant must only change existing source files.")
            for name, code in mapping.items():
                safe_relative(name)
                if not name.endswith(".py") or not code.endswith("\n"):
                    raise ValueError("Fixtures must be LF-terminated Python files.")
                ast.parse(code, filename=name)
        for suite in ("regression", "feedback", "holdout"):
            ast.parse(case[suite], filename=suite)
    return data, digest(raw)


def select_cases(dataset, requested=()):
    requested = list(requested)
    known = {case["id"] for case in dataset["cases"]}
    if len(set(requested)) != len(requested) or set(requested) - known:
        raise ValueError("Case IDs must be known and unique.")
    return [case for case in dataset["cases"] if not requested or case["id"] in requested]


def make_patch(case, variant="corrected"):
    chunks = []
    for name, after in sorted(case[variant].items()):
        before = case["source"][name]
        if before != after:
            chunks.append(f"diff --git a/{name} b/{name}\n")
            chunks.extend(difflib.unified_diff(before.splitlines(keepends=True),
                                              after.splitlines(keepends=True),
                                              fromfile=f"a/{name}", tofile=f"b/{name}"))
    return "".join(chunks)


def materialize(case, destination, image):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    repository = destination / "repository"
    repository.mkdir()
    for name, code in case["source"].items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
               GIT_AUTHOR_NAME="PatchTrail Benchmark", GIT_COMMITTER_NAME="PatchTrail Benchmark",
               GIT_AUTHOR_EMAIL="benchmark@localhost", GIT_COMMITTER_EMAIL="benchmark@localhost",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args):
        return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.hooksPath=",
                               "-c", "commit.gpgsign=false", "-C", str(repository), *args],
                              env=env, capture_output=True, check=True, timeout=20).stdout.decode("utf-8").strip()

    git("init", "--quiet", "--template=", "--object-format=sha1")
    git("add", "--", ".")
    git("commit", "--quiet", "-m", "Fixed baseline: " + case["id"])
    task = {"task_id": case["id"], "request": case["request"],
            "repository_path": str(repository), "base_commit": git("rev-parse", "HEAD"),
            "image": image, "editable_paths": sorted(case["source"]),
            "import_modules": case["import_modules"], "timeout_seconds": 30}
    for suite in ("regression", "feedback", "holdout"):
        directory = destination / "checks" / suite
        directory.mkdir(parents=True)
        (directory / f"test_{suite}.py").write_text(case[suite], encoding="utf-8", newline="\n")
        task[suite + "_dir"] = str(directory)
    task = TaskSpec.from_dict(task)
    write_json(destination / "task.json", task.to_dict())
    for variant in ("corrected", "overfit"):
        if case.get(variant):
            (destination / f"{variant}.patch").write_text(make_patch(case, variant), encoding="utf-8", newline="\n")
    files = {}
    for name in case["source"]:
        files["repository/" + name] = digest((repository / name).read_bytes())
    for path in (destination / "checks").rglob("*.py"):
        files[path.relative_to(destination).as_posix()] = digest(path.read_bytes())
    for path in [destination / "task.json", *destination.glob("*.patch")]:
        files[path.name] = digest(path.read_bytes())
    write_json(destination / "fixture-manifest.json", {"files": files, "base_commit": task.base_commit})
    return task


def assert_fixture_unchanged(directory):
    directory = Path(directory)
    record = json.loads((directory / "fixture-manifest.json").read_text(encoding="utf-8"))
    for name, expected in record["files"].items():
        if digest((directory / name).read_bytes()) != expected:
            raise VerificationError("SYSTEM_ERROR", "fixture_changed", "Evaluation fixture changed.")
    task = TaskSpec.load(directory / "task.json")
    # Test directory additions must not silently change the benchmark either.
    expected = {k for k in record["files"] if k.startswith("checks/")}
    actual = {p.relative_to(directory).as_posix() for p in (directory / "checks").rglob("*") if p.is_file()}
    source_actual = {"repository/" + p.relative_to(directory / "repository").as_posix()
                     for p in (directory / "repository").rglob("*")
                     if p.is_file() and ".git" not in p.relative_to(directory / "repository").parts}
    source_expected = {k for k in record["files"] if k.startswith("repository/")}
    if actual != expected or source_actual != source_expected or task.base_commit != record["base_commit"]:
        raise VerificationError("SYSTEM_ERROR", "fixture_changed", "Evaluation suite inventory changed.")
    return task
