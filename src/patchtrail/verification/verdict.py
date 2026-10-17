"""Validate structured test evidence, including empty/skip/collection failures."""

from patchtrail.domain.task import VerificationError


def summarize(report):
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report.get("schema_version") != 1):
        raise VerificationError("ENVIRONMENT_ERROR", "invalid_report", "Invalid report schema.")
    if report.get("error"):
        raise VerificationError("ENVIRONMENT_ERROR", str(report["error"]), str(report.get("message", "")))
    nodes, outcomes = report.get("collected"), report.get("outcomes")
    code = report.get("exit_code")
    collection_errors = report.get("collection_errors")
    if (not isinstance(nodes, list) or any(not isinstance(n, str) for n in nodes)
            or len(nodes) != len(set(nodes)) or not isinstance(outcomes, dict)
            or set(nodes) != set(outcomes) or type(code) is not int
            or not isinstance(collection_errors, list)):
        raise VerificationError("ENVIRONMENT_ERROR", "invalid_report", "Test identities or exit code are inconsistent.")
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "incomplete": 0}
    for item in outcomes.values():
        if not isinstance(item, dict) or item.get("status") not in counts:
            raise VerificationError("ENVIRONMENT_ERROR", "invalid_report", "Unknown test outcome.")
        reports = item.get("reports")
        if (not isinstance(reports, list) or not reports
                or any(not isinstance(entry, dict) or entry.get("when") not in ("setup", "call", "teardown")
                       or entry.get("outcome") not in ("passed", "failed", "skipped")
                       or type(entry.get("wasxfail")) is not bool for entry in reports)):
            raise VerificationError("ENVIRONMENT_ERROR", "invalid_report", "Missing or invalid test phases.")
        phases = [entry["when"] for entry in reports]
        if len(phases) != len(set(phases)):
            raise VerificationError("ENVIRONMENT_ERROR", "invalid_report", "Duplicate test phases.")
        if any(entry["outcome"] == "failed" and entry["when"] != "call" for entry in reports):
            expected = "error"
        elif any(entry["outcome"] == "failed" for entry in reports):
            expected = "failed"
        elif any(entry["outcome"] == "skipped" or entry["wasxfail"] for entry in reports):
            expected = "skipped"
        elif set(phases) == {"setup", "call", "teardown"}:
            expected = "passed"
        else:
            expected = "incomplete"
        if expected != item["status"]:
            raise VerificationError("ENVIRONMENT_ERROR", "invalid_report", "Test phase evidence contradicts outcome.")
        counts[item["status"]] += 1
    # Only the normal all-pass and assertion-failure codes are usable evidence.
    valid = (bool(nodes) and code in (0, 1) and not collection_errors
             and not counts["skipped"] and not counts["error"] and not counts["incomplete"])
    if code == 0 and counts["failed"]:
        valid = False
    if code == 1 and not counts["failed"]:
        valid = False
    return {"valid": valid, "passed": valid and code == 0 and counts["passed"] == len(nodes),
            "collected": sorted(nodes), "counts": counts, "exit_code": code,
            "collection_errors": collection_errors}


def compare_collection(baseline, candidate):
    if baseline["collected"] != candidate["collected"]:
        raise VerificationError("REPAIR_FAILED", "collection_changed", "Required test identities changed.")
