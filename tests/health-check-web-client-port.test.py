#!/usr/bin/env python3
"""Regression coverage for CLIENT_PORT-aware web-client health checks."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("health_check_web_port", SRC)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("ok  " if condition else "FAIL") + " " + label)
    if not condition:
        failures.append(label)


with tempfile.TemporaryDirectory() as tmp:
    env_path = Path(tmp) / ".env"

    check(
        hc.resolve_web_client_port(env={}, env_path=env_path) == {"port": 8080},
        "missing CLIENT_PORT uses the startup default",
    )

    env_path.write_text("CLIENT_PORT=8081\n")
    check(
        hc.resolve_web_client_port(env={}, env_path=env_path) == {"port": 8081},
        "canonical dotenv CLIENT_PORT is honored",
    )
    check(
        hc.resolve_web_client_port(env={"CLIENT_PORT": "9090"}, env_path=env_path) == {"port": 8081},
        "sourced dotenv overrides an inherited process value",
    )
    check(
        hc.resolve_web_client_port(env={"CLIENT_PORT": ""}, env_path=env_path) == {"port": 8081},
        "sourced dotenv overrides an empty inherited process value",
    )

    env_path.write_text("OTHER_PORT=7000\n")
    check(
        hc.resolve_web_client_port(env={"CLIENT_PORT": "9090"}, env_path=env_path) == {"port": 9090},
        "process value survives when dotenv does not assign CLIENT_PORT",
    )

    env_path.write_text("CLIENT_PORT=\n")
    check(
        hc.resolve_web_client_port(env={"CLIENT_PORT": "9090"}, env_path=env_path) == {"port": 8080},
        "empty sourced dotenv value matches startup fallback semantics",
    )

    env_path.write_text('export CLIENT_PORT="8181" # local conflict\n')
    check(
        hc.resolve_web_client_port(env={}, env_path=env_path) == {"port": 8181},
        "export, quotes, and comments match shell dotenv syntax",
    )

    env_path.write_text(
        "# comment\n"
        "\n"
        "OTHER_PORT=7000\n"
        "CLIENT_PORT_EXTRA=7001\n"
        "CLIENT_PORT=8082\n"
    )
    check(
        hc.resolve_web_client_port(env={}, env_path=env_path) == {"port": 8082},
        "comments, unrelated keys, and prefixed keys are ignored",
    )

    for malformed in (
        "CLIENT_PORT\n",
        'CLIENT_PORT="unterminated\n',
        "CLIENT_PORT=8080 extra\n",
    ):
        env_path.write_text(malformed)
        check(
            "error" in hc.resolve_web_client_port(env={}, env_path=env_path),
            f"malformed dotenv value {malformed.strip()!r} fails closed",
        )

    check(
        "error" in hc.resolve_web_client_port(env={}, env_path=Path(tmp)),
        "unreadable dotenv path fails closed",
    )

    env_path.write_text("OTHER_PORT=7000\n")
    for invalid in ("not-a-port", "0", "65536"):
        result = hc.resolve_web_client_port(env={"CLIENT_PORT": invalid}, env_path=env_path)
        check("error" in result, f"invalid CLIENT_PORT={invalid!r} fails closed")

with patch.object(
    hc,
    "resolve_web_client_port",
    return_value={"error": "invalid CLIENT_PORT='bad'"},
):
    checks = hc.run_all_checks()
    web_check = next(item for item in checks if item["name"] == "web-client")
    check(
        web_check == {
            "name": "web-client",
            "status": "down",
            "detail": "invalid CLIENT_PORT='bad'",
        },
        "run_all_checks reports invalid CLIENT_PORT as required health failure",
    )

source = SRC.read_text()
check(
    'check_port(web_config["port"], "web-client", probe=True)' in source
    and 'check_port(8080, "web-client", probe=True)' not in source,
    "run_all_checks probes the resolved port instead of hardcoded 8080",
)

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — web-client health follows CLIENT_PORT")
