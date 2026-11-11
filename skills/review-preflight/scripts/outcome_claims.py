"""Typed PR status rendering from collector receipts, without model-authored prose."""
import datetime
import math


def summarize(checks, decision, now):
    result = {"schema": 1, "merge_outcome": "unknown", "checks_status": "unknown",
              "overall_readiness": "unknown", "review_status": "unknown",
              "owner_authority": "unknown", "errors": []}
    if not isinstance(checks, dict) or not isinstance(decision, dict):
        result["errors"].append("Both collector receipts are required")
        return result
    try:
        times = [datetime.datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00")) for r in (checks, decision)]
        if any(t.tzinfo is None for t in times) or isinstance(now, bool) or not math.isfinite(now):
            raise ValueError()
        ages = [now - t.timestamp() for t in times]
        if any(age < 0 or age > 300 for age in ages):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        result["errors"].append("Observation timestamps are stale, future, or malformed")
        return result
    identity = (checks.get("repository"), checks.get("pr"), checks.get("head_sha"))
    other = (decision.get("repository"), decision.get("pr"), decision.get("head_sha"))
    if identity != other or not all(identity) or any(isinstance(r.get("pr"), bool) for r in (checks, decision)):
        result["errors"].append("Collector identities disagree or are incomplete")
        return result
    result.update(repository=identity[0], pr=identity[1], head_sha=identity[2])
    if any(r.get("evidence_status") != "stable" or r.get("errors") for r in (checks, decision)):
        result["errors"].append("Evidence is incomplete or ambiguous")
        return result
    if checks.get("state") != decision.get("state") or checks.get("merged") is not decision.get("merged"):
        result["errors"].append("Outcome changed between collector observations")
        return result
    if checks.get("merged") is True:
        if not checks.get("merged_at") or checks["merged_at"] != decision.get("merged_at"):
            result["errors"].append("Merge timestamps disagree or are missing")
            return result
        result["merge_outcome"] = "merged"
    elif checks.get("merged") is False and checks.get("state") in ("open", "closed"):
        result["merge_outcome"] = "not_merged"
    else:
        result["errors"].append("Merge outcome is malformed")
        return result
    buckets = checks.get("required_checks")
    if isinstance(buckets, list) and buckets and all(isinstance(c, dict) for c in buckets):
        names = [c.get("name") for c in buckets]
        if all(isinstance(n, str) and n for n in names) and len(set(names)) == len(names):
            if all(c.get("bucket") == "pass" and c.get("link") for c in buckets):
                result["checks_status"] = "passed"
            elif all(c.get("bucket") in ("pass", "fail", "pending", "skipping", "cancel") for c in buckets):
                result["checks_status"] = "blocked"
    if decision.get("review_decision") in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        result["review_status"] = "blocked"
    elif decision.get("review_decision") == "APPROVED":
        result["review_status"] = "github_approved"
    if result["checks_status"] == "blocked" or result["review_status"] == "blocked":
        result["overall_readiness"] = "blocked"
    # These collectors do not attest human policy or the actor's authority.
    result["note"] = "GitHub approval and passing checks do not establish all human/authority gates."
    return result


def render(result):
    outcome = {"merged": "Merged", "not_merged": "Not merged"}.get(result.get("merge_outcome"), "Merge outcome unknown")
    checks = {"passed": "passed", "blocked": "not all passed"}.get(result.get("checks_status"), "unknown")
    readiness = "blocked" if result.get("overall_readiness") == "blocked" else "unknown"
    return f"{outcome}. Required checks: {checks}. Overall readiness: {readiness}; human policy and actor authority are not attested."
