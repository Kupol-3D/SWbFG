# SPDX-License-Identifier: GPL-2.0-or-later
"""Space Arena transport spike: TLS + Bearer admission gate, MP header checks
and a local UDP relay (library only).  `mp_protocol` and `relay` are imported
from their own modules; this package surface keeps only the gate's API."""

from .auth_gate import (
    GatewayError,
    IdentityRegistry,
    TlsAuthGate,
    TlsRequiredError,
    generate_token,
    load_server_tls_context,
    make_logger,
    redact,
)

__all__ = [
    "GatewayError",
    "IdentityRegistry",
    "TlsAuthGate",
    "TlsRequiredError",
    "generate_token",
    "load_server_tls_context",
    "make_logger",
    "redact",
]
