# aiortp

Asyncio RTP/RTCP audio library for Python.

Plain RTP/RTCP for audio — no WebRTC, no ICE, no DTLS. Built for telephony and VoIP applications where you need direct control over RTP streams.

Portions derived from [aiortc](https://github.com/aiortc/aiortc) by Jeremy Lainé (BSD-3-Clause).

## Features

- **Pure Python** — zero required dependencies, Python >=3.11
- **AsyncIO native** — built on `asyncio.DatagramProtocol`
- **G.711 codecs** — µ-law (PCMU) and A-law (PCMA) with precomputed lookup tables
- **L16 codec** — linear 16-bit PCM (s16le ↔ s16be)
- **Optional Opus** — via `opuslib` (`pip install aiortp[opus]`)
- **RTCP** — Sender Reports, SDES, BYE with RFC 3550 randomized intervals
- **DTMF** — RFC 4733 telephone-event send/receive with redundant end packets
- **Jitter buffer** — extracted from aiortc, with configurable capacity and prefetch
- **Fully typed** — PEP 561 `py.typed` marker included

## Installation

```bash
pip install aiortp
```

With Opus support:

```bash
pip install aiortp[opus]
```

## Quick Start

```python
import asyncio
from aiortp import RTPSession, PayloadType

async def main():
    # Create two sessions on localhost
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 10000),
        remote_addr=("127.0.0.1", 10002),
        payload_type=PayloadType.PCMU,
    )

    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 10002),
        remote_addr=("127.0.0.1", 10000),
        payload_type=PayloadType.PCMU,
    )

    # Receive callback
    def on_audio(data: bytes, timestamp: int) -> None:
        print(f"Received {len(data)} bytes, ts={timestamp}")

    session_b.on_audio = on_audio

    # Send PCM audio (auto-encoded to µ-law)
    pcm = b"\x00" * 320  # 160 samples of silence (20ms at 8kHz)
    for i in range(10):
        session_a.send_audio_pcm(pcm, timestamp=i * 160)

    await asyncio.sleep(1)

    await session_a.close()
    await session_b.close()

asyncio.run(main())
```

## DTMF

```python
# Send
session.send_dtmf("1", duration_ms=160, timestamp=0)

# Receive
def on_dtmf(digit: str, duration: int) -> None:
    print(f"Got DTMF: {digit}")

session.on_dtmf = on_dtmf
```

## Codec Registry

```python
from aiortp import get_codec, PayloadType

codec = get_codec(PayloadType.PCMU)  # or PCMA, L16
encoded = codec.encode(pcm_bytes)
decoded = codec.decode(encoded)
```

## Low-Level Packets

```python
from aiortp import RtpPacket, RtcpPacket, is_rtcp

# Parse
packet = RtpPacket.parse(data)
print(packet.sequence_number, packet.timestamp, packet.payload_type)

# Build
packet = RtpPacket(
    payload_type=0,
    sequence_number=1000,
    timestamp=8000,
    ssrc=0xDEADBEEF,
    payload=b"\x80" * 160,
)
data = packet.serialize()

# Demux RTP vs RTCP
if is_rtcp(data):
    rtcp_packets = RtcpPacket.parse(data)
```

## Examples

See the [`examples/`](examples/) directory:

- **`loopback.py`** — two sessions exchanging G.711 audio on localhost
- **`dtmf.py`** — sending and receiving DTMF digits
- **`codec_roundtrip.py`** — encode/decode with each built-in codec
- **`raw_packets.py`** — low-level RTP/RTCP packet construction

## License

BSD-3-Clause. See [LICENSE](LICENSE) for details.
