"""Trusted container entry point. Executed only inside the verification image."""

import importlib
import importlib.util
import base64
import json
import os
import sys
from pathlib import Path


class Collector:
    def __init__(self):
        self.collected = []
        self.reports = {}
        self.collection_errors = []

    def pytest_collection_modifyitems(self, items):
        self.collected = [item.nodeid for item in items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(str(report.longrepr)[-2000:])

    def pytest_runtest_logreport(self, report):
        self.reports.setdefault(report.nodeid, []).append({
            "when": report.when, "outcome": report.outcome,
            "wasxfail": bool(getattr(report, "wasxfail", False)),
            "detail": str(report.longrepr)[-1500:] if report.failed else "",
        })


def execute(config):
    try:
        # Import the trusted test framework before adding candidate paths.
        import pytest
    except ImportError:
        return {"error": "pytest_missing", "message": "pytest is not installed in this image."}
    roots = [str((Path("/workspace") / root).resolve()) for root in config["source_roots"]]
    for root in roots:
        if not Path(root).is_relative_to("/workspace") or not Path(root).is_dir():
            return {"error": "source_root_missing", "message": root}
    sys.path[:0] = roots
    origins = {}
    for module in config["import_modules"]:
        try:
            spec = importlib.util.find_spec(module)
            if spec is None or not spec.origin or not Path(spec.origin).resolve().is_relative_to("/workspace"):
                return {"error": "wrong_import_origin", "message": module, "origins": origins}
            origins[module] = spec.origin
            importlib.import_module(module)
        except Exception as exc:
            return {"error": "source_import_error", "message": f"{type(exc).__name__}: {exc}"[:1500]}
    collector = Collector()
    exit_code = int(pytest.main([
        "-q", "-c", "/control/pytest.ini", "--rootdir=/checks", "--confcutdir=/checks",
        "--import-mode=importlib", "--assert=plain", "-p", "no:cacheprovider", "/checks",
    ], plugins=[collector]))
    outcomes = {}
    for node in collector.collected:
        reports = collector.reports.get(node, [])
        if any(item["outcome"] == "failed" and item["when"] != "call" for item in reports):
            status = "error"
        elif any(item["outcome"] == "failed" for item in reports):
            status = "failed"
        elif any(item["outcome"] == "skipped" or item["wasxfail"] for item in reports):
            status = "skipped"
        elif {item["when"] for item in reports if item["outcome"] == "passed"} == {"setup", "call", "teardown"}:
            status = "passed"
        else:
            status = "incomplete"
        outcomes[node] = {"status": status, "reports": reports}
    return {"pytest_version": pytest.__version__, "python_version": sys.version,
            "exit_code": exit_code, "collected": collector.collected, "outcomes": outcomes,
            "collection_errors": collector.collection_errors, "import_origins": origins}


def main():
    config = json.loads(Path("/control/config.json").read_text())
    try:
        result = execute(config)
    except BaseException as exc:
        result = {"error": "runner_error", "message": f"{type(exc).__name__}: {exc}"[:1500]}
    result.update(schema_version=1, nonce=config["nonce"], phase=config["phase"])
    data = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(data) > 524288:
        data = json.dumps({"schema_version": 1, "nonce": config["nonce"], "phase": config["phase"],
                           "error": "report_limit", "message": "Test report exceeded 512 KiB."}).encode()
    # tmpfs vanishes when a container stops. Send a bounded framed report over
    # the attached output channel, without a writable host mount.
    frame = b"\nPATCHTRAIL_RESULT=" + config["nonce"].encode() + b":" + base64.b64encode(data) + b"\n"
    sys.__stdout__.flush()
    sys.__stdout__.buffer.write(frame)
    sys.__stdout__.buffer.flush()


if __name__ == "__main__":
    main()
