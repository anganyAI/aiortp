"""
Low-level packet example: build, serialize, and parse RTP/RTCP packets by hand.

Demonstrates:
  - Constructing RtpPacket manually
  - Serializing and parsing RTP packets
  - Building and parsing RTCP compound packets (SR + SDES + BYE)
  - Using is_rtcp() to demux
"""

from aiortp import (
    RtcpByePacket,
    RtcpPacket,
    RtcpSdesPacket,
    RtcpSrPacket,
    RtpPacket,
    is_rtcp,
)
from aiortp.packet import RtcpSenderInfo, RtcpSourceInfo


def main() -> None:
    # --- Build and serialize an RTP packet ---
    print("=== RTP Packet ===")
    pkt = RtpPacket(
        payload_type=0,       # PCMU
        marker=1,             # first packet of a talkspurt
        sequence_number=1000,
        timestamp=8000,
        ssrc=0xDEADBEEF,
        payload=b"\x80" * 160,  # 160 bytes of µ-law silence
    )
    data = pkt.serialize()
    print(f"Serialized: {len(data)} bytes")
    print(f"  {data[:20].hex()}...")

    # Parse it back
    parsed = RtpPacket.parse(data)
    print(f"Parsed: seq={parsed.sequence_number}, ts={parsed.timestamp}, "
          f"pt={parsed.payload_type}, marker={parsed.marker}, "
          f"ssrc={parsed.ssrc:#010x}, payload={len(parsed.payload)} bytes")

    # Demux check
    print(f"is_rtcp: {is_rtcp(data)}")
    print()

    # --- Build an RTCP compound packet: SR + SDES + BYE ---
    print("=== RTCP Compound Packet ===")
    sr = RtcpSrPacket(
        ssrc=0xDEADBEEF,
        sender_info=RtcpSenderInfo(
            ntp_timestamp=0x1234567890ABCDEF,
            rtp_timestamp=48000,
            packet_count=100,
            octet_count=16000,
        ),
    )
    sdes = RtcpSdesPacket(
        chunks=[
            RtcpSourceInfo(
                ssrc=0xDEADBEEF,
                items=[(1, b"my-cname@example.com")],
            )
        ]
    )
    bye = RtcpByePacket(sources=[0xDEADBEEF])

    compound = bytes(sr) + bytes(sdes) + bytes(bye)
    print(f"Compound packet: {len(compound)} bytes")
    print(f"is_rtcp: {is_rtcp(compound)}")

    # Parse compound
    packets = RtcpPacket.parse(compound)
    print(f"Parsed {len(packets)} RTCP packets:")
    for i, p in enumerate(packets):
        print(f"  [{i}] {type(p).__name__}")
        if isinstance(p, RtcpSrPacket):
            si = p.sender_info
            print(f"      ssrc={p.ssrc:#010x}, packets={si.packet_count}, "
                  f"octets={si.octet_count}")
        elif isinstance(p, RtcpSdesPacket):
            for chunk in p.chunks:
                for item_type, item_value in chunk.items:
                    print(f"      ssrc={chunk.ssrc:#010x}, "
                          f"type={item_type}, value={item_value!r}")
        elif isinstance(p, RtcpByePacket):
            print(f"      sources={[f'{s:#010x}' for s in p.sources]}")


if __name__ == "__main__":
    main()
