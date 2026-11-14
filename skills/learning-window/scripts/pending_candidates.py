"""Durable untrusted fact proposals; no document writes or success acknowledgments."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
from window_state import _atomic, _encode
from collection_claims import summarize


def validate_proposal(receipt_path, proposal, store_identity):
    if not isinstance(proposal, dict) or not isinstance(store_identity, str) or not store_identity or proposal.get('store_identity') != store_identity:
        raise ValueError('adapter store identity required')
    text = proposal.get('text')
    refs = proposal.get('references')
    if not isinstance(text, str) or not 0 < len(text) <= 4000 or not isinstance(refs, list) or not 0 < len(refs) <= 64:
        raise ValueError('bounded claim and references required')
    raw = Path(receipt_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if proposal.get('receipt_digest') != digest:
        raise ValueError('receipt digest differs')
    receipt = json.loads(raw)
    summary = summarize(receipt, digest)
    if not summary['complete_available_history'] or proposal.get('scope') != receipt['scope']:
        raise ValueError('scope or coverage differs')
    events = {}
    for room in receipt['rooms']:
        for event in room['messages']:
            key = (room['room_id'], event['event_id'])
            if key in events and events[key] != event:
                raise ValueError('conflicting event identity')
            events[key] = event
    normalized = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError('reference malformed')
        if any(not isinstance(ref.get(k), str) or not ref[k] for k in ('room_id', 'event_id')):
            raise ValueError('reference identity malformed')
        key = (ref.get('room_id'), ref.get('event_id'))
        excerpt = ref.get('excerpt')
        if key not in events or not isinstance(excerpt, str) or not excerpt or len(excerpt) > 4000 or not isinstance(events[key].get('body'), str) or excerpt not in events[key]['body']:
            raise ValueError('reference excerpt does not resolve')
        normalized.append({'room_id': key[0], 'event_id': key[1], 'excerpt': excerpt})
    normalized.sort(key=lambda r: (r['room_id'], r['event_id'], r['excerpt']))
    if len({json.dumps(r, sort_keys=True) for r in normalized}) != len(normalized):
        raise ValueError('duplicate reference')
    bound = {'store_identity': store_identity, 'scope': receipt['scope'],
             'text': text, 'references': normalized}
    candidate_id = hashlib.sha256(_encode(bound)).hexdigest()
    return bound, digest, candidate_id


def propose(directory, receipt_path, proposal, store_identity):
    bound, digest, candidate_id = validate_proposal(receipt_path, proposal, store_identity)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / '.candidate-lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'pending-candidates.json'
        state = json.loads(path.read_text()) if path.exists() else {'schema': 1, 'candidates': {}}
        if state.get('schema') != 1 or not isinstance(state.get('candidates'), dict):
            raise ValueError('candidate state malformed')
        row = {**bound, 'candidate_id': candidate_id, 'receipt_digests': [digest], 'status': 'pending',
               'document_effect': 'unverified', 'semantic_accuracy': 'unknown', 'authority': 'unknown'}
        prior = state['candidates'].get(candidate_id)
        if prior is not None:
            if not isinstance(prior, dict) or any(prior.get(k) != v for k, v in bound.items()) or prior.get('status') != 'pending':
                raise ValueError('candidate conflict')
            digests = prior.get('receipt_digests')
            if not isinstance(digests, list) or any(not isinstance(d, str) or len(d) != 64 for d in digests):
                raise ValueError('candidate provenance malformed')
            row['receipt_digests'] = sorted(set(digests + [digest]))
        state['candidates'][candidate_id] = row
        _atomic(path, _encode(state))
    return row
