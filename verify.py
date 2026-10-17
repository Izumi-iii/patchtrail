"""Independent patch verification entry point (does not start the web server)."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from patchtrail.domain.task import TaskSpec, VerificationError
from patchtrail.verification.service import Verifier


def main():
    parser = argparse.ArgumentParser(description="Verify a patch in a clean Linux container")
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--runs-dir", type=Path, default=Path(".patchtrail/runs"))
    arguments = parser.parse_args()
    try:
        task = TaskSpec.load(arguments.task)
        result = Verifier(arguments.runs_dir).verify(task, arguments.patch)
    except VerificationError as exc:
        result = {"status": exc.status, "reason_code": exc.reason, "message": str(exc)}
    except OSError as exc:
        result = {"status": "SYSTEM_ERROR", "reason_code": "io_error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASSED" else 1)


if __name__ == "__main__":
    main()
