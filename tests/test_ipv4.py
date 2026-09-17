from __future__ import annotations

import socket

from nse_alert.ipv4 import force_ipv4, restore_getaddrinfo


def test_force_ipv4_uses_af_inet() -> None:
    restore_getaddrinfo()
    orig = socket.getaddrinfo
    seen: list[int] = []

    def spy(
        host: object,
        port: object,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list:
        seen.append(family)
        return orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = spy  # type: ignore[assignment]
    try:
        force_ipv4()
        socket.getaddrinfo("127.0.0.1", 80, type=socket.SOCK_STREAM)
        assert seen
        assert all(f == socket.AF_INET for f in seen)
    finally:
        restore_getaddrinfo()
