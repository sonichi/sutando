"""Which form a cron entry executes as — the ONE definition.

`src/cron-runner.py` decides whether to run an entry and
`src/dashboard_schedules.py` decides how to describe it. When those two read
`shell_command` independently they disagree, and the disagreement is silent:
the runner skips a malformed entry while the dashboard advertises a fallback
skill that will never fire. Both bind this selector instead.

Precedence is shell_command > prompt_skill > prompt, matching
skills/schedule-crons/SKILL.md. Stdlib only, no I/O — callers pass a parsed
entry dict and resolve paths themselves.
"""
from __future__ import annotations

SHELL = "shell"
SKILL = "skill"
PROMPT = "prompt"
MALFORMED = "malformed"


def select_execution_form(entry: dict) -> tuple[str, str]:
    """Return (kind, target) for one crons.json entry.

    `MALFORMED` means the shell key is PRESENT but unusable — a blank string or
    a non-string. That is terminal, never a fall-through to the skill or prompt
    leg: the runner refuses to execute such an entry, so any surface claiming a
    skill would be describing work that never runs.

    The target is the payload VERBATIM. Whitespace decides whether a form is
    usable; it never edits what runs, because these are tuned prompts.
    """
    if not isinstance(entry, dict):
        return MALFORMED, ""
    # Presence, not truthiness: `entry.get(...) or ''` erases the difference
    # between an absent key and a present-but-empty one, which is the bug.
    if "shell_command" in entry:
        cmd = entry.get("shell_command")
        if not isinstance(cmd, str) or not cmd.strip():
            return MALFORMED, _describe_bad_shell(cmd)
        return SHELL, cmd
    skill = entry.get("prompt_skill")
    if isinstance(skill, str) and skill.strip():
        return SKILL, skill
    prompt = entry.get("prompt")
    return PROMPT, prompt if isinstance(prompt, str) else ""


def _describe_bad_shell(cmd) -> str:
    """Name why it is unusable, so a surface can say so instead of guessing."""
    if not isinstance(cmd, str):
        return f"shell_command is {type(cmd).__name__}, not a string"
    return "shell_command is blank"


# Which forms an executor can actually run. A launchd cron shells out; the
# Codex runner only enqueues an agent task, so a shell entry is not runnable.
LAUNCHD_FORMS = (SHELL, SKILL, PROMPT)
CODEX_FORMS = (SKILL, PROMPT)

# Keyed by schedule_owner()'s vocabulary. Only the launchd cron-runner shells
# out; every other owner hands a prompt to an agent.
EXECUTOR_FORMS = {
    "launchd": LAUNCHD_FORMS,
    "codex": CODEX_FORMS,
    "session": CODEX_FORMS,
    "dynamic-loop": CODEX_FORMS,
}


def select_for_executor(entry: dict, supported) -> tuple[str, str]:
    """(kind, target) for an executor that runs only `supported` forms.

    An unsupported form is MALFORMED *for that executor* — never a fall-through
    to a lower-precedence leg. Falling through is how a surface comes to
    advertise a shell command while the executor quietly launches a skill.
    """
    kind, target = select_execution_form(entry)
    if kind == MALFORMED:
        return kind, target
    if kind not in supported:
        return MALFORMED, f"{kind} entries are not runnable by this scheduler"
    return kind, target


# ── who owns a schedule ───────────────────────────────────────────────────
LOOP_SKILL = "proactive-loop"
LOOP_NAME = "main-loop"


def is_proactive_loop(entry: dict) -> bool:
    """Whether this entry IS the recurring proactive driver.

    Decided by the SELECTED form, never by a raw key. `shell_command` outranks
    `prompt_skill`, so a record carrying both runs as shell — and a shell
    record that merely mentions the loop is a shell schedule. Reading the raw
    key exempts that shell form from launchd (the only executor that can run
    it) and hands it to Codex (which cannot), leaving it owned by nobody.
    """
    kind, target = select_execution_form(entry)
    if kind in (SHELL, MALFORMED):
        return False
    return target.strip().lstrip("/") == LOOP_SKILL or entry.get("name") == LOOP_NAME


def launchd_eligible(entry: dict) -> bool:
    """Whether the durable launchd runner should own this entry.

    The reconciler, the Codex scheduler and the health probe all have to agree
    on this; when each spelled it out separately they agreed only by
    coincidence, and a mixed record made all three answer "not mine".
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("launchd") is True or entry.get("execution") == "codex-task":
        return False
    if entry.get("loop") == "dynamic" or not entry.get("cron"):
        return False
    return not is_proactive_loop(entry)
