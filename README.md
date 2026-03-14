# aiortp

Asyncio RTP/RTCP library for Python — audio and video.

Plain RTP/RTCP for audio and video — no WebRTC, no ICE, no DTLS. Built for telephony, VoIP, and video streaming applications where you need direct control over RTP streams.

Portions derived from [aiortc](https://github.com/aiortc/aiortc) by Jeremy Lainé (BSD-3-Clause).

## Features

- **Pure Python** — zero required dependencies, Python >=3.11
- **AsyncIO native** — built on `asyncio.DatagramProtocol`
- **Audio codecs** — G.711 µ-law/A-law, L16, G.722 (`pip install aiortp[g722]`), Opus (`pip install aiortp[opus]`)
- **Video codecs** — H.264 (RFC 6184) and VP9 (RFC 9628) depacketization/packetization
- **RTCP** — Sender Reports, SDES, BYE, PLI, NACK with RFC 3550 randomized intervals
- **DTMF** — RFC 4733 telephone-event send/receive with redundant end packets
- **Jitter buffer** — reordering for both audio (timestamp boundaries) and video (marker-bit frame detection)
- **STUN** — inline Binding Response for basic ICE connectivity
- **Fully typed** — PEP 561 `py.typed` marker included

## Installation

```bash
pip install aiortp
```

With optional codecs:

```bash
pip install aiortp[opus]   # Opus support
pip install aiortp[g722]   # G.722 wideband
```

## Quick Start — Audio

```python
import asyncio
from aiortp import RTPSession, PayloadType

async def main():
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

## Quick Start — Video

```python
import asyncio
from aiortp import VideoRTPSession

async def main():
    sender = await VideoRTPSession.create(
        local_addr=("127.0.0.1", 20000),
        remote_addr=("127.0.0.1", 20002),
        codec="h264",
    )

    receiver = await VideoRTPSession.create(
        local_addr=("127.0.0.1", 20002),
        remote_addr=("127.0.0.1", 20000),
        codec="h264",
    )

    def on_frame(data: bytes, timestamp: int, is_keyframe: bool) -> None:
        print(f"Frame: {len(data)} bytes, keyframe={is_keyframe}")

    receiver.on_frame = on_frame

    # Send H.264 NAL units (SPS + PPS + IDR)
    sps = bytes([0x67, 0x42, 0x00, 0x1E])
    pps = bytes([0x68, 0xCE, 0x38, 0x80])
    idr = bytes([0x65]) + b"\x00" * 100
    sender.send_frame([sps, pps, idr], timestamp=0, keyframe=True)

    await asyncio.sleep(1)
    await sender.close()
    await receiver.close()

asyncio.run(main())
```

VP9 works the same way — pass `codec="vp9"` to `VideoRTPSession.create()`.

## DTMF

```python
# Send
session.send_dtmf("1", duration_ms=160, timestamp=0)

# Receive
def on_dtmf(digit: str, duration: int) -> None:
    print(f"Got DTMF: {digit}")

session.on_dtmf = on_dtmf
```

## Video RTCP Feedback

```python
# Request a keyframe from the remote sender
receiver.request_keyframe()

# Get notified when the remote side requests a keyframe
def on_keyframe_needed() -> None:
    print("Remote requested a keyframe")

sender.on_keyframe_needed = on_keyframe_needed
```

## Codec Registry

```python
from aiortp import get_codec, PayloadType

codec = get_codec(PayloadType.PCMU)  # or PCMA, L16, G722
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

## Video Depacketizers (Standalone)

```python
from aiortp import H264Depacketizer, VP9Depacketizer

# H.264: feed RTP payloads, get NAL units
depkt = H264Depacketizer()
nals = depkt.feed(rtp_payload, marker=is_last_packet)

# VP9: feed RTP payloads, get (frame_data, is_keyframe) tuples
depkt = VP9Depacketizer()
frames = depkt.feed(rtp_payload, marker=is_last_packet)
```

## Examples

See the [`examples/`](examples/) directory:

- **`loopback.py`** — two sessions exchanging G.711 audio on localhost
- **`dtmf.py`** — sending and receiving DTMF digits
- **`codec_roundtrip.py`** — encode/decode with each built-in codec
- **`raw_packets.py`** — low-level RTP/RTCP packet construction
- **`send_wav.py`** — stream a WAV file over RTP

## License

MIT. See [LICENSE](LICENSE) for details.
