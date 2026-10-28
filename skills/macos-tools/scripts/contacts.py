#!/usr/bin/env python3
"""
Sutando contacts reader — search macOS Contacts via AppleScript.

Usage (``--owner-asked`` only when the owner asked for the local Contacts app —
it raises a macOS permission prompt; without it the script refuses, exit 2; a
macOS denial (-1743) exits 3 with no retry):
  python3 contacts.py search "Bob" --owner-asked            # search by name
  python3 contacts.py search "bob@x.com" --owner-asked      # search by email
  python3 contacts.py add "Bob Smith" --phone 123 --email a@b.com --owner-asked
  python3 contacts.py update "Bob" --phone 456 --owner-asked          # update phone
  python3 contacts.py update "Bob" --email new@x.com --owner-asked    # update email
  python3 contacts.py all --owner-asked                     # list all (first 50)

Output: name, email, phone for matching contacts.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# Sibling helper: the scripts run by path, so their directory is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_pim_consent as consent  # noqa: E402
from applescript_args import osascript_cmd  # noqa: E402

SEARCH_SCRIPT = """
on run argv
set searchText to item 1 of argv
tell application "Contacts"
    set output to ""
    set results to (every person whose name contains searchText)
    if (count of results) > 20 then set results to items 1 thru 20 of results
    repeat with p in results
        set pName to name of p
        set pEmails to ""
        repeat with e in emails of p
            set pEmails to pEmails & (value of e) & ","
        end repeat
        set pPhones to ""
        repeat with ph in phones of p
            set pPhones to pPhones & (value of ph) & ","
        end repeat
        set output to output & pName & "|||" & pEmails & "|||" & pPhones & "\\n"
    end repeat
    return output
end tell
end run
"""

ADD_SCRIPT = """
on run argv
set firstName to item 1 of argv
set lastName to item 2 of argv
set phoneValue to item 3 of argv
set emailValue to item 4 of argv
tell application "Contacts"
if lastName is "" then
    set newPerson to make new person with properties {first name:firstName}
else
    set newPerson to make new person with properties {first name:firstName, last name:lastName}
end if
if phoneValue is not "" then
    make new phone at end of phones of newPerson with properties {label:"mobile", value:phoneValue}
end if
if emailValue is not "" then
    make new email at end of emails of newPerson with properties {label:"home", value:emailValue}
end if
save
end tell
end run
"""

UPDATE_SCRIPT = """
on run argv
set searchText to item 1 of argv
set phoneValue to item 2 of argv
set emailValue to item 3 of argv
tell application "Contacts"
set results to (every person whose name contains searchText)
if (count of results) = 0 then
    return "NOT_FOUND: no contact matching " & searchText
end if
set p to item 1 of results
if phoneValue is not "" then
    -- Remove existing phones and add new one
    repeat while (count of phones of p) > 0
        delete (item 1 of phones of p)
    end repeat
    make new phone at end of phones of p with properties {label:"mobile", value:phoneValue}
end if
if emailValue is not "" then
    repeat while (count of emails of p) > 0
        delete (item 1 of emails of p)
    end repeat
    make new email at end of emails of p with properties {label:"home", value:emailValue}
end if
save
return "OK: updated " & (name of p)
end tell
end run
"""


def _osascript(script: str, timeout: int, *values: str) -> subprocess.CompletedProcess:
    result = subprocess.run(osascript_cmd(script, *values), capture_output=True, text=True, timeout=timeout)
    consent.exit_if_denied("Contacts", result.stderr)
    return result


def search_contacts(query: str) -> list[dict]:
    # Ensure Contacts.app is running (AppleScript fails with -600 if not)
    import time
    subprocess.run(["open", "-ga", "Contacts"], capture_output=True, timeout=5)
    time.sleep(1)
    # Search by name or email
    result = _osascript(SEARCH_SCRIPT, 30, query)
    if result.returncode != 0:
        return [{"error": result.stderr.strip()}]

    contacts = []
    seen = set()
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        if name in seen:
            continue
        seen.add(name)
        emails = [e.strip() for e in parts[1].split(",") if e.strip()]
        phones = [p.strip() for p in parts[2].split(",") if p.strip()] if len(parts) > 2 else []
        contacts.append({"name": name, "emails": emails, "phones": phones})
    return contacts


def main(argv=None):
    argv = consent.require_consent("Contacts", argv)
    consent.exit_if_denied_earlier("Contacts")
    if len(argv) < 2:
        print("Usage: python3 contacts.py search 'name or email' --owner-asked")
        sys.exit(1)

    cmd = argv[1]
    if cmd == "search" and len(argv) > 2:
        query = argv[2]
        results = search_contacts(query)
        if not results:
            print(f"No contacts matching '{query}'")
            return
        if "error" in results[0]:
            print(f"Error: {results[0]['error']}")
            return
        for c in results:
            print(f"  {c['name']}")
            for e in c["emails"]:
                print(f"    email: {e}")
            for p in c["phones"]:
                print(f"    phone: {p}")
    elif cmd == "add" and len(argv) >= 3:
        name = argv[2]
        phone = None
        email = None
        i = 3
        while i < len(argv):
            if argv[i] == "--phone" and i + 1 < len(argv):
                phone = argv[i + 1]; i += 2
            elif argv[i] == "--email" and i + 1 < len(argv):
                email = argv[i + 1]; i += 2
            else:
                i += 1
        parts = name.split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""
        import time
        subprocess.run(["open", "-ga", "Contacts"], capture_output=True, timeout=5)
        time.sleep(1)
        result = _osascript(ADD_SCRIPT, 15, first, last, phone or "", email or "")
        if result.returncode != 0:
            print(f"Error: {result.stderr.strip()}")
            sys.exit(1)
        print(f"Added {name}" + (f" phone:{phone}" if phone else "") + (f" email:{email}" if email else ""))
    elif cmd == "update" and len(argv) >= 3:
        name = argv[2]
        phone = None
        email = None
        i = 3
        while i < len(argv):
            if argv[i] == "--phone" and i + 1 < len(argv):
                phone = argv[i + 1]; i += 2
            elif argv[i] == "--email" and i + 1 < len(argv):
                email = argv[i + 1]; i += 2
            else:
                i += 1
        if not phone and not email:
            print("Error: provide --phone and/or --email to update")
            sys.exit(1)
        import time
        subprocess.run(["open", "-ga", "Contacts"], capture_output=True, timeout=5)
        time.sleep(1)
        result = _osascript(UPDATE_SCRIPT, 15, name, phone or "", email or "")
        if result.returncode != 0:
            print(f"Error: {result.stderr.strip()}")
            sys.exit(1)
        print(result.stdout.strip())
    elif cmd == "all":
        results = search_contacts("")
        if not results:
            print("No contacts.")
            return
        print(json.dumps(results, indent=2))
    else:
        print("Usage: python3 contacts.py search 'name' --owner-asked")
        print("       python3 contacts.py add 'Name' --phone 123 --email a@b.com --owner-asked")
        print("       python3 contacts.py update 'Name' --phone 456 --email new@x.com --owner-asked")
        sys.exit(1)


if __name__ == "__main__":
    main()
