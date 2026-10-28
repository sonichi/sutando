"""Run a fixed AppleScript with caller values as osascript arguments.

Values reach the script's ``on run argv`` handler as data, never as source, so
no value can change what the script does.
"""


def osascript_cmd(script: str, *values: str) -> list[str]:
    # "--" ends osascript's own options, so a value starting with "-" stays data.
    return ["osascript", "-e", script, "--", *(str(v) for v in values)]
