"""Channel-neutral Delivery Core: contract types + protocols (contract),
the drain loop (core), and the default backend (backend_a). Seam design:
the master-room doc docs/delivery-core-seam-v0.2.md."""
from .contract import (BackendCapabilities, ClaimBackend, ClaimToken,
                       DeliveryAttempt,
                       CleanupReport, DeliveryOutcome, DeliveryProvider,
                       DeliveryReceipt, DrainReport, DrainResult, DrainStatus,
                       ProviderCapabilities, ProviderIndeterminate,
                       ProviderRefused, RecoverReport)
from .core import DeliveryCore, RetryPolicy, idempotency_key
from .backend_a import DesignAClaimBackend
from .backend_c import DesignCClaimBackend  # noqa: F401

__all__ = [
    "BackendCapabilities", "ClaimBackend", "ClaimToken", "CleanupReport",
    "DeliveryAttempt",
    "DeliveryOutcome", "DeliveryProvider", "DeliveryReceipt", "DrainReport",
    "DrainResult", "DrainStatus", "ProviderCapabilities",
    "ProviderIndeterminate", "ProviderRefused", "RecoverReport",
    "DeliveryCore", "RetryPolicy", "idempotency_key", "DesignAClaimBackend",
    "DesignCClaimBackend",
]
