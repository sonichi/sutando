"""Source adapter contract.

A source module exposes a single function:

    def fetch(config: dict) -> list[dict]:
        '''Return candidate items (events, PRs, alerts, ...) as plain dicts.'''

The runner then applies `match:` filters from the ping yaml on each item.
Item dicts must include enough fields to render the ping's body_template
plus a `dedup_key_suffix` for stable per-item dedup.
"""
