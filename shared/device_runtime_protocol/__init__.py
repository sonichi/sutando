"""Device Runtime Protocol — the shared schema for act() and present().

Owned by NEITHER transport (owner ruling 2026-08-12): runtime-api is the
per-device Runtime Host, the relay is the cross-device transport, and every
future runtime (iPhone, Android, cloud browser) consumes THIS package rather
than any server's internal model. act() and present() are symmetric in
protocol status — identity, task/trace linkage, capability routing, policy,
preconditions, lifecycle, durable result, audit — but deliberately NOT one
payload schema.
"""

PROTOCOL_VERSION = "1"

from .errors import (  # noqa: F401,E402
    ERROR_CODES,
    ProtocolFault,
    fault,
)
from .capabilities import (  # noqa: F401,E402
    CapabilityState,
    resolve_capability_state,
    validate_capability_name,
)
from .action import (  # noqa: F401,E402
    SUPPORTED_PROTOCOL_VERSIONS,
    ActionEnvelope,
    ActionResult,
    action_digest,
    canonical_action,
    effects_digest,
)
from .canonical import (  # noqa: F401,E402
    canonical_digest,
    canonical_json,
)
from .present import (  # noqa: F401,E402
    DISPOSITIONS,
    EXPERIENCE_INTENTS,
    PRESENT_OPERATIONS,
    ExperienceResponseEnvelope,
    PresentEnvelope,
    PresentResult,
    build_approval_content,
)
