"""Review, approve and apply a verified PatchTrail candidate without model calls."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from patchtrail.approvals.service import ApprovalService
from patchtrail.domain.task import VerificationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=ROOT / ".patchtrail/approvals")
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request", help="Freeze a PASSED Agent run for review")
    request.add_argument("--run", type=Path, required=True)
    request.add_argument("--ttl-hours", type=int, default=24)
    commands.add_parser("list")
    for name in ("show", "approve", "reject", "apply", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--id", required=True)
        if name == "show":
            command.add_argument("--diff", action="store_true")
        else:
            command.add_argument("--binding", required=True, help="SHA from the reviewed approval")
    args = parser.parse_args()
    try:
        service = ApprovalService(args.store)
        if args.command == "request":
            result = service.request(args.run, ttl_hours=args.ttl_hours)
        elif args.command == "list":
            result = service.list()
        elif args.command == "show":
            result = service.get(args.id, include_diff=args.diff)
        elif args.command in ("approve", "reject"):
            result = service.decide(args.id, args.binding, args.command)
        else:
            result = service.apply(args.id, args.binding, recover=args.command == "recover")
    except VerificationError as exc:
        result = {"status": exc.status, "reason_code": exc.reason, "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(json.dumps({"status": "APPROVAL_ERROR", "reason_code": "io_or_format_error",
                          "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    diff = result.pop("diff", None) if isinstance(result, dict) else None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if diff is not None:
        print("\n--- Frozen patch for review ---\n")
        print(diff, end="" if diff.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
