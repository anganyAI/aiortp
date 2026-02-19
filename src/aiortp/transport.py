import asyncio
import logging
import socket
from collections.abc import Callable
from struct import pack, unpack_from
from typing import Optional

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

    Includes only the XOR-MAPPED-ADDRESS attribute so that the remote
    ICE agent can confirm connectivity.
    """
    # Transaction ID is bytes 8..20 of the request
    txn_id = request[8:20]

    # XOR-MAPPED-ADDRESS (type 0x0020)
    ip_int = int.from_bytes(socket.inet_aton(addr[0]), "big")
    xport = addr[1] ^ (_STUN_MAGIC >> 16)
    xaddr = ip_int ^ _STUN_MAGIC
    attr = pack("!HH BBH I", 0x0020, 8, 0, 0x01, xport, xaddr)

    # Header: type 0x0101 (Binding Success), length, magic, txn_id
    header = pack("!HHI", 0x0101, len(attr), _STUN_MAGIC) + txn_id
    return header + attr


class RtpTransport(asyncio.DatagramProtocol):
    def __init__(
        self,
        on_rtp: Callable[[bytes], None],
        on_rtcp: Callable[[bytes], None],
    ) -> None:
        self._on_rtp = on_rtp
        self._on_rtcp = on_rtcp
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._remote_addr: Optional[tuple[str, int]] = None
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
            self._on_rtcp(data)
        else:
            self._on_rtp(data)

    def error_received(self, exc: Exception) -> None:
        logger.warning("Transport error: %s", exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._closed.set()

    def send(self, data: bytes, addr: Optional[tuple[str, int]] = None) -> None:
        if self._transport is None:
            return
        target = addr or self._remote_addr
        if target is not None:
            self._transport.sendto(data, target)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
