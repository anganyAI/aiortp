import asyncio
import socket


class PortAllocator:
    def __init__(self, port_range: tuple[int, int] = (10000, 20000)) -> None:
        self._min_port = port_range[0]
        self._max_port = port_range[1]
        # Ensure min_port is even
        if self._min_port % 2 != 0:
            self._min_port += 1
        self._allocated: set[int] = set()
        self._lock = asyncio.Lock()

    async def allocate(self) -> tuple[int, int]:
        """
        Allocate an even/odd port pair for RTP/RTCP.
        Returns (rtp_port, rtcp_port) where rtcp_port = rtp_port + 1.
        """
        async with self._lock:
            for port in range(self._min_port, self._max_port, 2):
                if port in self._allocated:
                    continue
                # Try to bind both ports
                rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                rtcp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    rtp_sock.bind(("", port))
                    rtcp_sock.bind(("", port + 1))
                    rtp_sock.close()
                    rtcp_sock.close()
                    self._allocated.add(port)
                    return port, port + 1
                except OSError:
                    rtp_sock.close()
                    rtcp_sock.close()
                    continue
            raise RuntimeError("No available port pair in range")

    async def release(self, rtp_port: int) -> None:
        """Release a previously allocated port pair."""
        async with self._lock:
            self._allocated.discard(rtp_port)
