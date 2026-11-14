"""Read-only canonical collection report from digest-verified persisted bundles."""
import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from collection_claims import summarize, summarize_population, validate_claims


def load_population(paths):
    """Bound optional reporting independently of proposal acceptance policy."""
    if not isinstance(paths, list) or not 0 < len(paths) <= 1000:
        raise ValueError('bounded receipt population required')
    receipts, total = [], 0
    for value in paths:
        path = Path(value)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 4000000:
                raise ValueError('bounded ordinary receipt required')
            data = stream.read(4000001)
            after = os.fstat(stream.fileno())
            current = path.lstat()
            if len(data) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('receipt changed during reporting read')
        total += len(data)
        if len(data) > 4000000 or total > 16000000:
            raise ValueError('receipt reporting budget exceeded')
        digest = hashlib.sha256(data).hexdigest()
        if path.name != digest + '.json':
            raise ValueError('receipt reporting digest mismatch')
        receipts.append((json.loads(data), digest))
    population = summarize_population(receipts)
    if len(json.dumps(population)) > 128000:
        raise ValueError('receipt population output budget exceeded')
    return population


def load_summaries(paths):
    rows = []
    for value in paths:
        path = Path(value)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if path.name != digest + ".json":
            raise ValueError("receipt digest mismatch")
        rows.append(summarize(json.loads(data), digest))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("receipts", nargs="+")
    parser.add_argument("--claims")
    args = parser.parse_args()
    rows = load_summaries(args.receipts)
    if args.claims:
        validate_claims(rows, json.loads(Path(args.claims).read_text()))
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
