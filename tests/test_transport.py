"""Tests for RtpTransport STUN handling."""

import socket
from struct import pack, unpack

from aiortp.transport import _STUN_MAGIC, _stun_binding_response

_TXN_ID = bytes(range(12))


def _binding_request() -> bytes:
    return pack("!HHI", 0x0001, 0, _STUN_MAGIC) + _TXN_ID


def test_binding_response_ipv4() -> None:
    resp = _stun_binding_response(_binding_request(), ("192.0.2.7", 4242))

    msg_type, _length, magic = unpack("!HHI", resp[:8])
    assert msg_type == 0x0101
    assert magic == _STUN_MAGIC
    assert resp[8:20] == _TXN_ID

    attr_type, attr_len, _reserved, family, xport = unpack("!HHBBH", resp[20:28])
    assert attr_type == 0x0020  # XOR-MAPPED-ADDRESS
    assert attr_len == 8
    assert family == 0x01
    assert xport ^ (_STUN_MAGIC >> 16) == 4242
    addr = bytes(b ^ k for b, k in zip(resp[28:32], pack("!I", _STUN_MAGIC), strict=True))
    assert socket.inet_ntoa(addr) == "192.0.2.7"


def test_binding_response_ipv6_zone_id_stripped() -> None:
    """Link-local sources carry a zone-id that is not part of the wire format."""
    plain = _stun_binding_response(_binding_request(), ("fe80::1", 4242))
    zoned = _stun_binding_response(_binding_request(), ("fe80::1%eth0", 4242))
    assert zoned == plain


def test_binding_response_ipv6() -> None:
    resp = _stun_binding_response(_binding_request(), ("2001:db8::1", 4242))

    attr_type, attr_len, _reserved, family, xport = unpack("!HHBBH", resp[20:28])
    assert attr_type == 0x0020
    assert attr_len == 20
    assert family == 0x02
    assert xport ^ (_STUN_MAGIC >> 16) == 4242
    xor_key = pack("!I", _STUN_MAGIC) + _TXN_ID
    addr = bytes(b ^ k for b, k in zip(resp[28:44], xor_key, strict=True))
    assert socket.inet_ntop(socket.AF_INET6, addr) == "2001:db8::1"
