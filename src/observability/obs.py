"""``emit(ev)`` -- the single, universal observability facade (Python twin of obs.ts).

Best-effort, sampleable, structurally unable to raise into its caller. Every
Sutando Python service calls this instead of ``print`` for anything that matters.

Contract:
  - stamps ``schema``, ``ts``, ``node``, and (if absent) ``trace_id``.
  - sampling drops only ``ok`` events; ``error``/``denied`` and ``usage.recorded``
    are ALWAYS kept.
  - each sink write is isolated; nothing propagates out.
  - on first emit with no registered sink, the configured default sinks
    (jsonl-file) auto-register from ``load_observability_config()``.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .ids import new_trace_id
from .node import node_id
from .config import load_observability_config
from .sink import Sink, sink_from_config

__all__ = ["emit", "register_sink", "set_sampler", "reset_sinks"]

_sinks: list[Sink] = []
_auto_loaded = False
_trace_sample = 1.0
_trace_loaded = False
_custom_sampler: Callable[[dict[str, Any]], bool] | None = None


def register_sink(sink: Sink) -> None:
    """Register an additional sink. Suppresses default-sink auto-loading."""
    _sinks.append(sink)


def set_sampler(fn: Callable[[dict[str, Any]], bool]) -> None:
    """Override the sampler for ``ok`` events. Return True to keep."""
    global _custom_sampler
    _custom_sampler = fn


def reset_sinks() -> None:
    """Test hook: forget sinks + sampler + cached config."""
    global _sinks, _auto_loaded, _trace_sample, _trace_loaded, _custom_sampler
    _sinks = []
    _auto_loaded = False
    _trace_sample = 1.0
    _trace_loaded = False
    _custom_sampler = None


def _load_sample_once() -> None:
    global _trace_loaded, _trace_sample
    if _trace_loaded:
        return
    _trace_loaded = True
    try:
        _trace_sample = load_observability_config()["observability"]["sampling"]["trace"]
    except Exception:
        pass  # keep 1.0


def _ensure_default_sinks() -> None:
    global _auto_loaded
    if _auto_loaded or _sinks:
        return
    _auto_loaded = True
    try:
        for sc in load_observability_config()["observability"]["sinks"]:
            s = sink_from_config(sc)
            if s is not None:
                _sinks.append(s)
    except Exception:
        pass  # leave empty; emit still no-ops safely


def _should_keep(ev: dict[str, Any]) -> bool:
    if ev.get("outcome") != "ok" or ev.get("kind") == "usage.recorded":
        return True
    if _custom_sampler is not None:
        return _custom_sampler(ev)
    import random

    return random.random() < _trace_sample


def emit(ev_input: dict[str, Any]) -> None:
    try:
        ev: dict[str, Any] = {
            "schema": 1,
            "ts": round(time.time(), 3),
            "trace_id": ev_input.get("trace_id") or new_trace_id(),
            "node": ev_input.get("node") or node_id(),
            "source": ev_input["source"],
            "actor": ev_input["actor"],
            "kind": ev_input["kind"],
            "outcome": ev_input["outcome"],
        }
        for opt in ("span_id", "parent_span_id", "source_file", "duration_ms", "usage", "data"):
            if ev_input.get(opt) is not None:
                ev[opt] = ev_input[opt]

        _load_sample_once()
        if not _should_keep(ev):
            return

        _ensure_default_sinks()
        for sink in _sinks:
            try:
                sink.write(ev)
            except Exception:
                pass  # one bad sink never blocks the others
    except Exception:
        pass  # emit() is structurally incapable of raising into its caller
