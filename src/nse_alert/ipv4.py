from __future__ import annotations

import logging
import socket
from collections.abc import Callable

logger = logging.getLogger(__name__)

_installed = False
_orig_getaddrinfo: Callable[..., list] | None = None


def force_ipv4() -> None:
    """Make DNS lookups return IPv4 only (AWS dual-stack prefers IPv6).

    Kite order placement checks the *egress* IP against the developer-console
    whitelist. On dual-stack VMs, ``api.kite.trade`` often goes out as IPv6
    even when only the Elastic IPv4 is whitelisted.
    """
    global _installed, _orig_getaddrinfo
    if _installed:
        return
    _orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_only(
        host: object,
        port: object,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list:
        if family in (0, socket.AF_UNSPEC):
            family = socket.AF_INET
        assert _orig_getaddrinfo is not None
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = _ipv4_only  # type: ignore[assignment]
    _installed = True
    logger.info("KITE_FORCE_IPV4: DNS/API calls will use IPv4 only")


def restore_getaddrinfo() -> None:
    """Test helper — undo :func:`force_ipv4`."""
    global _installed, _orig_getaddrinfo
    if _orig_getaddrinfo is not None:
        socket.getaddrinfo = _orig_getaddrinfo  # type: ignore[assignment]
    _installed = False
    _orig_getaddrinfo = None
