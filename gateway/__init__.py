# SPDX-License-Identifier: GPL-2.0-or-later
"""Space Arena transport spike: TLS + Bearer admission gate (library only)."""

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
