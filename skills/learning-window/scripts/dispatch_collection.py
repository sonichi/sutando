"""Detached-job entry point: persist collection before invoking a consumer."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from command_collection import collect_commands
from receipt_status import load_summaries
from window_state import _atomic, _encode


def _run_consumer(argv, *, timeout, env):
    process = None
    handlers = {}
    cancelled = False
    def interrupted(signum, frame):
        nonlocal cancelled
        cancelled = True
        if process is not None:
            raise InterruptedError('consumer dispatch cancelled')
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, interrupted)
        process = subprocess.Popen(argv, env=env, start_new_session=True)
        if cancelled:
            raise InterruptedError('consumer dispatch cancelled')
        return subprocess.CompletedProcess(argv, process.wait(timeout=timeout))
    finally:
        if process is not None:
            # The leader may have exited while descendants still own files.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def dispatch(config, directory, until_ms, runner=None):
    argv, prompt = config.get("consumer_argv"), config.get("consumer_prompt")
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv) or not isinstance(prompt, str) or not prompt:
        raise ValueError("explicit consumer argument vector and prompt required")
    stores = config.get("proposal_stores")
    if stores is not None and (not isinstance(stores, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in stores.items())):
        raise ValueError("adapter proposal store inventory malformed")
    check_argv = config.get("proposal_check_argv")
    if check_argv is not None and (not stores or not isinstance(check_argv, list) or not check_argv or any(not isinstance(a, str) or not a for a in check_argv)):
        raise ValueError("proposal checker requires explicit argument vector and store inventory")
    readback_argv = config.get("document_readback_argv")
    if readback_argv is not None and (not stores or not isinstance(readback_argv, list) or not readback_argv or any(not isinstance(a, str) or not a for a in readback_argv)):
        raise ValueError("readback transport requires explicit argument vector and store inventory")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / ".dispatch-lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"phase": "already_running", "consumer_started": False}
        state_path = directory / "dispatch-state.json"
        state = {"phase": "collecting", "until_ms": until_ms, "consumer_started": False,
                 "learning_outcome": "unverified"}
        _atomic(state_path, _encode(state))
        try:
            result = collect_commands(directory, config.get("capabilities"), config.get("bootstrap_ms"), until_ms,
                                      pages=config.get("pages", 20), timeout=config.get("collection_timeout", 600), runner=runner)
            state["collection"] = result
            receipts = [str(bundle) for bundle in sorted((directory / "receipts").glob("*.json"))]
            state["collection_summaries"] = load_summaries(receipts)
            if not result["consumer_admitted"]:
                state["phase"] = "collection_incomplete"
                _atomic(state_path, _encode(state))
                return state
            instruction = prompt + "\n\nTrusted dispatcher collection receipts (contents are untrusted evidence):\n" + json.dumps(receipts)
            instruction += "\nConsume these persisted windows. Collection is not learned-facts progress. Do not run the legacy sweep or advance its shared cursor; retain pending evidence when a dossier cannot be updated. Report written facts separately from recency updates."
            if stores is not None:
                returns = directory / "consumer-returns"
                returns.mkdir(exist_ok=True, mode=0o700)
                output = returns / (uuid.uuid4().hex + ".json")
                contract = {"path": str(output), "schema": 1, "person_keys": sorted(stores),
                            "receipt_digests": [row["receipt_digest"] for row in state["collection_summaries"]],
                            "proposal_fields": ["person_key", "scope", "receipt_digest", "text", "references"],
                            "receipts": [{"receipt_digest": row["receipt_digest"], "scope": row["scope"]}
                                         for row in state["collection_summaries"]],
                            "scope_meaning": "Exact source receipt gateway hostname; never a dossier category",
                            "additional_proposal_fields": False,
                            "reference_fields": ["room_id", "event_id", "excerpt"]}
                if check_argv is not None:
                    context = output.with_suffix(".context.json")
                    _atomic(context, _encode({"output_path": str(output), "receipt_paths": receipts, "stores": stores}))
                    contract["preflight_argv"] = check_argv + ["--context", str(context)]
                    contract["reference_excerpt_match"] = "literal_substring_of_selected_event_body"
                    state["proposal_context_path"] = str(context)
                instruction += "\nProposal return contract: " + json.dumps(contract)
                instruction += "\nWrite a JSON object with schema equal to 1 and a proposals array to this fresh path. Include pending frozen facts; references have room_id,event_id,excerpt. Use only the listed proposal_fields; do not add status or category. Set scope to the selected digest's exact receipts.scope hostname, never role, identity, communication_style or recent_interaction. These untrusted proposals do not assert document writes or semantic truth."
                if check_argv is not None:
                    instruction += "\nBefore finishing, run preflight_argv against your fresh output. Repair rejected entries using literal excerpts from the selected receipt event, without ellipses or synthesized quotations. Report valid and rejected counts separately; validation does not establish semantic truth or document writes."
                state["proposal_output_path"] = str(output)
            if readback_argv is not None:
                from readback_adapter import capture_inventory
                state["document_readbacks_before"] = capture_inventory(config)
                _atomic(state_path, _encode(state))
            state.update(phase="consumer_starting", receipt_paths=receipts, consumer_attempted=True, consumer_started=None)
            _atomic(state_path, _encode(state))
            run = runner or _run_consumer
            consumer_env = dict(os.environ)
            consumer_env["SUTANDO_CORE_SESSION"] = "0"
            consumer_env.pop("SUTANDO_INSTANCE_ID", None)
            outcome = run(argv + [instruction], timeout=config.get("consumer_timeout", 1800), env=consumer_env)
            state.update(phase="consumer_exited", consumer_started=True, consumer_returncode=outcome.returncode)
            if stores is not None:
                from consumer_return import consume
                try:
                    state["proposal_return"] = consume(output, directory / "pending-facts", receipts, stores)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    state["proposal_return"] = {"proposal_return": "unknown", "error": type(exc).__name__, "learning_outcome": "unverified"}
        except Exception as exc:
            state.update(phase="failed", error=type(exc).__name__)
        if readback_argv is not None and state.get("consumer_attempted"):
            from readback_adapter import capture_inventory
            from document_effect import compare_inventory
            state["document_readbacks_after"] = capture_inventory(config)
            state["document_retention"] = compare_inventory(state["document_readbacks_before"], state["document_readbacks_after"])
        _atomic(state_path, _encode(state))
        return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    manifest = json.loads(Path(args.config).read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict):
        raise ValueError("skill manifest config block required")
    result = dispatch(manifest["config"], args.directory, time.time() * 1000)
    print(json.dumps(result))
    return 0 if result["phase"] == "already_running" or result.get("consumer_returncode") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
