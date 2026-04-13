#!/bin/bash
# POC: CodeQL #38 — incomplete URL substring sanitization
# Tests that hostname spoofing is prevented.

echo "=== URL substring check ==="
python3 -c "
from urllib.parse import urlparse

urls = [
    ('https://my-machine.tail1234.ts.net/ws', True, 'legit Tailscale Funnel'),
    ('https://evil-ts.net.attacker.com/ws', False, 'hostname spoofing'),
    ('https://not-tailscale.com/ts.net/ws', False, 'ts.net in path, not host'),
    ('https://ts.net.evil.com/ws', False, 'ts.net as subdomain of evil.com'),
]

print('Before fix (substring check):')
for url, expected, desc in urls:
    result = 'ts.net' in url
    status = 'PASS' if result == expected else 'FAIL'
    print(f'  {status} | {desc}: in={result} expected={expected} | {url}')

print()
print('After fix (hostname endswith check):')
for url, expected, desc in urls:
    host = urlparse(url).hostname or ''
    result = host.endswith('.ts.net')
    status = 'PASS' if result == expected else 'FAIL'
    print(f'  {status} | {desc}: endswith={result} expected={expected} | {url}')
"
