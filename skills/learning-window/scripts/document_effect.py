"""Mechanical document read-back verification, not semantic accuracy or authority."""
import datetime
import hashlib


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def compare_inventory(before, after):
    results = {}
    for key in sorted(set(before) | set(after)):
        first, last = before.get(key, {}), after.get(key, {})
        outcome = 'unknown'
        if first.get('readback') == last.get('readback') == 'verified':
            start = datetime.datetime.fromisoformat(first['observed_at']).timestamp()
            end = datetime.datetime.fromisoformat(last['observed_at']).timestamp()
            if (first['store_identity'] == last['store_identity'] and
                    first['person_key'] == last['person_key'] == key and 0 <= end - start <= 300):
                outcome = 'unchanged' if first['document_sha256'] == last['document_sha256'] else 'changed_unattributed'
        results[key] = {'physical_retention': outcome, 'semantic_accuracy': 'unknown',
                        'authority': 'unknown', 'learning_outcome': 'unverified'}
    return results
