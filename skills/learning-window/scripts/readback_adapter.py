"""Read-only injected per-person transport; no document effects or acknowledgments."""
import datetime
import json
import math
import subprocess
import tempfile
import time
from document_effect import digest


def parse_person(response, person_key, store_identity, observed_at):
    if not isinstance(response, dict) or response.get('ok') is not True or response.get('truncated'):
        raise ValueError('complete successful per-person response required')
    body = response.get('body')
    person = body.get('person') if isinstance(body, dict) else None
    if not isinstance(person, dict) or body.get('truncated') or person.get('truncated'):
        raise ValueError('per-person envelope required; list responses are not documents')
    if person.get('id') != store_identity or person.get('slug') != person_key:
        raise ValueError('cloud person differs from adapter inventory')
    document = person.get('doc')
    if not isinstance(document, str):
        raise ValueError('explicit full document required; omitted or null is unknown')
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at):
        raise ValueError('finite adapter observation clock required')
    return {'store_identity': store_identity, 'person_key': person_key, 'document': document,
            'document_sha256': digest(document),
            'observed_at': datetime.datetime.fromtimestamp(observed_at, datetime.timezone.utc).isoformat(),
            'semantic_accuracy': 'unknown', 'authority': 'unknown'}


def capture_person(config, person_key):
    argv, stores = config.get('document_readback_argv'), config.get('proposal_stores')
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise ValueError('explicit adapter read-only transport argument vector required')
    if not isinstance(stores, dict) or not isinstance(person_key, str) or person_key not in stores or not isinstance(stores[person_key], str) or not stores[person_key]:
        raise ValueError('person outside adapter inventory')
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv + [person_key], stdout=output, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode != 0:
            raise ValueError('read-only transport failed; document unknown')
        size = output.tell()
        if size > 2000000:
            raise ValueError('response exceeds readback budget; document unknown')
        output.seek(0)
        raw = output.read(size)
        if len(raw) != size:
            raise ValueError('incomplete transport response')
    return parse_person(json.loads(raw), person_key, stores[person_key], time.time())


def capture_inventory(config):
    observations = {}
    for key in sorted(config['proposal_stores']):
        try:
            row = capture_person(config, key)
            row['document_codepoints'] = len(row.pop('document'))
            observations[key] = {'readback': 'verified', **row}
        except Exception as exc:
            observations[key] = {'readback': 'unknown', 'error': type(exc).__name__}
    return observations
