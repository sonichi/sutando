"""Channel-neutral Delivery Core: contract types + protocols (contract),
the drain loop (core), and the default backend (backend_a). Seam design:
the master-room doc docs/delivery-core-seam-v0.2.md."""
from .contract import (BackendCapabilities, ClaimBackend, ClaimToken, Defer,
                       DeliveryAttempt,
                       CleanupReport, DeliveryOutcome, DeliveryProvider,
                       DeliveryReceipt, DrainReport, DrainResult, DrainStatus,
                       ProviderCapabilities, ProviderIndeterminate,
                       ProviderRefused, ProviderPermanentRefused, RecoverReport)
from .core import WITHDRAWN, DeliveryCore, RetryPolicy, idempotency_key
from .backend_a import (REFUSED_FOR_GOOD, DesignAClaimBackend, is_refused_for_good,
                        refused_for_good)
from .backend_c import DesignCClaimBackend  # noqa: F401

__all__ = [
    "BackendCapabilities", "ClaimBackend", "ClaimToken", "CleanupReport", "Defer",
    "DeliveryAttempt",
    "DeliveryOutcome", "DeliveryProvider", "DeliveryReceipt", "DrainReport",
    "DrainResult", "DrainStatus", "ProviderCapabilities",
    "ProviderIndeterminate", "ProviderRefused", "ProviderPermanentRefused", "RecoverReport",
    "DeliveryCore", "RetryPolicy", "WITHDRAWN", "idempotency_key", "DesignAClaimBackend",
    "DesignCClaimBackend", "REFUSED_FOR_GOOD", "is_refused_for_good", "refused_for_good",
]
