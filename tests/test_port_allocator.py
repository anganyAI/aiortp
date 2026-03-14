"""Tests for PortAllocator and session integration."""

from __future__ import annotations

import pytest

from aiortp.port_allocator import PortAllocator
from aiortp.session import RTPSession


class TestPortAllocator:
    async def test_allocate_returns_even_odd_pair(self) -> None:
        alloc = PortAllocator(port_range=(30000, 30100))
        rtp, rtcp = await alloc.allocate()
        assert rtp % 2 == 0
        assert rtcp == rtp + 1
        await alloc.release(rtp)

    async def test_release_allows_reuse(self) -> None:
        alloc = PortAllocator(port_range=(30000, 30004))
        rtp1, _ = await alloc.allocate()
        await alloc.release(rtp1)
        rtp2, _ = await alloc.allocate()
        assert rtp2 == rtp1

    async def test_multiple_allocations_unique(self) -> None:
        alloc = PortAllocator(port_range=(30000, 30100))
        pair1 = await alloc.allocate()
        pair2 = await alloc.allocate()
        assert pair1[0] != pair2[0]
        await alloc.release(pair1[0])
        await alloc.release(pair2[0])

    async def test_exhaustion_raises(self) -> None:
        # Range of 4 ports = 2 pairs max
        alloc = PortAllocator(port_range=(30200, 30204))
        await alloc.allocate()
        await alloc.allocate()
        with pytest.raises(RuntimeError, match="No available port pair"):
            await alloc.allocate()

    async def test_odd_min_port_rounded_up(self) -> None:
        alloc = PortAllocator(port_range=(30001, 30100))
        rtp, _ = await alloc.allocate()
        assert rtp % 2 == 0
        assert rtp >= 30002
        await alloc.release(rtp)


class TestSessionWithAllocator:
    async def test_audio_session_uses_allocator(self) -> None:
        alloc = PortAllocator(port_range=(31000, 31100))
        session = await RTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=0,
            port_allocator=alloc,
        )
        try:
            bound = session._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
            assert 31000 <= bound[1] < 31100
            assert bound[1] % 2 == 0
        finally:
            await session.close()

        # Port released after close — can allocate again
        rtp, _ = await alloc.allocate()
        assert 31000 <= rtp < 31100
        await alloc.release(rtp)
