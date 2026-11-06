"""Validate an untrusted consumer proposal file against adapter-owned capabilities."""
import hashlib
import json
import os
import stat
from pathlib import Path
from pending_candidates import propose, validate_proposal


def read_json(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError('ordinary consumer output file required') from exc
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 256000:
            raise ValueError('bounded ordinary consumer output file required')
        raw = stream.read(256001)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        if len(raw) != before.st_size or len(raw) > 256000 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('consumer output changed during read')
    return json.loads(raw)


def review(path, receipt_paths, stores):
    data = read_json(path)
    if not isinstance(data, dict) or set(data) != {'schema', 'proposals'} or data['schema'] != 1 or not isinstance(data['proposals'], list) or len(data['proposals']) > 100:
        raise ValueError('explicit proposal return schema required')
    if not isinstance(stores, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in stores.items()):
        raise ValueError('adapter store inventory required')
    receipts = {hashlib.sha256(Path(p).read_bytes()).hexdigest(): Path(p) for p in receipt_paths}
    accepted, rejected = [], []
    for i, row in enumerate(data['proposals']):
        try:
            fields = {'person_key', 'scope', 'receipt_digest', 'text', 'references'}
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError('proposal fields must be exactly person_key,scope,receipt_digest,text,references')
            if row['person_key'] not in stores:
                raise ValueError('person_key outside adapter store inventory')
            if row['receipt_digest'] not in receipts:
                raise ValueError('receipt_digest outside retained adapter receipts')
            proposal = {k: v for k, v in row.items() if k != 'person_key'}
            proposal['store_identity'] = stores[row['person_key']]
            validate_proposal(receipts[row['receipt_digest']], proposal, proposal['store_identity'])
            accepted.append({'index': i, 'receipt_path': str(receipts[row['receipt_digest']]), 'proposal': proposal})
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({'index': i, 'reason': str(exc)})
    return accepted, rejected


def consume(path, directory, receipt_paths, stores):
    rows, rejected = review(path, receipt_paths, stores)
    accepted = []
    for row in rows:
        try:
            saved = propose(directory, row['receipt_path'], row['proposal'], row['proposal']['store_identity'])
            accepted.append(saved['candidate_id'])
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({'index': row['index'], 'reason': str(exc)})
    rejected.sort(key=lambda row: row['index'])
    return {'proposal_return': 'partial' if rejected else 'persisted', 'accepted_candidate_ids': accepted,
            'rejected': rejected, 'learning_outcome': 'unverified', 'document_writes': 0}
