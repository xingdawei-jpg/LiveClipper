"""
Shared SSL context helper.
Secure by default (full certificate verification).
Set LIVECLIPPER_INSECURE_SSL=1 to disable verification
(corporate proxy SSL inspection, self-signed certs, etc).
"""

from __future__ import annotations

import os
import ssl

_INSECURE = os.environ.get("LIVECLIPPER_INSECURE_SSL", "").strip().lower()
_INSECURE = _INSECURE in ("1", "true", "yes", "on")


def create_ssl_context() -> ssl.SSLContext:
    """Create an SSL context with proper certificate verification.

    When LIVECLIPPER_INSECURE_SSL=1 is set, certificate verification
    and hostname checking are disabled.  This is useful for users behind
    corporate proxies that do SSL inspection.
    """
    ctx = ssl.create_default_context()
    if _INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
