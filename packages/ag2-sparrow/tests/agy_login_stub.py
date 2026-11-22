#!/usr/bin/env python3
"""A stand-in for `agy acp --login` (tests only): prints a sign-in link, asks for the code, exits 0 for the
dummy code DUMMY-CODE-1234 (naming a dummy account), 1 for any other. Writes nothing anywhere."""
import sys

print("\x1b[1mSign in with Google\x1b[0m: https://accounts.google.com/o/oauth2/v2/auth?client_id=dummy&state=s1")
print("Paste the authorization code here: ", end="", flush=True)
code = sys.stdin.readline().strip()
if code == "DUMMY-CODE-1234":
    print("\nLogged in as alice@example.com")
    sys.exit(0)
print("\ninvalid authorization code")
sys.exit(1)
