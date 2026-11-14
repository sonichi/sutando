"""Typed collection summaries; no claims about dossier writes or learned facts."""
import datetime
from collections import Counter
from window_state import _timestamp


def summarize(receipt, digest):
    since, until = (_timestamp(receipt.get(k)) for k in ("since_ms", "until_ms"))
    rows = receipt.get("rooms")
    if since > until or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or isinstance(receipt.get("membership_count"), bool) or receipt.get("membership_count") != len(rows):
        raise ValueError("invalid receipt window or membership")
    ids = [row.get("room_id") for row in rows]
    if not receipt.get("scope") or len(set(ids)) != len(ids) or any(not isinstance(r, str) or not r for r in ids):
        raise ValueError("invalid scope or room identities")
    events, unread = set(), []
    for row in rows:
        if not isinstance(row.get("errors"), list) or row["errors"] or row.get("coverage") not in ("reached_cutoff", "server_no_cursor"):
            unread.append(row["room_id"])
        messages = row.get("messages")
        if not isinstance(messages, list):
            raise ValueError("missing message inventory")
        for event in messages:
            if not isinstance(event, dict) or not isinstance(event.get("event_id"), str) or not event["event_id"] or not since <= _timestamp(event.get("ts")) <= until:
                raise ValueError("invalid event identity or window")
            events.add((row["room_id"], event["event_id"]))
    def iso(value):
        return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc).isoformat(timespec="microseconds")
    return {"receipt_digest": digest, "scope": receipt["scope"], "since_ms": since, "until_ms": until,
            "since_iso": iso(since), "until_iso": iso(until), "membership_count": len(rows),
            "unique_events": len(events), "unread_rooms": unread, "complete_available_history": not unread,
            "learning_outcome": "unverified"}


def validate_claims(summaries, claims):
    if not isinstance(claims, list) or len(claims) != len(summaries):
        raise ValueError("exact receipt claim population required")
    expected = {row["receipt_digest"]: row for row in summaries}
    if len(expected) != len(summaries):
        raise ValueError("duplicate receipt")
    seen = set()
    for claim in claims:
        digest = claim.get("receipt_digest") if isinstance(claim, dict) else None
        if digest in seen or digest not in expected or claim != expected[digest]:
            raise ValueError("collection claim differs from persisted evidence")
        seen.add(digest)
    return True


def summarize_population(receipts):
    """Count retained rows and scoped event identities; never infer human identity."""
    scopes, digests, all_events = {}, set(), set()
    for receipt, digest in receipts:
        summary = summarize(receipt, digest)
        if digest in digests:
            raise ValueError('duplicate receipt population')
        digests.add(digest)
        scope = summary['scope']
        row = scopes.setdefault(scope, {'receipt_count': 0, 'message_rows': 0,
            'events': set(), 'sender_rows': Counter(), 'sender_events': {},
            'unknown_sender_rows': 0, 'incomplete_receipt_count': 0})
        row['receipt_count'] += 1
        row['incomplete_receipt_count'] += not summary['complete_available_history']
        for room in receipt['rooms']:
            for event in room['messages']:
                key = (room['room_id'], event['event_id'])
                row['message_rows'] += 1
                row['events'].add(key)
                all_events.add((scope, *key))
                sender = event.get('sender')
                if isinstance(sender, str) and sender:
                    row['sender_rows'][sender] += 1
                    row['sender_events'].setdefault(sender, set()).add(key)
                else:
                    row['unknown_sender_rows'] += 1
    return {'status': 'verified', 'receipt_count': len(digests),
        'message_rows': sum(row['message_rows'] for row in scopes.values()),
        'unique_scope_room_events': len(all_events),
        'by_scope': {scope: {'receipt_count': row['receipt_count'],
            'message_rows': row['message_rows'], 'unique_room_events': len(row['events']),
            'sender_message_rows': dict(sorted(row['sender_rows'].items())),
            'sender_unique_room_events': {sender: len(events) for sender, events in sorted(row['sender_events'].items())},
            'unknown_sender_rows': row['unknown_sender_rows'],
            'incomplete_receipt_count': row['incomplete_receipt_count']}
            for scope, row in sorted(scopes.items())},
        'population': 'all retained receipts, including overlapping and historical windows',
        'human_identity': 'unverified', 'semantic_accuracy': 'unknown', 'learning_outcome': 'unverified'}
