"""Whether an outgoing chat post should render link preview cards.

Every outbound adapter reads the threshold from here: a copy per bridge
drifts, and the copy nobody remembers is the one that ships the wrong default.
"""

from __future__ import annotations

import re

_LINK = re.compile(r"https?://")

# One link IS the post's value (an article, a PR, a dashboard); two or more
# turn it into a wall of cards. Measured 110 single-link vs 25 multi-link.
DENSE_LINK_COUNT = 2


def should_unfurl(body: str) -> bool:
    """True when ``body`` carries fewer than ``DENSE_LINK_COUNT`` links."""
    return len(_LINK.findall(body or "")) < DENSE_LINK_COUNT
