#!/usr/bin/env python3
"""Microsoft 365 connectivity CLI, built on the open-source python-o365 (O365)
library, which wraps Microsoft Graph.

Subcommands:
  auth            Run the delegated OAuth2 consent flow and cache the token.
  onedrive-list   List items in a OneDrive folder (root by default).
  onedrive-get    Download a OneDrive file by path to a local destination.
  outlook-list    List recent Outlook inbox messages.
  outlook-send    Send an Outlook email.
  calendar-list   List upcoming calendar events over N days.
  teams-post      Post a message to a Teams channel.

Credentials are read from the environment (populate from the Sutando vault):
  MS365_CLIENT_ID, MS365_CLIENT_SECRET, MS365_TENANT_ID
Never hardcode secrets. The OAuth token is cached under the workspace state dir
at state/ms365-token/ (override the base with MS365_STATE_DIR).
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# python-o365 is imported LAZILY (see _require_o365 below), not at module
# level, so `ms365.py --help` and argparse work on any interpreter — including
# Python 3.9, where python-o365 itself won't import (it needs 3.10+). Only the
# commands that actually talk to Microsoft Graph pull the dependency in.
# ---------------------------------------------------------------------------
def _require_o365():  # pragma: no cover - live dependency; not unit-testable
    """Import python-o365, exiting with a helpful message if it can't load.

    Catches SyntaxError as well as ImportError: on Python < 3.10 the O365
    source uses 3.10+ syntax and raises SyntaxError on import, which we turn
    into a clean 'requires Python 3.10+' message instead of a traceback.
    """
    try:
        from O365 import Account, FileSystemTokenBackend
    except (ImportError, SyntaxError):
        sys.stderr.write(
            "Cannot load 'O365' (python-o365). Install it with:\n"
            "    pip install O365\n"
            "    (or: pip install -r requirements.txt)\n"
            "python-o365 requires Python 3.10+; this interpreter is "
            f"{sys.version_info.major}.{sys.version_info.minor}.\n"
        )
        sys.exit(1)
    return Account, FileSystemTokenBackend


# Delegated Microsoft Graph scopes this skill requests. These must match the
# delegated permissions granted on the Azure AD app registration.
#
# Do NOT list the MSAL-reserved scopes here (openid / offline_access / profile):
# MSAL's initiate_auth_code_flow adds them itself and raises ValueError
# ("You cannot use any scope value that is reserved") if they're passed
# explicitly — which breaks `auth` out of the box. Refresh tokens still work:
# MSAL always requests offline_access for us.
SCOPES = [
    "User.Read",
    "Files.ReadWrite.All",
    "Mail.Read",
    "Mail.Send",
    "Calendars.ReadWrite",
    "ChannelMessage.Send",
    "Chat.Read",
]


def _token_dir():
    """Directory holding the cached OAuth token, under the resolved workspace.

    MS365_STATE_DIR is an explicit override. Otherwise default through the
    repo's resolve_workspace() (repo root and the resolved workspace differ on
    some hosts, so `os.getcwd()/state` would cache the token in the wrong tree);
    fall back to `os.getcwd()/state` only if that resolver is unavailable.
    """
    base = os.environ.get("MS365_STATE_DIR")
    if not base:
        try:
            repo_src = str(Path(__file__).resolve().parents[3] / "src")
            if repo_src not in sys.path:
                sys.path.insert(0, repo_src)
            from workspace_default import resolve_workspace  # noqa: E402
            base = os.path.join(str(resolve_workspace(migrate=False)), "state")
        except Exception as e:
            # FAIL CLOSED. Never silently fall back to os.getcwd()/state — that
            # would strand the OAuth refresh token in a second, unpredictable
            # location depending on where the command was launched (qingyun CR
            # #2682). If the workspace can't be resolved, require an explicit
            # MS365_STATE_DIR rather than inventing a credential path.
            raise SystemExit(
                f"ms365: cannot resolve the workspace for the token cache "
                f"({type(e).__name__}: {e}). Set MS365_STATE_DIR to an explicit "
                f"owner-only directory and retry."
            )
    return os.path.join(base, "ms365-token")


# The cached OAuth token grants Files.ReadWrite / Mail.Send / Calendars.ReadWrite
# (and Teams) — a long-lived, high-value credential. python-o365's
# FileSystemTokenBackend creates missing parent dirs with no mode (0755 under the
# usual 022 umask) and writes the token file with open("w") (0644), i.e.
# world-readable. The helpers below force owner-only perms (qingyun CR #2682).
_TOKEN_FILENAME = "o365_token.txt"


def _secure_dir(path):
    """Create `path` and force it to owner-only 0700, regardless of umask or a
    pre-existing looser mode."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _restrict_token_file(token_dir, filename=_TOKEN_FILENAME):
    """Force the cached token file to owner-only 0600 (the backend writes 0644)."""
    p = os.path.join(token_dir, filename)
    try:
        if os.path.exists(p):
            os.chmod(p, 0o600)
    except OSError:
        pass


def _owner_only_backend(FileSystemTokenBackend, token_dir, filename=_TOKEN_FILENAME):
    """A FileSystemTokenBackend that re-applies 0600 to the token file after
    EVERY save/refresh, so a token written on the initial consent or on a later
    silent refresh never lands world-readable."""
    class _OwnerOnlyTokenBackend(FileSystemTokenBackend):
        def save_token(self, *args, **kwargs):
            result = super().save_token(*args, **kwargs)
            _restrict_token_file(token_dir, filename)
            return result
    return _OwnerOnlyTokenBackend(token_path=token_dir, token_filename=filename)


def _require_credentials():
    """Return (client_id, client_secret, tenant_id) or exit if any is unset."""
    missing = [
        name
        for name in ("MS365_CLIENT_ID", "MS365_CLIENT_SECRET", "MS365_TENANT_ID")
        if not os.environ.get(name)
    ]
    if missing:
        sys.stderr.write(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nPopulate them from the Sutando vault before running this command.\n"
        )
        sys.exit(2)
    return (
        os.environ["MS365_CLIENT_ID"],
        os.environ["MS365_CLIENT_SECRET"],
        os.environ["MS365_TENANT_ID"],
    )


def _build_account():  # pragma: no cover  (live Graph calls; not unit-testable)
    """Construct an O365 Account with a filesystem-backed token cache."""
    Account, FileSystemTokenBackend = _require_o365()
    client_id, client_secret, tenant_id = _require_credentials()

    token_dir = _secure_dir(_token_dir())
    token_backend = _owner_only_backend(FileSystemTokenBackend, token_dir)

    account = Account(
        credentials=(client_id, client_secret),
        auth_flow_type="authorization",  # confidential client (id + secret)
        tenant_id=tenant_id,
        token_backend=token_backend,
    )
    return account


def _ensure_authenticated(account):
    """Exit with guidance if there is no valid cached token yet."""
    if not account.is_authenticated:
        sys.stderr.write(
            "Not authenticated. Run:\n    python3 scripts/ms365.py auth\n"
        )
        sys.exit(3)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------
def cmd_auth(_args):  # pragma: no cover  (live Graph calls; not unit-testable)
    account = _build_account()
    # Runs the console consent flow: prints an authorization URL and prompts
    # for the redirected URL, then caches the token. Returns True on success.
    # O365 >= 2.1 uses `requested_scopes=`; older 2.0.x used `scopes=`.
    try:
        result = account.authenticate(requested_scopes=SCOPES)
    except TypeError:
        result = account.authenticate(scopes=SCOPES)
    if result:
        print("Authenticated. Token cached under: " + _token_dir())
        return 0
    sys.stderr.write("Authentication failed.\n")
    return 1


def cmd_onedrive_list(args):  # pragma: no cover  (live Graph calls; not unit-testable)
    account = _build_account()
    _ensure_authenticated(account)
    storage = account.storage()
    drive = storage.get_default_drive()
    if args.folder:
        folder = drive.get_item_by_path(args.folder)
    else:
        folder = drive.get_root_folder()
    for item in folder.get_items():
        kind = "dir " if item.is_folder else "file"
        print(f"{kind}\t{item.name}")
    return 0


def cmd_onedrive_get(args):  # pragma: no cover  (live Graph calls; not unit-testable)
    account = _build_account()
    _ensure_authenticated(account)
    storage = account.storage()
    drive = storage.get_default_drive()
    item = drive.get_item_by_path(args.path)
    if item is None:
        sys.stderr.write(f"Not found: {args.path}\n")
        return 1
    dest = os.path.abspath(args.dest)
    dest_dir = os.path.dirname(dest) or "."
    # DriveItem.download(to_path=<dir>, name=<filename>) writes into a directory.
    item.download(to_path=dest_dir, name=os.path.basename(dest))
    print(f"Downloaded {args.path} -> {dest}")
    return 0


def cmd_outlook_list(args):  # pragma: no cover  (live Graph calls; not unit-testable)
    account = _build_account()
    _ensure_authenticated(account)
    mailbox = account.mailbox()
    inbox = mailbox.inbox_folder()
    for msg in inbox.get_messages(limit=args.n):
        sender = getattr(msg.sender, "address", "") if msg.sender else ""
        print(f"{msg.received}\t{sender}\t{msg.subject}")
    return 0


def cmd_outlook_send(args):  # pragma: no cover  (live Graph calls; not unit-testable)
    account = _build_account()
    _ensure_authenticated(account)
    mailbox = account.mailbox()
    message = mailbox.new_message()
    for recipient in args.to.split(","):
        recipient = recipient.strip()
        if recipient:
            message.to.add(recipient)
    message.subject = args.subject
    message.body = args.body
    message.send()
    print(f"Sent to {args.to}: {args.subject}")
    return 0


def cmd_calendar_list(args):  # pragma: no cover  (live Graph calls; not unit-testable)
    account = _build_account()
    _ensure_authenticated(account)
    schedule = account.schedule()
    calendar = schedule.get_default_calendar()

    start = datetime.now()
    end = start + timedelta(days=args.days)
    # calendarView expansion: include_recurring=True expands recurring series
    # within the window defined by start_recurring/end_recurring (ISO strings).
    events = calendar.get_events(
        limit=None,
        include_recurring=True,
        start_recurring=start.isoformat(),
        end_recurring=end.isoformat(),
    )
    for event in events:
        print(f"{event.start}\t{event.end}\t{event.subject}")
    return 0


def cmd_teams_post(args):  # pragma: no cover  (live Graph calls; not unit-testable)
    account = _build_account()
    _ensure_authenticated(account)
    teams = account.teams()

    team = None
    for t in teams.get_my_teams():
        if t.display_name == args.team:
            team = t
            break
    if team is None:
        sys.stderr.write(f"Team not found: {args.team}\n")
        return 1

    channel = None
    for ch in team.get_channels():
        if ch.display_name == args.channel:
            channel = ch
            break
    if channel is None:
        sys.stderr.write(f"Channel not found: {args.channel}\n")
        return 1

    # Channel.send_message(content=None, content_type='text') in python-o365.
    channel.send_message(content=args.message, content_type="text")
    print(f"Posted to {args.team}/{args.channel}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="ms365.py",
        description="Microsoft 365 connectivity via python-o365 (Microsoft Graph).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="Run the OAuth consent flow and cache the token.")
    p_auth.set_defaults(func=cmd_auth)

    p_od_list = sub.add_parser("onedrive-list", help="List a OneDrive folder.")
    p_od_list.add_argument("folder", nargs="?", default=None, help="Folder path (default: root).")
    p_od_list.set_defaults(func=cmd_onedrive_list)

    p_od_get = sub.add_parser("onedrive-get", help="Download a OneDrive file.")
    p_od_get.add_argument("path", help="OneDrive file path, e.g. /Reports/x.pdf")
    p_od_get.add_argument("dest", help="Local destination path.")
    p_od_get.set_defaults(func=cmd_onedrive_get)

    p_mail_list = sub.add_parser("outlook-list", help="List recent inbox messages.")
    p_mail_list.add_argument("--n", type=int, default=10, help="Number of messages (default: 10).")
    p_mail_list.set_defaults(func=cmd_outlook_list)

    p_mail_send = sub.add_parser("outlook-send", help="Send an email.")
    p_mail_send.add_argument("--to", required=True, help="Recipient(s), comma-separated.")
    p_mail_send.add_argument("--subject", required=True, help="Email subject.")
    p_mail_send.add_argument("--body", required=True, help="Email body.")
    p_mail_send.set_defaults(func=cmd_outlook_send)

    p_cal = sub.add_parser("calendar-list", help="List upcoming calendar events.")
    p_cal.add_argument("--days", type=int, default=7, help="Window in days (default: 7).")
    p_cal.set_defaults(func=cmd_calendar_list)

    p_teams = sub.add_parser("teams-post", help="Post a message to a Teams channel.")
    p_teams.add_argument("--team", required=True, help="Team display name.")
    p_teams.add_argument("--channel", required=True, help="Channel display name.")
    p_teams.add_argument("--message", required=True, help="Message text.")
    p_teams.set_defaults(func=cmd_teams_post)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
