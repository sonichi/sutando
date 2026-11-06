---
name: phone-conversation
description: "Make conversational phone calls and join Zoom meetings via Twilio + Gemini. Multi-turn AI conversations on the phone on behalf of the user."
---

# Phone Conversation

Make outbound phone calls and join Zoom meetings where Sutando has a real multi-turn conversation, powered by Gemini.

## When to Use

- "Call +14155551234 and ask if they're available for dinner"
- "Call the restaurant and make a reservation for 7pm"
- "Call my dentist and reschedule my appointment"
- "Phone the landlord and ask about the maintenance request"
- "Join my Zoom meeting 1234567890"
- "Dial into the meeting and take notes"
- Any time you need Sutando to have a phone conversation or join a meeting on your behalf

## Setup from the chat (only sign-up and the card are manual)

The owner signs up at twilio.com and adds a payment method; everything after that is
an API call, so do it for them with `scripts/twilio-setup.py`:

1. Ask for the **Account SID** and **Auth Token** (Twilio console → Account info) and
   store them: `vault set TWILIO_ACCOUNT_SID …`, `vault set TWILIO_AUTH_TOKEN …`. That is
   enough for the script: it, `startup.sh`'s phone gate and the phone server itself all
   resolve the environment (`.env` included) first and the vault when it is empty, so
   nothing is copied into `.env`. The phone server and its tunnel stay OFF until step 4
   has written `TWILIO_PHONE_NUMBER`: the gate asks for all three (SID, token, number),
   because the server exits without the number and a tunnel to it would be public and dead.
2. `python3 skills/phone-conversation/scripts/twilio-setup.py status` — account type
   (a **Trial** account cannot buy a number or call unverified numbers; say so and
   point at https://console.twilio.com/billing), numbers owned, webhook drift.
3. `… numbers --country US --area 415` — list voice-capable numbers; let the owner pick.
4. `… buy +14155551234` — buys it, points its voice webhook at this machine (the running
   server's tunnel from `GET localhost:3100/health`; else `TWILIO_WEBHOOK_URL`, an
   operator-set fixed external URL; else `WEBHOOK_BASE_URL`), and writes
   `TWILIO_PHONE_NUMBER` + `TWILIO_WEBHOOK_PUSHED` (the base it pushed) into `<repo>/.env`
   in place. It never writes `TWILIO_WEBHOOK_URL`: the server binds that key instead of
   starting its own tunnel, so a moving ngrok URL recorded there goes stale on the next
   restart. Then restart the phone server (`startup.sh`).
5. `… set-webhook [BASE]` — re-point an owned number after the tunnel URL moved (`status`
   says "last pushed to Twilio … run set-webhook" when it did). With `TWILIO_AUTO_WEBHOOK=1`
   in `.env` the server pushes the tunnel it just bound on every start, so a restart with
   an unreserved ngrok needs no hand step. That push gives api.twilio.com 10 s per call and
   logs a skip (`webhook sync skipped … run twilio-setup.py set-webhook`) instead of holding
   the start; `set-webhook` is the retry.
6. Confirm: `curl localhost:3100/health` shows the tunnel; `status` shows no drift.

Twilio's refusals are printed verbatim (error code + message + more_info link); relay
them as they are, the fix is on Twilio's side (upgrade, verify a number, billing).

## How It Works

Uses Twilio Media Streams for real-time bidirectional audio, piped to Gemini Live for natural conversation. The caller can interrupt mid-sentence — no waiting for the AI to finish speaking.

1. Call connects → Twilio opens a WebSocket audio stream
2. Audio flows bidirectionally between the caller and Gemini Live
3. Gemini responds in real time, interruptible at any point
4. Full transcript is saved when the call ends
