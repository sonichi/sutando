---
name: ms365-connect
description: >-
  Connect to Microsoft 365 (OneDrive, Outlook mail, Calendar, Teams) via
  Microsoft Graph using the open-source python-o365 (O365) library. Use when
  the user wants to list/download OneDrive files, read or send Outlook email,
  list calendar events, or post a message to a Teams channel from a Sutando
  workflow.
---

# ms365-connect

Microsoft 365 connectivity for Sutando, built on the Apache-2.0
[`python-o365`](https://github.com/O365/python-o365) library (`O365` on PyPI),
which is a thin wrapper over Microsoft Graph.

## What it does

All operations go through Microsoft Graph via python-o365:

- **OneDrive** — list a folder's contents (`onedrive-list`) and download a file
  to a local path (`onedrive-get`).
- **Outlook mail** — list recent inbox messages (`outlook-list`) and send an
  email (`outlook-send`).
- **Calendar** — list upcoming events over a window of days (`calendar-list`).
- **Teams** — post a message to a channel in a team (`teams-post`).

Authentication is a one-time delegated OAuth2 consent flow (`auth`); the refresh
token is cached on disk so subsequent commands run non-interactively.

## Usage

Run the CLI with `python3 scripts/ms365.py <subcommand>`:

```bash
# One-time: run the OAuth consent flow and cache the token
python3 scripts/ms365.py auth

# OneDrive
python3 scripts/ms365.py onedrive-list                 # list root
python3 scripts/ms365.py onedrive-list "/Projects/Q3"  # list a folder by path
python3 scripts/ms365.py onedrive-get "/Reports/x.pdf" ./x.pdf

# Outlook mail
python3 scripts/ms365.py outlook-list --n 10
python3 scripts/ms365.py outlook-send --to a@b.com --subject "Hi" --body "Hello"

# Calendar
python3 scripts/ms365.py calendar-list --days 7

# Teams
python3 scripts/ms365.py teams-post --team "Engineering" --channel "General" \
  --message "Deploy is green"
```

Every subcommand except `auth` requires that `auth` has already been run and a
cached token exists.

## Configuration

Secrets are read from the environment (populate them from the Sutando vault;
**never hardcode**):

| Variable | Purpose |
|----------|---------|
| `MS365_CLIENT_ID` | Azure AD application (client) ID |
| `MS365_CLIENT_SECRET` | Azure AD client secret value |
| `MS365_TENANT_ID` | Directory (tenant) ID, or `common` / `organizations` |
| `MS365_AUTH_FLOW` | `credentials` (default; confidential client, id+secret) or `public` (native client, id only — required when the app's redirect is registered under "Mobile and desktop applications"; symptom of the mismatch: `AADSTS700025` on token exchange). In `public` flow `MS365_CLIENT_SECRET` is not required. |

If any is unset, the CLI exits with a clear message naming the missing variable.

**Token cache:** the OAuth token is stored under the workspace state dir at
`state/ms365-token/o365_token.txt` via python-o365's `FileSystemTokenBackend`.
Override the base with `MS365_STATE_DIR` if the workspace state dir is elsewhere.
Treat this file as a live credential (owner-only permissions).

## Azure AD app registration (one-time, owner does this)

Delegated Graph access requires an app registration. The owner performs this
once in the Azure Portal:

1. **Register the app** — Azure Portal → **Microsoft Entra ID** (Azure AD) →
   **App registrations** → **New registration**. Name it (e.g. `Sutando M365`).
   For "Supported account types", pick single-tenant unless multi-tenant is
   required. Copy the **Application (client) ID** and **Directory (tenant) ID**.

2. **Add a redirect URI** — in the app's **Authentication** blade →
   **Add a platform** → **Mobile and desktop applications**, add python-o365's
   default native-client redirect:
   `https://login.microsoftonline.com/common/oauth2/nativeclient`
   (A `http://localhost` redirect also works if you prefer a loopback flow.)

3. **Grant delegated Microsoft Graph scopes** — **API permissions** → **Add a
   permission** → **Microsoft Graph** → **Delegated permissions**, add:

   | Scope | Why |
   |-------|-----|
   | `offline_access` | Issue a refresh token so the token cache survives |
   | `User.Read` | Read the signed-in user's basic profile |
   | `Files.ReadWrite.All` | List and download (and write) OneDrive files |
   | `Mail.Read` | Read Outlook inbox messages |
   | `Mail.Send` | Send Outlook email as the user |
   | `Calendars.ReadWrite` | Read (and create) calendar events |
   | `Team.ReadBasic.All` | Look up the team by name (`GET /me/joinedTeams`) — required before posting |
   | `Channel.ReadBasic.All` | Look up the channel by name (`GET /teams/{id}/channels`) — required before posting |
   | `ChannelMessage.Send` | Post messages to Teams channels |
   | `Chat.Read` | Read Teams chat/channel context |

   > `teams-post` resolves the team and channel **by display name** before it
   > can send, so the two `*.ReadBasic.All` lookup scopes are mandatory — without
   > them the lookups return empty and the CLI reports `Team not found` even
   > though the team exists.

   Click **Grant admin consent** for the tenant if your org requires it.

4. **Create a client secret** — **Certificates & secrets** → **New client
   secret**. Copy the secret **Value** immediately (shown once).

5. **Store the 3 credentials in the vault, not in code** — put the
   Application (client) ID, the client secret Value, and the Directory (tenant)
   ID in the Sutando vault, exposed to this skill as `MS365_CLIENT_ID`,
   `MS365_CLIENT_SECRET`, and `MS365_TENANT_ID`.

Then run `python3 scripts/ms365.py auth` once to complete the consent flow and
cache the token.

## Requirements

Install the dependency: `pip install -r requirements.txt` (`O365`).
