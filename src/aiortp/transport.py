import asyncio
import logging
import socket
from collections.abc import Callable
from struct import pack, unpack_from

from .packet import is_rtcp

logger = logging.getLogger(__name__)

# STUN magic cookie (RFC 5389)
_STUN_MAGIC = 0x2112A442


def _is_stun(data: bytes) -> bool:
    """Check if *data* looks like a STUN message (RFC 5389)."""
    return (
        len(data) >= 20
        and (data[0] & 0xC0) == 0  # first 2 bits must be 0
        and unpack_from("!I", data, 4)[0] == _STUN_MAGIC
    )


def _stun_binding_response(request: bytes, addr: tuple[str, int]) -> bytes:
    """Build a minimal STUN Binding Success Response (RFC 5389).

    Includes only the XOR-MAPPED-ADDRESS attribute (no MESSAGE-INTEGRITY),
    enough for simple connectivity probes but not a full ICE agent.
    """
    # Transaction ID is bytes 8..20 of the request
    txn_id = request[8:20]

    # XOR-MAPPED-ADDRESS (type 0x0020); IPv6 XORs with magic || txn_id
    if ":" in addr[0]:
        family = 0x02
        # Zone-id ("fe80::1%eth0") is not part of the wire format and
        # inet_pton rejects it on Linux
        raw = socket.inet_pton(socket.AF_INET6, addr[0].split("%")[0])
        xor_key = pack("!I", _STUN_MAGIC) + txn_id
    else:
        family = 0x01
        raw = socket.inet_aton(addr[0])
        xor_key = pack("!I", _STUN_MAGIC)
    xaddr = bytes(b ^ k for b, k in zip(raw, xor_key, strict=True))
    xport = addr[1] ^ (_STUN_MAGIC >> 16)
    attr = pack("!HHBBH", 0x0020, 4 + len(xaddr), 0, family, xport) + xaddr

    # Header: type 0x0101 (Binding Success), length, magic, txn_id
    header = pack("!HHI", 0x0101, len(attr), _STUN_MAGIC) + txn_id
    return header + attr


class RtpTransport(asyncio.DatagramProtocol):
    def __init__(
        self,
        on_rtp: Callable[[bytes, tuple[str, int]], None],
        on_rtcp: Callable[[bytes, tuple[str, int]], None],
    ) -> None:
        self._on_rtp = on_rtp
        self._on_rtcp = on_rtcp
        self._transport: asyncio.DatagramTransport | None = None
        self._remote_addr: tuple[str, int] | None = None
        self._closed = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if _is_stun(data):
            # Reply to STUN Binding Requests so ICE connectivity checks pass
            if self._transport is not None and len(data) >= 20 and data[1] == 0x01:
                resp = _stun_binding_response(data, addr)
                self._transport.sendto(resp, addr)
            return
        if is_rtcp(data):
            self._on_rtcp(data, addr)
        else:
            self._on_rtp(data, addr)

    def error_received(self, exc: Exception) -> None:
        logger.warning("Transport error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._closed.set()

    def send(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        if self._transport is None:
            return
        target = addr or self._remote_addr
        if target is not None:
            self._transport.sendto(data, target)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
