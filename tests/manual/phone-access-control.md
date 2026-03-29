# Phone Access Control — Manual Test

## Setup

- `OWNER_NUMBER` in `.env` — the owner's phone number
- `VERIFIED_CALLERS` in `.env` — comma-separated verified numbers
- Unverified: any number not in either list

## Owner (calls from OWNER_NUMBER)

- [x] Conversation — "Hi, how are you?"
- [ ] hang_up — "Goodbye"
- [ ] volume — "Set volume to 50%"
- [ ] brightness — "Set brightness to 80%"
- [ ] get_current_time — "What time is it?"
- [ ] lookup_meeting_id — "What's the Zoom meeting ID?"
- [x] work — "Check my email" (tested: delegated task to core)
- [ ] get_task_status — "Are you still working on that?"
- [ ] scroll — "Scroll down"
- [x] switch_tab — "Switch to the web UI tab" (tested: switched tab)
- [ ] open_url — "Open google.com"
- [ ] switch_app — "Switch to Slack"
- [x] capture_screen — "What's on my screen?"
- [ ] type_text — "Type hello world"
- [ ] clipboard — "What's in my clipboard?"
- [ ] cancel_task — "Cancel that task"
- [ ] toggle_tasks — "Show the task list"
- [x] summon — "Summon to Zoom" (tested: started Zoom with screen share)
- [ ] join_zoom — "Join the Zoom meeting"
- [ ] join_gmeet — "Join the Google Meet"
- [x] call_contact — "Call Xueqing" (tested: looked up contact, dialed)

## Verified caller (calls from VERIFIED_CALLERS, not owner)

- [x] Conversation — "Hi, how are you?" (tested via child call to Xueqing)
- [x] hang_up — "Goodbye" (tested: Xueqing ended call)
- [ ] volume — "Set volume to 50%" — should work
- [ ] brightness — "Set brightness to 80%" — should work
- [ ] get_current_time — "What time is it?" — should work
- [x] lookup_meeting_id — "What's the Zoom meeting ID?" (tested: returned ID + passcode)
- [ ] work — should be blocked
- [ ] get_task_status — should be blocked
- [ ] scroll — should be blocked
- [ ] switch_tab — should be blocked
- [ ] open_url — should be blocked
- [ ] switch_app — should be blocked
- [ ] capture_screen — should be blocked
- [ ] type_text — should be blocked
- [ ] clipboard — should be blocked
- [ ] cancel_task — should be blocked
- [ ] toggle_tasks — should be blocked
- [ ] summon — should be blocked
- [ ] join_zoom — should be blocked
- [ ] join_gmeet — should be blocked
- [ ] call_contact — should be blocked

## Unverified caller (any other number)

- [x] Conversation — "Hi, how are you?" (tested from 646 number)
- [ ] hang_up — "Goodbye"
- [x] volume — "Set volume to 50%" (tested: tool called, value was wrong before fix)
- [ ] brightness — "Set brightness to 80%"
- [ ] get_current_time — "What time is it?"
- [ ] lookup_meeting_id — should be blocked
- [ ] work — should be blocked
- [ ] scroll — should be blocked
- [ ] summon — should be blocked
- [ ] call_contact — should be blocked

## Tested by

- @susanliu_ — 2026-03-29 (partial — items marked [x] manually verified via phone calls)
