"""Adapter CLI: authenticated GET only, delegating cloud auth and HTTP mechanics."""
import argparse
import importlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('person_key')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', args.person_key):
        parser.error('canonical person key required')
    sys.path.insert(0, str(Path(args.engine) / 'src'))
    cloud = importlib.import_module('cloud_auth')
    try:
        base, token = cloud.read_cloud_auth(Path(args.workspace))
        if not base or not token:
            raise ValueError('not signed in')
        body = cloud.cloud_request(base, token, 'GET', '/api/people/' + quote(args.person_key, safe=''), timeout=10)
        print(json.dumps({'ok': True, 'body': body}))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
