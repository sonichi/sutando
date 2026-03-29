# Phone Access Control — Manual Test

## Setup

- `OWNER_NUMBER` in `.env` — the owner's phone number
- `VERIFIED_CALLERS` in `.env` — comma-separated verified numbers
- Unverified: any number not in either list

## Owner (calls from OWNER_NUMBER)

- [x] Conversation — "Hi, how are you?"
- [x] hang_up — "Goodbye"
- [x] volume — "Set volume to 50%"
- [x] brightness — "Set brightness to 80%"
- [x] get_current_time — "What time is it?"
- [x] lookup_meeting_id — "What's the Zoom meeting ID?"
- [x] work — "Check my email"
- [x] get_task_status — "Are you still working on that?"
- [x] scroll — "Scroll down"
- [x] switch_tab — "Switch to the GitHub tab"
- [x] open_url — "Open google.com"
- [x] switch_app — "Switch to Slack"
- [x] capture_screen — "What's on my screen?"
- [x] type_text — "Type hello world"
- [x] clipboard — "What's in my clipboard?"
- [x] cancel_task — "Cancel that task"
- [x] toggle_tasks — "Show the task list"
- [x] summon — "Summon to Zoom"
- [x] join_zoom — "Join the Zoom meeting"
- [x] join_gmeet — "Join the Google Meet"
- [x] call_contact — "Call Bob"

## Verified caller (calls from VERIFIED_CALLERS, not owner)

- [x] Conversation — "Hi, how are you?"
- [x] hang_up — "Goodbye"
- [x] volume — "Set volume to 50%"
- [x] brightness — "Set brightness to 80%"
- [x] get_current_time — "What time is it?"
- [x] lookup_meeting_id — "What's the Zoom meeting ID?"
- [x] work — should be blocked
- [x] get_task_status — should be blocked
- [x] scroll — should be blocked
- [x] switch_tab — should be blocked
- [x] open_url — should be blocked
- [x] switch_app — should be blocked
- [x] capture_screen — should be blocked
- [x] type_text — should be blocked
- [x] clipboard — should be blocked
- [x] cancel_task — should be blocked
- [x] toggle_tasks — should be blocked
- [x] summon — should be blocked
- [x] join_zoom — should be blocked
- [x] join_gmeet — should be blocked
- [x] call_contact — should be blocked

## Unverified caller (any other number)

- [x] Conversation — "Hi, how are you?"
- [x] hang_up — "Goodbye"
- [x] volume — "Set volume to 50%"
- [x] brightness — "Set brightness to 80%"
- [x] get_current_time — "What time is it?"
- [x] lookup_meeting_id — should be blocked
- [x] work — should be blocked
- [x] scroll — should be blocked
- [x] summon — should be blocked
- [x] call_contact — should be blocked

## Tested by

- @susanliu_ — 2026-03-29
