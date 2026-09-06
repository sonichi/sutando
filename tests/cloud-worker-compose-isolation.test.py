#!/usr/bin/env python3
r"""Per-user cloud workers must not share a network or a sidecar token.

keweichen blocker 4 on #3803: the template advertised "N users = N service
blocks" while declaring no networks (every service joins the project default,
so any worker can reach any other user's sidecar) and sourcing every sidecar's
token from one project-level `${AG2ASSISTANT_ACP_TOKEN}`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# yaml reaches CI transitively via detect-secrets (its install pulls pyyaml);
# if this import ever fails, that dependency changed, not this test.
import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "deploy" / "cloud-worker" / "docker-compose.yml"
FAILS: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("  ok  " if ok else "  FAIL ") + msg)
    if not ok:
        FAILS.append(msg)


def main() -> int:
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = doc.get("services") or {}
    print(f"1. every service declares its own network ({len(services)} services)")
    check(bool(doc.get("networks")), "the file declares a `networks:` section at all")
    for name, svc in services.items():
        nets = svc.get("networks") or []
        check(bool(nets), f"{name}: declares networks (default project network = shared)")

    print("2. a worker and ITS sidecar share a network; different users do not")
    worker = services.get("worker-example") or {}
    sidecar = services.get("assistant-example") or {}
    check(set(worker.get("networks") or []) == set(sidecar.get("networks") or []),
          f"the example pair shares one network: {worker.get('networks')} vs {sidecar.get('networks')}")
    declared = set((doc.get("networks") or {}).keys())
    check("example" in declared, f"that network is declared: {sorted(declared)}")

    print("3. the sidecar token is PER USER, not project-wide")
    tok = str(((sidecar.get("environment") or {}).get("AG2ASSISTANT_ACP_TOKEN")) or "")
    check(tok != "${AG2ASSISTANT_ACP_TOKEN:-}",
          f"not the shared project-level var: {tok!r}")
    check(re.fullmatch(r"\$\{AG2ASSISTANT_ACP_TOKEN_[A-Z0-9_]+:?-?\}", tok) is not None,
          f"it is a per-user var: {tok!r}")

    print("4. the copy-a-user instructions name both per-user things")
    head = COMPOSE.read_text(encoding="utf-8")
    check("networks" in head.split("services:")[0],
          "the header tells an operator to change the network name")
    check("AG2ASSISTANT_ACP_TOKEN_" in head.split("services:")[0],
          "the header tells an operator to change the token var")

    print("\n" + (f"FAILED ({len(FAILS)})" if FAILS else "PASS — per-user compose isolation"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
