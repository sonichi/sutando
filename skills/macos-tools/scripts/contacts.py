#!/usr/bin/env python3
"""
Sutando contacts reader — search macOS Contacts via AppleScript.

Usage:
  python3 src/contacts.py search "Bob"         # search by name
  python3 src/contacts.py search "bob@x.com"   # search by email
  python3 src/contacts.py all                   # list all (first 50)

Output: name, email, phone for matching contacts.
"""

import json
import re
import subprocess
import sys


def search_contacts(query: str) -> list[dict]:
    # Ensure Contacts.app is running (AppleScript fails with -600 if not)
    import time
    subprocess.run(["open", "-ga", "Contacts"], capture_output=True, timeout=5)
    time.sleep(1)
    # Search by name or email
    script = f"""
tell application "Contacts"
    set output to ""
    set results to (every person whose name contains "{query}")
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
"""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
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


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 src/contacts.py search 'name or email'")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "search" and len(sys.argv) > 2:
        query = sys.argv[2]
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
    elif cmd == "add" and len(sys.argv) >= 3:
        name = sys.argv[2]
        phone = None
        email = None
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--phone" and i + 1 < len(sys.argv):
                phone = sys.argv[i + 1]; i += 2
            elif sys.argv[i] == "--email" and i + 1 < len(sys.argv):
                email = sys.argv[i + 1]; i += 2
            else:
                i += 1
        parts = name.split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""
        import time
        subprocess.run(["open", "-ga", "Contacts"], capture_output=True, timeout=5)
        time.sleep(1)
        props = f'first name:"{first}"'
        if last:
            props += f', last name:"{last}"'
        lines = [f'set newPerson to make new person with properties {{{props}}}']
        if phone:
            lines.append(f'make new phone at end of phones of newPerson with properties {{label:"mobile", value:"{phone}"}}')
        if email:
            lines.append(f'make new email at end of emails of newPerson with properties {{label:"home", value:"{email}"}}')
        lines.append("save")
        script = 'tell application "Contacts"\n' + "\n".join(lines) + '\nend tell'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            print(f"Error: {result.stderr.strip()}")
            sys.exit(1)
        print(f"Added {name}" + (f" phone:{phone}" if phone else "") + (f" email:{email}" if email else ""))
    elif cmd == "all":
        results = search_contacts("")
        if not results:
            print("No contacts.")
            return
        print(json.dumps(results, indent=2))
    else:
        print("Usage: python3 src/contacts.py search 'name'")
        print("       python3 src/contacts.py add 'Name' --phone 123 --email a@b.com")
        sys.exit(1)


if __name__ == "__main__":
    main()
