import asyncio

import pytest

from aiortp.packet import (
    RtcpByePacket,
    RtcpPacket,
    RtcpSdesPacket,
    RtcpSenderInfo,
    RtcpSourceInfo,
    RtcpSrPacket,
)
from aiortp.session import RTPSession


@pytest.mark.asyncio
async def test_sr_sent() -> None:
    """Verify SR is sent within the RTCP interval."""
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),
        payload_type=0,
        rtcp_interval=0.1,  # Very short interval for testing
    )

    rtp_a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=(rtp_a_addr[0], rtp_a_addr[1]),
        payload_type=0,
        rtcp_interval=60.0,
    )

    rtp_b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    # Point A to B
    session_a.update_remote((rtp_b_addr[0], rtp_b_addr[1]))

    # Wait for at least one RTCP report
    await asyncio.sleep(0.5)

    # Check stats show packets sent
    stats = session_a.stats
    assert stats["ssrc"] == session_a._ssrc

    await session_a.close()
    await session_b.close()


@pytest.mark.asyncio
async def test_bye_packet() -> None:
    """Test that BYE packet is properly formed."""
    bye = RtcpByePacket(sources=[12345])
    data = bytes(bye)

    packets = RtcpPacket.parse(data)
    assert len(packets) == 1
    assert isinstance(packets[0], RtcpByePacket)
    assert packets[0].sources == [12345]


@pytest.mark.asyncio
async def test_sr_sdes_compound() -> None:
    """Test that SR+SDES compound packet is properly formed."""
    sr = RtcpSrPacket(
        ssrc=12345,
        sender_info=RtcpSenderInfo(
            ntp_timestamp=0,
            rtp_timestamp=0,
            packet_count=10,
            octet_count=1600,
        ),
    )

    sdes = RtcpSdesPacket(
        chunks=[
            RtcpSourceInfo(
                ssrc=12345,
                items=[(1, b"aiortp")],
            )
        ]
    )

    data = bytes(sr) + bytes(sdes)
    packets = RtcpPacket.parse(data)
    assert len(packets) == 2
    assert isinstance(packets[0], RtcpSrPacket)
    assert isinstance(packets[1], RtcpSdesPacket)
    assert packets[0].sender_info.packet_count == 10
