#!/usr/bin/env python3
"""Black-box benchmark runner for a live Sutando workspace.

The controller owns the suite and scoring.  The Sutando instance under test
only sees ordinary task files and writes ordinary result files, so the run
exercises the same queue/watcher/core path as a user task.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SCHEMA = 1
SCRIPT_DIR = Path(__file__).parent
DEFAULT_SUITE = SCRIPT_DIR.parent / "benchmarks" / "smoke.json"
DEFAULT_SUTANDO_CONFIG = SCRIPT_DIR / "sutando-config.sh"
CODE_IDENTITY_FIELDS = (
    "revision", "commit", "branch", "describe", "tree_sha", "tree_digest",
    "dirty", "source", "built_at",
)
RESULT_SETTLE_S = 0.01


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: Iterable[float], quantile: float) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 1)


def load_suite(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(), object_pairs_hook=_unique_json_object)
    except RecursionError as exc:
        raise ValueError("suite JSON exceeds supported nesting") from exc
    validate_suite(data)
    return data


def validate_suite(data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("suite must be a JSON object")
    if type(data.get("schema")) is not int or data["schema"] != SCHEMA:
        raise ValueError("suite must have schema=1")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("suite name must be a non-empty string")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("suite cases must be a non-empty list")
    seen = set()
    for index, case in enumerate(cases, 1):
        prefix = f"case {index}"
        if not isinstance(case, dict):
            raise ValueError(f"{prefix} must be a JSON object")
        for field in ("id", "prompt"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f"{prefix}: {field} must be a non-empty string")
        case_id = case["id"]
        if case_id in seen:
            raise ValueError(f"{prefix}: id must be unique")
        seen.add(case_id)
        _validate_expectation(case.get("expect", {}), prefix)


def _validate_expectation(expected: Any, prefix: str) -> None:
    if not isinstance(expected, dict):
        raise ValueError(f"{prefix}: expect must be a JSON object")
    unknown = set(expected) - {"equals", "contains", "regex", "max_chars", "json_equals"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"{prefix}: unknown expect assertion(s): {names}; "
                         "use equals, contains, regex, max_chars or json_equals")
    for field in ("equals", "regex"):
        if field in expected and not isinstance(expected[field], str):
            raise ValueError(f"{prefix}: expect.{field} must be a string")
    if "contains" in expected:
        value = expected["contains"]
        if not ((isinstance(value, str) and value) or (
                isinstance(value, list) and value
                and all(isinstance(item, str) and item for item in value))):
            raise ValueError(f"{prefix}: expect.contains must be a non-empty string "
                             "or a non-empty list of non-empty strings")
    if "max_chars" in expected:
        maximum = expected["max_chars"]
        if type(maximum) is not int or maximum < 0:
            raise ValueError(f"{prefix}: expect.max_chars must be a non-negative integer")
    if "regex" in expected:
        if not expected["regex"]:
            raise ValueError(f"{prefix}: expect.regex must be a non-empty string")
        try:
            re.compile(expected["regex"], re.IGNORECASE | re.MULTILINE)
        except (re.error, OverflowError, RecursionError) as exc:
            raise ValueError(f"{prefix}: invalid expect.regex: {exc}") from exc
    if "json_equals" in expected:
        try:
            _validate_json_value(expected["json_equals"], set())
        except RecursionError as exc:
            raise ValueError(f"{prefix}: expect.json_equals exceeds supported nesting") from exc
        except ValueError as exc:
            raise ValueError(f"{prefix}: invalid expect.json_equals: {exc}") from exc


def _validate_json_value(value: Any, active: set) -> None:
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("numbers must be finite")
        return
    if kind not in (list, dict):
        raise ValueError("values must use plain JSON types")
    if kind is dict and any(type(key) is not str for key in value):
        raise ValueError("object keys must be strings")
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic values are not JSON")
    active.add(identity)
    try:
        for item in value.values() if kind is dict else value:
            _validate_json_value(item, active)
    finally:
        active.remove(identity)


def _unique_json_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def _json_values_equal(actual: Any, wanted: Any) -> bool:
    pending = [(actual, wanted)]
    while pending:
        actual, wanted = pending.pop()
        if type(actual) in (int, float) and type(wanted) in (int, float):
            if actual != wanted:
                return False
            continue
        if type(actual) is not type(wanted):
            return False
        if isinstance(actual, list):
            if len(actual) != len(wanted):
                return False
            pending.extend(zip(actual, wanted))
        elif isinstance(actual, dict):
            if actual.keys() != wanted.keys():
                return False
            pending.extend((item, wanted[key]) for key, item in actual.items())
        elif actual != wanted:
            return False
    return True


def score_response(response: str, expected: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    checks: List[Dict[str, Any]] = []

    def add(kind: str, wanted: Any, passed: bool) -> None:
        checks.append({"kind": kind, "expected": wanted, "passed": passed})

    if "equals" in expected:
        wanted = str(expected["equals"])
        add("equals", wanted, response.strip() == wanted)
    if "contains" in expected:
        raw = expected["contains"]
        wanted_items = raw if isinstance(raw, list) else [raw]
        for wanted in wanted_items:
            add("contains", wanted, str(wanted).casefold() in response.casefold())
    if "regex" in expected:
        pattern = str(expected["regex"])
        add("regex", pattern, re.search(pattern, response, re.IGNORECASE | re.MULTILINE) is not None)
    if "max_chars" in expected:
        maximum = int(expected["max_chars"])
        add("max_chars", maximum, len(response.strip()) <= maximum)
    if "json_equals" in expected:
        wanted = expected["json_equals"]
        try:
            actual = json.loads(response, object_pairs_hook=_unique_json_object,
                                parse_float=_finite_json_float, parse_constant=_finite_json_float)
            passed = _json_values_equal(actual, wanted)
        except (ValueError, OverflowError, RecursionError):
            passed = False
        add("json_equals", wanted, passed)
    if not checks:
        add("nonempty", True, bool(response.strip()))
    return all(check["passed"] for check in checks), checks


def workspace_diagnostics(workspace: Path, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    tasks = workspace / "tasks"
    results = workspace / "results"
    cores = list((workspace / "state" / "cores").glob("*.alive"))
    ages = [max(0.0, now - path.stat().st_mtime) for path in cores]
    return {
        "workspace": str(workspace.resolve()),
        "exists": workspace.is_dir(),
        "tasks_writable": tasks.is_dir() and os.access(tasks, os.W_OK),
        "results_readable": results.is_dir() and os.access(results, os.R_OK),
        "live_cores": sum(age <= 90 for age in ages),
        "youngest_heartbeat_age_s": round(min(ages), 1) if ages else None,
    }


def runtime_identity(descriptor: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    if not isinstance(descriptor, dict):
        raise ValueError("runtime descriptor must be a JSON object")
    reported_workspace = descriptor.get("workspace")
    if not isinstance(reported_workspace, str):
        raise ValueError("runtime descriptor has no workspace")
    if Path(reported_workspace).expanduser().resolve() != workspace.expanduser().resolve():
        raise ValueError("runtime descriptor belongs to a different workspace")
    code = descriptor.get("code")
    if not isinstance(code, dict):
        raise ValueError("runtime descriptor has no code identity")
    revision = code.get("revision")
    source = code.get("source")
    if not (isinstance(revision, str) and len(revision) >= 8
            and all(character in "0123456789abcdefABCDEF" for character in revision)):
        raise ValueError("runtime descriptor code identity is unattributed")
    if source not in {"git", "engine-manifest"}:
        raise ValueError("runtime descriptor code identity is unattributed")
    identity = {
        "runtime_id": descriptor.get("runtimeId"),
        "repo": descriptor.get("repo"),
        "code": {field: code.get(field) for field in CODE_IDENTITY_FIELDS},
    }
    content_id = code.get("tree_digest") or code.get("tree_sha")
    identity["version_key"] = ":".join([
        source, revision, str(content_id or "content-unknown"),
        "dirty" if code.get("dirty") else "clean",
    ])
    identity["exact"] = bool(
        (source == "git" and code.get("dirty") is False)
        or (source == "engine-manifest" and code.get("tree_digest"))
    )
    return identity


def probe_runtime(config_script: Path, workspace: Path) -> Dict[str, Any]:
    result = subprocess.run(
        ["bash", str(config_script.resolve()), "runtime"], capture_output=True, text=True,
        timeout=30, cwd=config_script.resolve().parent.parent,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"runtime probe failed: {detail}")
    try:
        descriptor = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("runtime probe did not return JSON") from exc
    return runtime_identity(descriptor, workspace)


def _version_display(subject: Dict[str, Any]) -> str:
    runtime = subject.get("runtime")
    if not isinstance(runtime, dict):
        return "unattributed"
    code = runtime.get("code", {})
    describe = code.get("describe") or code.get("commit") or code.get("revision")
    if not describe:
        return "unattributed"
    suffix = " (dirty)" if code.get("dirty") and not str(describe).endswith("-dirty") else ""
    return f"{describe}{suffix} [{code.get('source') or 'unknown source'}]"


def _version_details(subject: Dict[str, Any]) -> List[str]:
    runtime = subject.get("runtime")
    if not isinstance(runtime, dict):
        return ["- Revision: `unattributed`", "- Exact attribution: **False**"]
    code = runtime.get("code", {})
    content = code.get("tree_digest") or code.get("tree_sha") or "unavailable"
    return [
        f"- Revision: `{code.get('revision') or 'unattributed'}`",
        f"- Content identity: `{content}`",
        f"- Exact attribution: **{runtime.get('exact') is True}**",
    ]


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-") or "case"


def _result_candidates(results: Path, task_id: str) -> Iterable[Path]:
    direct = results / f"{task_id}.txt"
    if direct.is_file():
        yield direct
    for path in results.glob(f"**/{task_id}.txt"):
        if path != direct and path.is_file():
            yield path


def _submit(tasks: Path, task_id: str, prompt: str) -> Path:
    body = (
        f"id: {task_id}\n"
        f"timestamp: {utc_now()}\n"
        "source: benchmark\n"
        "interaction_type: message\n"
        "access_tier: owner\n"
        "priority: low\n"
        "from: sutando-bench\n"
        f"task: {prompt}\n"
    )
    final = tasks / f"{task_id}.txt"
    temporary = tasks / f".{task_id}.{os.getpid()}.tmp"
    temporary.write_text(body)
    temporary.replace(final)
    return final


def _wait_for_result(results: Path, task_id: str, timeout_s: float,
                     poll_s: float) -> Tuple[Optional[str], Optional[Path], float]:
    start = time.perf_counter()
    deadline = start + timeout_s
    while time.perf_counter() < deadline:
        for path in _result_candidates(results, task_id):
            try:
                before = path.stat()
                time.sleep(RESULT_SETTLE_S)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    continue
                response = path.read_text(errors="replace")
                final = path.stat()
                if (after.st_size, after.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                    continue
                return response, path, (time.perf_counter() - start) * 1000
            except OSError:
                continue
        time.sleep(poll_s)
    return None, None, (time.perf_counter() - start) * 1000


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    completed = [row for row in rows if row["status"] == "completed"]
    latencies = [row["latency_ms"] for row in completed]
    passed = sum(bool(row["passed"]) for row in rows)
    return {
        "attempts": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "pass_rate": round(passed / len(rows), 4) if rows else 0.0,
        "no_response": sum(row["status"] == "timeout" for row in rows),
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 1) if latencies else None,
        },
    }


def run_suite(workspace: Path, suite: Dict[str, Any], repeat: int,
              timeout_s: float, poll_s: float, label: str,
              runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    validate_suite(suite)
    fingerprints = {
        case["id"]: "sha256:" + hashlib.sha256(json.dumps(
            {"prompt": case["prompt"], "expect": case.get("expect", {})},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")).hexdigest()
        for case in suite["cases"]
    }
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    rows: List[Dict[str, Any]] = []
    started_at = utc_now()
    for repetition in range(1, repeat + 1):
        for case_index, case in enumerate(suite["cases"], 1):
            task_id = "task-bench-{}-{}-{}-{}".format(
                run_id, case_index, _safe_id(case["id"]), repetition)
            submitted = time.perf_counter()
            _submit(workspace / "tasks", task_id, case["prompt"])
            response, result_path, wait_ms = _wait_for_result(
                workspace / "results", task_id, timeout_s, poll_s)
            latency_ms = (time.perf_counter() - submitted) * 1000
            if response is None:
                passed, checks, status = False, [], "timeout"
            else:
                passed, checks = score_response(response, case.get("expect", {}))
                status = "completed"
            rows.append({
                "case_id": case["id"],
                "category": case.get("category", "uncategorized"),
                "repetition": repetition,
                "task_id": task_id,
                "status": status,
                "passed": passed,
                "latency_ms": round(latency_ms, 1),
                "wait_ms": round(wait_ms, 1),
                "response": response,
                "result_path": str(result_path) if result_path else None,
                "checks": checks,
            })
    return {
        "schema": SCHEMA,
        "kind": "sutando-benchmark-run",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "subject": {"label": label, "workspace": str(workspace.resolve()),
                    "host": socket.gethostname(), "runtime": runtime,
                    "version_stable": None},
        "suite": {"name": suite["name"], "description": suite.get("description", ""),
                  "case_fingerprints": fingerprints},
        "configuration": {"repeat": repeat, "timeout_s": timeout_s, "poll_s": poll_s},
        "summary": summarize(rows),
        "cases": rows,
    }


def render_run(run: Dict[str, Any]) -> str:
    summary = run["summary"]
    latency = summary["latency_ms"]
    lines = [
        "# Sutando benchmark report", "",
        f"- Subject: `{run['subject']['label']}`",
        f"- Version: `{_version_display(run['subject'])}`",
    ]
    lines.extend(_version_details(run["subject"]))
    lines.extend([
        f"- Version stable for full run: **{run['subject'].get('version_stable', 'unknown')}**",
        f"- Suite: `{run['suite']['name']}`",
        f"- Pass rate: **{summary['passed']}/{summary['attempts']} ({summary['pass_rate']:.1%})**",
        f"- No response: **{summary['no_response']}**",
        f"- Completion latency: p50 **{_fmt_ms(latency['p50'])}**, p95 **{_fmt_ms(latency['p95'])}**, max **{_fmt_ms(latency['max'])}**",
        "", "| Case | Run | Result | Latency |", "|---|---:|---|---:|",
    ])
    for row in run["cases"]:
        verdict = "PASS" if row["passed"] else row["status"].upper()
        lines.append(f"| {row['case_id']} | {row['repetition']} | {verdict} | {_fmt_ms(row['latency_ms'])} |")
    failures = [row for row in run["cases"] if not row["passed"]]
    if failures:
        lines.extend(["", "## Failures", ""])
        for row in failures:
            lines.append(f"- `{row['case_id']}` run {row['repetition']}: {row['status']}")
    return "\n".join(lines) + "\n"


def _fmt_ms(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f} ms"


def _case_outcomes(run: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per-case pass/fail folded across repetitions. A case counts as passing
    only when every repetition passed — one flaky repetition is not a pass."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in run.get("cases") or []:
        case_id = row.get("case_id")
        if case_id is None:
            continue
        rec = out.setdefault(case_id, {"passed": 0, "total": 0})
        rec["total"] += 1
        if row.get("passed"):
            rec["passed"] += 1
    for rec in out.values():
        rec["all_passed"] = rec["total"] > 0 and rec["passed"] == rec["total"]
    return out


def _read_case_fingerprints(run: Dict[str, Any], case_ids: set, side: str) -> Optional[Dict[str, str]]:
    if "case_fingerprints" not in run["suite"]:
        return None
    fingerprints = run["suite"]["case_fingerprints"]
    if (not isinstance(fingerprints, dict) or set(fingerprints) != case_ids
            or any(not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
                   for value in fingerprints.values())):
        raise ValueError(f"{side}: invalid suite.case_fingerprints; "
                         "expected one SHA-256 fingerprint for every result case")
    return fingerprints


def compare_runs(baseline: Dict[str, Any], candidate: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    if baseline["suite"]["name"] != candidate["suite"]["name"]:
        raise ValueError("cannot compare runs from different suites")
    base_cases = _case_outcomes(baseline)
    cand_cases = _case_outcomes(candidate)
    shared = sorted(set(base_cases) & set(cand_cases))
    base_fingerprints = _read_case_fingerprints(baseline, set(base_cases), "baseline")
    cand_fingerprints = _read_case_fingerprints(candidate, set(cand_cases), "candidate")
    suite_warnings = []
    if base_fingerprints is None:
        suite_warnings.append("baseline_case_definitions_unknown")
    if cand_fingerprints is None:
        suite_warnings.append("candidate_case_definitions_unknown")
    if base_fingerprints is not None and cand_fingerprints is not None:
        changed = [case_id for case_id in shared
                   if base_fingerprints[case_id] != cand_fingerprints[case_id]]
        if changed:
            raise ValueError("case definitions differ for shared IDs: "
                             + ", ".join(repr(case_id) for case_id in changed)
                             + "; rerun with matching prompts and expectations")
    if set(base_cases) != set(cand_cases):
        suite_warnings.append("case_set_changed")
    base = baseline["summary"]
    cand = candidate["summary"]
    regressions = []
    if cand["pass_rate"] < base["pass_rate"]:
        regressions.append("pass_rate")
    if cand["no_response"] > base["no_response"]:
        regressions.append("no_response")
    b95 = base["latency_ms"]["p95"]
    c95 = cand["latency_ms"]["p95"]
    if b95 is not None and c95 is not None and c95 > b95 * 1.20:
        regressions.append("latency_p95")
    regressed = [c for c in shared
                 if base_cases[c]["all_passed"] and not cand_cases[c]["all_passed"]]
    recovered = [c for c in shared
                 if not base_cases[c]["all_passed"] and cand_cases[c]["all_passed"]]
    transitions = {
        "pass_to_pass": sum(1 for c in shared if base_cases[c]["all_passed"]
                            and cand_cases[c]["all_passed"]),
        "pass_to_fail": len(regressed),
        "fail_to_pass": len(recovered),
        "fail_to_fail": sum(1 for c in shared if not base_cases[c]["all_passed"]
                            and not cand_cases[c]["all_passed"]),
    }
    # An equal pass rate can hide one case breaking while another recovers, so
    # a per-case regression gates independently of the aggregate.
    if regressed:
        regressions.append("cases_regressed")
    base_runtime = baseline["subject"].get("runtime")
    cand_runtime = candidate["subject"].get("runtime")
    attribution_warnings = []
    if not isinstance(base_runtime, dict):
        attribution_warnings.append("baseline_unattributed")
    if not isinstance(cand_runtime, dict):
        attribution_warnings.append("candidate_unattributed")
    if baseline["subject"].get("version_stable") is not True:
        attribution_warnings.append("baseline_version_not_stable")
    if candidate["subject"].get("version_stable") is not True:
        attribution_warnings.append("candidate_version_not_stable")
    if isinstance(base_runtime, dict) and base_runtime.get("exact") is not True:
        attribution_warnings.append("baseline_version_not_exact")
    if isinstance(cand_runtime, dict) and cand_runtime.get("exact") is not True:
        attribution_warnings.append("candidate_version_not_exact")
    same_version = bool(
        isinstance(base_runtime, dict) and isinstance(cand_runtime, dict)
        and base_runtime.get("exact") is True and cand_runtime.get("exact") is True
        and base_runtime.get("version_key") == cand_runtime.get("version_key")
    )
    data = {
        "schema": SCHEMA,
        "kind": "sutando-benchmark-comparison",
        "baseline": baseline["subject"]["label"],
        "candidate": candidate["subject"]["label"],
        "versions": {"baseline": base_runtime, "candidate": cand_runtime},
        "attribution": {"warnings": attribution_warnings, "same_version": same_version},
        "suite_compatibility": {
            "shared_case_definitions_verified": bool(shared) and base_fingerprints is not None
                                                and cand_fingerprints is not None,
            "warnings": suite_warnings,
        },
        "regressions": regressions,
        "metrics": {
            "pass_rate": {"baseline": base["pass_rate"], "candidate": cand["pass_rate"]},
            "no_response": {"baseline": base["no_response"], "candidate": cand["no_response"]},
            "latency_p95_ms": {"baseline": b95, "candidate": c95},
        },
        "cases": {
            "transitions": transitions,
            "regressed": regressed,
            "recovered": recovered,
            "only_in_baseline": sorted(set(base_cases) - set(cand_cases)),
            "only_in_candidate": sorted(set(cand_cases) - set(base_cases)),
        },
    }
    report = "\n".join([
        "# Sutando benchmark comparison", "",
        f"Baseline: `{data['baseline']}`  ",
        f"Baseline version: `{_version_display(baseline['subject'])}`  ",
        f"Candidate: `{data['candidate']}`  ",
        f"Candidate version: `{_version_display(candidate['subject'])}`", "",
        "| Metric | Baseline | Candidate |", "|---|---:|---:|",
        f"| Pass rate | {base['pass_rate']:.1%} | {cand['pass_rate']:.1%} |",
        f"| No response | {base['no_response']} | {cand['no_response']} |",
        f"| Latency p95 | {_fmt_ms(b95)} | {_fmt_ms(c95)} |", "",
        f"Cases compared: {len(shared)}", "",
        "| Transition | Cases |", "|---|---:|",
        f"| Pass -> pass | {transitions['pass_to_pass']} |",
        f"| Pass -> FAIL | {transitions['pass_to_fail']} |",
        f"| Fail -> pass | {transitions['fail_to_pass']} |",
        f"| Fail -> fail | {transitions['fail_to_fail']} |", "",
        "Regressed cases: " + (", ".join(f"`{c}`" for c in regressed)
                               if regressed else "none"),
        "Recovered cases: " + (", ".join(f"`{c}`" for c in recovered)
                               if recovered else "none"),
        "",
        "Regressions: " + (", ".join(regressions) if regressions else "none"),
        "Attribution warnings: " + (", ".join(attribution_warnings)
                                     if attribution_warnings else "none"),
        "Same runtime version: " + ("yes" if same_version else "no"), "",
        "Shared case definitions verified: " + (
            "yes" if data["suite_compatibility"]["shared_case_definitions_verified"] else "no"),
        "Suite warnings: " + (", ".join(suite_warnings) if suite_warnings else "none"), "",
    ])
    return data, report


def write_run(run: Dict[str, Any], output: Path) -> Tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "run.json"
    report_path = output / "report.md"
    json_path.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    report_path.write_text(render_run(run))
    return json_path, report_path


def _load_run(path: Path) -> Dict[str, Any]:
    if path.is_dir():
        path = path / "run.json"
    data = json.loads(path.read_text())
    if data.get("kind") != "sutando-benchmark-run":
        raise ValueError(f"not a Sutando benchmark run: {path}")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sutando-bench")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="check a live benchmark workspace")
    doctor.add_argument("--workspace", type=Path, required=True)
    run = commands.add_parser("run", help="run a suite against a live workspace")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--timeout", type=float, default=180.0)
    run.add_argument("--poll", type=float, default=0.25)
    run.add_argument("--label", default="current")
    run.add_argument("--sutando-config", type=Path, default=DEFAULT_SUTANDO_CONFIG,
                     help="sutando-config.sh belonging to the runtime under test")
    run.add_argument("--output", type=Path)
    report = commands.add_parser("report", help="render a saved run")
    report.add_argument("run", type=Path)
    compare = commands.add_parser("compare", help="compare two saved runs")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--output", type=Path)
    compare.add_argument("--fail-on-regression", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        diagnostics = workspace_diagnostics(args.workspace)
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
        if not diagnostics["tasks_writable"] or not diagnostics["results_readable"]:
            return 2
        return 0 if diagnostics["live_cores"] else 1
    if args.command == "report":
        print(render_run(_load_run(args.run)), end="")
        return 0
    if args.command == "compare":
        try:
            data, report = compare_runs(_load_run(args.baseline), _load_run(args.candidate))
        except (OSError, ValueError) as exc:
            print(f"sutando-bench: cannot compare runs: {exc}", file=sys.stderr)
            return 2
        print(report, end="")
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "comparison.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            (args.output / "comparison.md").write_text(report)
        return 1 if args.fail_on_regression and data["regressions"] else 0
    diagnostics = workspace_diagnostics(args.workspace)
    if not diagnostics["tasks_writable"] or not diagnostics["results_readable"]:
        print("sutando-bench: workspace needs writable tasks/ and readable results/", file=sys.stderr)
        return 2
    if args.repeat < 1 or args.timeout <= 0 or args.poll <= 0:
        print("sutando-bench: repeat, timeout, and poll must be positive", file=sys.stderr)
        return 2
    try:
        suite = load_suite(args.suite)
    except (OSError, ValueError) as exc:
        print(f"sutando-bench: invalid suite: {exc}; fix the suite and retry", file=sys.stderr)
        return 2
    try:
        runtime_start = probe_runtime(args.sutando_config, args.workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sutando-bench: cannot attribute runtime version: {exc}", file=sys.stderr)
        return 2
    run = run_suite(args.workspace, suite, args.repeat, args.timeout, args.poll,
                    args.label, runtime_start)
    try:
        runtime_end = probe_runtime(args.sutando_config, args.workspace)
        run["subject"]["version_stable"] = runtime_start == runtime_end
        if runtime_start != runtime_end:
            run["subject"]["runtime_end"] = runtime_end
    except (OSError, RuntimeError, ValueError) as exc:
        run["subject"]["version_stable"] = False
        run["subject"]["runtime_end_error"] = str(exc)
    output = args.output or Path("benchmark-runs") / run["run_id"]
    json_path, report_path = write_run(run, output)
    print(render_run(run), end="")
    print(f"Artifacts: {json_path} {report_path}")
    if run["subject"]["version_stable"] is not True:
        return 2
    return 0 if run["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
