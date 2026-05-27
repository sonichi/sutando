"""Action adapter contract.

An action module exposes:

    def send(ping, body: str) -> dict:
        '''Deliver the body via this channel.

        Return {"ok": True, "id": "<some-id>"} on success, or
        {"ok": False, "error": "<reason>"} on failure.
        '''

The runner calls the action picked by the channel router. Failures are
logged; the runner does NOT advance the dedup map on failure so the next
cron tick retries.
"""
