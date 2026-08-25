#!/usr/bin/env python3
"""dm-result must refuse a [channel:] redirect instead of silently DMing the body.

parse_markers() STRIPS the marker, so a dropped redirect is invisible downstream:
the body arrives in the owner DM looking well-formed, addressed to nobody in particular.
"""

from contextlib import redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import tempfile

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "dm-result.py"

_TD = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = _TD
os.environ["DISCORD_BOT_TOKEN"] = ""
os.environ["SUTANDO_DM_OWNER_ID"] = ""

spec = importlib.util.spec_from_file_location("dm_result_redirect", SCRIPT)
dm = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(dm)

CHAN = "1538218588937134170"


def _send(text):
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = dm.send_dm(text)
    return rc, buf.getvalue()


def test_redirect_is_refused_by_name():
    rc, err = _send(f"[channel: {CHAN}]\nbody meant for a channel")
    assert rc is False, "a body it cannot route must not report success"
    # The RETURN VALUE cannot discriminate: with no token the pre-fix code also
    # returns False. Only the reason names the redirect, so assert on that.
    assert CHAN in err, f"refusal must name the channel it could not reach; got: {err!r}"
    assert "redirect" in err.lower(), f"refusal must name the cause; got: {err!r}"


def test_control_no_redirect_fails_for_a_different_reason():
    """Without a redirect the guard must not fire — otherwise it refuses everything."""
    rc, err = _send("an ordinary body with no markers at all")
    assert rc is False, "no token in this env, so it still fails"
    assert "redirect" not in err.lower(), f"guard fired on a body with no redirect: {err!r}"
    assert CHAN not in err


def test_marker_below_the_first_line_is_not_a_redirect():
    """CLAUDE.md scopes [channel:] to the FIRST non-empty line; the guard must match.

    Refusing on a marker mentioned mid-prose would block ordinary bodies that merely
    quote one -- over-refusal is the failure mode on this side.
    """
    rc, err = _send(f"leading line\n[channel: {CHAN}]\ntrailing")
    assert rc is False, "no token in this env, so it still fails"
    assert "redirect" not in err.lower(), (
        f"guard fired on a non-first-line marker, which the bridge does not treat "
        f"as a redirect; got: {err!r}"
    )


if __name__ == "__main__":
    test_redirect_is_refused_by_name()
    test_control_no_redirect_fails_for_a_different_reason()
    test_marker_below_the_first_line_is_not_a_redirect()
    print("PASS: 3/3")
