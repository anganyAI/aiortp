"""
Loopback example: two RTP sessions on localhost exchanging G.711 µ-law audio.

Demonstrates:
  - Creating two RTPSession instances
  - Sending PCM audio (auto-encoded via PCMU codec)
  - Receiving decoded audio via the on_audio callback
  - Printing session stats
  - Graceful shutdown with RTCP BYE
"""

import asyncio
import struct

from aiortp import RTPSession, PayloadType


def generate_sine_pcm(frequency: float, sample_rate: int, num_samples: int) -> bytes:
    """Generate a sine wave as s16le PCM."""
    import math

    buf = bytearray()
    for i in range(num_samples):
        sample = int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
        buf += struct.pack("<h", sample)
    return bytes(buf)


async def main() -> None:
    received_frames: list[tuple[bytes, int]] = []
    done = asyncio.Event()

    # --- Create two sessions on localhost with OS-assigned ports ---
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),  # placeholder, updated below
        payload_type=PayloadType.PCMU,
    )

    # Read session A's actual bound address
    addr_a = session_a._rtp_transport._transport.get_extra_info("sockname")
    print(f"Session A listening on {addr_a}")

    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=addr_a,
        payload_type=PayloadType.PCMU,
    )

    addr_b = session_b._rtp_transport._transport.get_extra_info("sockname")
    print(f"Session B listening on {addr_b}")

    # Point A → B
    session_a.update_remote(addr_b)

    # --- Set up receive callback on session B ---
    def on_audio(data: bytes, timestamp: int) -> None:
        received_frames.append((data, timestamp))
        print(f"  B received frame: ts={timestamp}, {len(data)} bytes PCM")
        if len(received_frames) >= 5:
            done.set()

    session_b.on_audio = on_audio

    # --- Send 20 frames of 440 Hz tone (20 ms each) from A → B ---
    print("\nSending 20 frames of 440 Hz tone from A → B ...")
    for i in range(20):
        pcm = generate_sine_pcm(
            frequency=440.0,
            sample_rate=8000,
            num_samples=160,  # 20 ms at 8 kHz
        )
        session_a.send_audio_pcm(pcm, timestamp=i * 160)

    # Wait for some frames to arrive
    try:
        await asyncio.wait_for(done.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass

    # --- Print stats ---
    print(f"\nSession A stats: {session_a.stats}")
    print(f"Session B stats: {session_b.stats}")
    print(f"Frames received by B: {len(received_frames)}")

    # --- Graceful shutdown (sends RTCP BYE) ---
    await session_a.close()
    await session_b.close()
    print("\nBoth sessions closed.")


if __name__ == "__main__":
    asyncio.run(main())
