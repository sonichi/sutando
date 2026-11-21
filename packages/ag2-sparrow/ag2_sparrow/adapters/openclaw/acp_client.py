"""The shared ACP client (ag2_sparrow.acp_client), introducing itself as this adapter."""
from __future__ import annotations

from ... import acp_client as _shared
from ...acp_client import (AUTH_REQUIRED, CLIENT_CAPABILITIES, METHOD_NOT_FOUND, PROTOCOL_VERSION,  # noqa: F401
                           AcpError, AcpGone, AcpTimeout)

CLIENT_INFO = {"name": "ag2-sparrow-openclaw", "version": "0.1.0"}


class AcpClient(_shared.AcpClient):
    client_info = CLIENT_INFO
