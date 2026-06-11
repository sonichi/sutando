"""Sutando platform kernel — the seams (config · obs · meter · ports · bus).

Python twin tree of the TS kernel. Imported as a package so intra-kernel imports
are relative and collision-safe (no bare ``import ids`` on a shared sys.path):

    from kernel.obs import emit
    from kernel.meter import record
    from kernel.config import load_observability_config

Requires the repo ``src/`` on ``sys.path`` (the same precondition that makes the
``kernel`` package importable also resolves the flat ``workspace_default`` twin).
"""

from __future__ import annotations
