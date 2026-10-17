"""Create a new, self-contained Git bug fixture. Refuses to overwrite anything."""

import argparse
import json
import subprocess
from pathlib import Path


def create_demo(destination, image="patchtrail-pytest:0.1"):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    repository = destination / "repository"
    repository.mkdir()
    (repository / "shop").mkdir()
    (repository / "shop" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (repository / "shop" / "refund.py").write_text(
        "def refund_amount(original, discount=0):\n    return original\n", encoding="utf-8", newline="\n")

    def git(*args):
        return subprocess.run(["git", "-c", "core.hooksPath=", "-C", str(repository), *args],
                              check=True, capture_output=True).stdout.decode().strip()

    git("init", "--quiet", "--template=")
    git("add", ".")
    git("-c", "user.name=PatchTrail Demo", "-c", "user.email=demo@localhost",
        "commit", "--quiet", "-m", "Reproducible refund bug")
    commit = git("rev-parse", "HEAD")
    suites = {}
    for name, code in {
        "regression": "from shop.refund import refund_amount\n\ndef test_no_discount():\n    assert refund_amount(100) == 100\n",
        "feedback": "from shop.refund import refund_amount\n\ndef test_discount():\n    assert refund_amount(100, 20) == 80\n",
        "holdout": "from shop.refund import refund_amount\n\ndef test_another_discount():\n    assert refund_amount(90, 15) == 75\n",
    }.items():
        suite = destination / "checks" / name
        suite.mkdir(parents=True)
        (suite / f"test_{name}.py").write_text(code, encoding="utf-8", newline="\n")
        suites[name] = str(suite)
    task = {"schema_version": 1, "task_id": "refund-discount-demo",
            "request": "Refund the original amount minus the discount; preserve orders without discounts.",
            "repository_path": str(repository), "base_commit": commit, "image": image,
            "editable_paths": ["shop"], "source_roots": ["."],
            "import_modules": ["shop.refund"],
            "regression_dir": suites["regression"], "feedback_dir": suites["feedback"],
            "holdout_dir": suites["holdout"], "timeout_seconds": 30}
    (destination / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")
    for name, expression in (("correct", "original - discount"),
                             ("wrong", "original + discount"),
                             ("regression", "original - discount - 1")):
        patch = ("diff --git a/shop/refund.py b/shop/refund.py\n"
                 "--- a/shop/refund.py\n+++ b/shop/refund.py\n"
                 "@@ -1,2 +1,2 @@\n def refund_amount(original, discount=0):\n"
                 f"-    return original\n+    return {expression}\n")
        (destination / f"{name}.patch").write_text(patch, encoding="utf-8", newline="\n")
    return task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".patchtrail/demo"))
    parser.add_argument("--image", default="patchtrail-pytest:0.1")
    args = parser.parse_args()
    try:
        task = create_demo(args.output, args.image)
    except FileExistsError:
        parser.exit(1, "Output directory already exists; choose a new --output path.\n")
    print(json.dumps({"task": str(args.output.resolve() / "task.json"),
                      "base_commit": task["base_commit"],
                      "patches": ["correct.patch", "wrong.patch", "regression.patch"]}, indent=2))


if __name__ == "__main__":
    main()
