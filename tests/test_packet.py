from aiortp import packet
from aiortp.packet import (
    RtcpByePacket,
    RtcpPacket,
    RtcpPsfbPacket,
    RtcpRrPacket,
    RtcpRtpfbPacket,
    RtcpSdesPacket,
    RtcpSrPacket,
    RtpPacket,
    clamp_packets_lost,
    pack_header_extensions,
    pack_packets_lost,
    unpack_header_extensions,
    unpack_packets_lost,
)

from .utils import TestCase, load


class RtcpPacketTest(TestCase):
    def test_bye(self) -> None:
        data = load("rtcp_bye.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpByePacket)
        self.assertEqual(pkt.sources, [2924645187])
        self.assertEqual(bytes(pkt), data)

    def test_bye_invalid(self) -> None:
        data = load("rtcp_bye_invalid.bin")

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP bye length is invalid")

    def test_bye_no_sources(self) -> None:
        data = load("rtcp_bye_no_sources.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpByePacket)
        self.assertEqual(pkt.sources, [])
        self.assertEqual(bytes(pkt), data)

    def test_bye_only_padding(self) -> None:
        data = load("rtcp_bye_padding.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpByePacket)
        self.assertEqual(pkt.sources, [])
        self.assertEqual(bytes(pkt), b"\x80\xcb\x00\x00")

    def test_bye_only_padding_zero(self) -> None:
        data = load("rtcp_bye_padding.bin")[0:4] + b"\x00\x00\x00\x00"

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP packet padding length is invalid")

    def test_psfb_invalid(self) -> None:
        data = load("rtcp_psfb_invalid.bin")

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(
            str(cm.exception), "RTCP payload-specific feedback length is invalid"
        )

    def test_psfb_pli(self) -> None:
        data = load("rtcp_psfb_pli.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpPsfbPacket)
        self.assertEqual(pkt.fmt, 1)
        self.assertEqual(pkt.ssrc, 1414554213)
        self.assertEqual(pkt.media_ssrc, 587284409)
        self.assertEqual(pkt.fci, b"")
        self.assertEqual(bytes(pkt), data)

    def test_rr(self) -> None:
        data = load("rtcp_rr.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpRrPacket)
        self.assertEqual(pkt.ssrc, 817267719)
        self.assertEqual(pkt.reports[0].ssrc, 1200895919)
        self.assertEqual(pkt.reports[0].fraction_lost, 0)
        self.assertEqual(pkt.reports[0].packets_lost, 0)
        self.assertEqual(pkt.reports[0].highest_sequence, 630)
        self.assertEqual(pkt.reports[0].jitter, 1906)
        self.assertEqual(pkt.reports[0].lsr, 0)
        self.assertEqual(pkt.reports[0].dlsr, 0)
        self.assertEqual(bytes(pkt), data)

    def test_rr_invalid(self) -> None:
        data = load("rtcp_rr_invalid.bin")

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP receiver report length is invalid")

    def test_rr_truncated(self) -> None:
        data = load("rtcp_rr.bin")

        for length in range(1, 4):
            with self.assertRaises(ValueError) as cm:
                RtcpPacket.parse(data[0:length])
            self.assertEqual(
                str(cm.exception), "RTCP packet length is less than 4 bytes"
            )

        for length in range(4, 32):
            with self.assertRaises(ValueError) as cm:
                RtcpPacket.parse(data[0:length])
            self.assertEqual(str(cm.exception), "RTCP packet is truncated")

    def test_sdes(self) -> None:
        data = load("rtcp_sdes.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpSdesPacket)
        self.assertEqual(pkt.chunks[0].ssrc, 1831097322)
        self.assertEqual(
            pkt.chunks[0].items, [(1, b"{63f459ea-41fe-4474-9d33-9707c9ee79d1}")]
        )
        self.assertEqual(bytes(pkt), data)

    def test_sdes_item_truncated(self) -> None:
        data = load("rtcp_sdes_item_truncated.bin")

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP SDES item is truncated")

    def test_sdes_source_truncated(self) -> None:
        data = load("rtcp_sdes_source_truncated.bin")

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP SDES source is truncated")

    def test_sr(self) -> None:
        data = load("rtcp_sr.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpSrPacket)
        self.assertEqual(pkt.ssrc, 1831097322)
        self.assertEqual(pkt.sender_info.ntp_timestamp, 16016567581311369308)
        self.assertEqual(pkt.sender_info.rtp_timestamp, 1722342718)
        self.assertEqual(pkt.sender_info.packet_count, 269)
        self.assertEqual(pkt.sender_info.octet_count, 13557)
        self.assertEqual(len(pkt.reports), 1)
        self.assertEqual(pkt.reports[0].ssrc, 2398654957)
        self.assertEqual(pkt.reports[0].fraction_lost, 0)
        self.assertEqual(pkt.reports[0].packets_lost, 0)
        self.assertEqual(pkt.reports[0].highest_sequence, 246)
        self.assertEqual(pkt.reports[0].jitter, 127)
        self.assertEqual(pkt.reports[0].lsr, 0)
        self.assertEqual(pkt.reports[0].dlsr, 0)
        self.assertEqual(bytes(pkt), data)

    def test_sr_invalid(self) -> None:
        data = load("rtcp_sr_invalid.bin")

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP sender report length is invalid")

    def test_rtpfb(self) -> None:
        data = load("rtcp_rtpfb.bin")
        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 1)

        pkt = self.ensureIsInstance(packets[0], RtcpRtpfbPacket)
        self.assertEqual(pkt.fmt, 1)
        self.assertEqual(pkt.ssrc, 2336520123)
        self.assertEqual(pkt.media_ssrc, 4145934052)
        self.assertEqual(
            pkt.lost,
            [12, 32, 39, 54, 76, 110, 123, 142, 183, 187, 223, 236, 271, 292],
        )
        self.assertEqual(bytes(pkt), data)

    def test_rtpfb_invalid(self) -> None:
        data = load("rtcp_rtpfb_invalid.bin")

        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP RTP feedback length is invalid")

    def test_compound(self) -> None:
        data = load("rtcp_sr.bin") + load("rtcp_sdes.bin")

        packets = RtcpPacket.parse(data)
        self.assertEqual(len(packets), 2)
        self.assertIsInstance(packets[0], RtcpSrPacket)
        self.assertIsInstance(packets[1], RtcpSdesPacket)

    def test_bad_version(self) -> None:
        data = b"\xc0" + load("rtcp_rr.bin")[1:]
        with self.assertRaises(ValueError) as cm:
            RtcpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTCP packet has invalid version")


class RtpPacketTest(TestCase):
    def test_dtmf(self) -> None:
        data = load("rtp_dtmf.bin")
        pkt = RtpPacket.parse(data)
        self.assertEqual(pkt.version, 2)
        self.assertEqual(pkt.marker, 1)
        self.assertEqual(pkt.payload_type, 101)
        self.assertEqual(pkt.sequence_number, 24152)
        self.assertEqual(pkt.timestamp, 4021352124)
        self.assertEqual(pkt.csrc, [])
        self.assertEqual(pkt.extensions, packet.HeaderExtensions())
        self.assertEqual(len(pkt.payload), 4)
        self.assertEqual(pkt.serialize(), data)

    def test_no_ssrc(self) -> None:
        data = load("rtp.bin")
        pkt = RtpPacket.parse(data)
        self.assertEqual(pkt.version, 2)
        self.assertEqual(pkt.marker, 0)
        self.assertEqual(pkt.payload_type, 0)
        self.assertEqual(pkt.sequence_number, 15743)
        self.assertEqual(pkt.timestamp, 3937035252)
        self.assertEqual(pkt.csrc, [])
        self.assertEqual(pkt.extensions, packet.HeaderExtensions())
        self.assertEqual(len(pkt.payload), 160)
        self.assertEqual(pkt.serialize(), data)

        self.assertEqual(
            repr(pkt),
            "RtpPacket(seq=15743, ts=3937035252, marker=0, payload=0, 160 bytes)",
        )

    def test_padding_only(self) -> None:
        data = load("rtp_only_padding.bin")
        pkt = RtpPacket.parse(data)
        self.assertEqual(pkt.version, 2)
        self.assertEqual(pkt.marker, 0)
        self.assertEqual(pkt.payload_type, 120)
        self.assertEqual(pkt.sequence_number, 27759)
        self.assertEqual(pkt.timestamp, 4044047131)
        self.assertEqual(pkt.csrc, [])
        self.assertEqual(pkt.extensions, packet.HeaderExtensions())
        self.assertEqual(len(pkt.payload), 0)
        self.assertEqual(pkt.padding_size, 224)

        serialized = pkt.serialize()
        self.assertEqual(len(serialized), len(data))
        self.assertEqual(serialized[0:12], data[0:12])
        self.assertEqual(serialized[-1], data[-1])

    def test_padding_only_with_header_extensions(self) -> None:
        extensions_map = packet.HeaderExtensionsMap()
        extensions_map.configure(
            [
                (
                    2,
                    "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
                ),
            ]
        )

        data = load("rtp_only_padding_with_header_extensions.bin")
        pkt = RtpPacket.parse(data, extensions_map)
        self.assertEqual(pkt.version, 2)
        self.assertEqual(pkt.marker, 0)
        self.assertEqual(pkt.payload_type, 98)
        self.assertEqual(pkt.sequence_number, 22138)
        self.assertEqual(pkt.timestamp, 3171065731)
        self.assertEqual(pkt.csrc, [])
        self.assertEqual(
            pkt.extensions, packet.HeaderExtensions(abs_send_time=15846540)
        )
        self.assertEqual(len(pkt.payload), 0)
        self.assertEqual(pkt.padding_size, 224)

        serialized = pkt.serialize(extensions_map)
        self.assertEqual(len(serialized), len(data))
        self.assertEqual(serialized[0:20], data[0:20])
        self.assertEqual(serialized[-1], data[-1])

    def test_padding_too_long(self) -> None:
        data = load("rtp_only_padding.bin")[0:12] + b"\x02"
        with self.assertRaises(ValueError) as cm:
            RtpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTP packet padding length is invalid")

    def test_padding_zero(self) -> None:
        data = load("rtp_only_padding.bin")[0:12] + b"\x00"
        with self.assertRaises(ValueError) as cm:
            RtpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTP packet padding length is invalid")

    def test_with_csrc(self) -> None:
        data = load("rtp_with_csrc.bin")
        pkt = RtpPacket.parse(data)
        self.assertEqual(pkt.version, 2)
        self.assertEqual(pkt.marker, 0)
        self.assertEqual(pkt.payload_type, 0)
        self.assertEqual(pkt.sequence_number, 16082)
        self.assertEqual(pkt.timestamp, 144)
        self.assertEqual(pkt.csrc, [2882400001, 3735928559])
        self.assertEqual(pkt.extensions, packet.HeaderExtensions())
        self.assertEqual(len(pkt.payload), 160)
        self.assertEqual(pkt.serialize(), data)

    def test_with_csrc_truncated(self) -> None:
        data = load("rtp_with_csrc.bin")
        for length in range(12, 20):
            with self.assertRaises(ValueError) as cm:
                RtpPacket.parse(data[0:length])
            self.assertEqual(str(cm.exception), "RTP packet has truncated CSRC")

    def test_with_sdes_mid(self) -> None:
        extensions_map = packet.HeaderExtensionsMap()
        extensions_map.configure(
            [(9, "urn:ietf:params:rtp-hdrext:sdes:mid")]
        )

        data = load("rtp_with_sdes_mid.bin")
        pkt = RtpPacket.parse(data, extensions_map)
        self.assertEqual(pkt.version, 2)
        self.assertEqual(pkt.marker, 1)
        self.assertEqual(pkt.payload_type, 111)
        self.assertEqual(pkt.sequence_number, 14156)
        self.assertEqual(pkt.timestamp, 1327210925)
        self.assertEqual(pkt.csrc, [])
        self.assertEqual(pkt.extensions, packet.HeaderExtensions(mid="0"))
        self.assertEqual(len(pkt.payload), 54)
        self.assertEqual(pkt.serialize(extensions_map), data)

    def test_with_sdes_mid_truncated(self) -> None:
        data = load("rtp_with_sdes_mid.bin")

        for length in range(12, 16):
            with self.assertRaises(ValueError) as cm:
                RtpPacket.parse(data[0:length])
            self.assertEqual(
                str(cm.exception), "RTP packet has truncated extension profile / length"
            )

        for length in range(16, 20):
            with self.assertRaises(ValueError) as cm:
                RtpPacket.parse(data[0:length])
            self.assertEqual(
                str(cm.exception), "RTP packet has truncated extension value"
            )

    def test_truncated(self) -> None:
        data = load("rtp.bin")[0:11]
        with self.assertRaises(ValueError) as cm:
            RtpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTP packet length is less than 12 bytes")

    def test_bad_version(self) -> None:
        data = b"\xc0" + load("rtp.bin")[1:]
        with self.assertRaises(ValueError) as cm:
            RtpPacket.parse(data)
        self.assertEqual(str(cm.exception), "RTP packet has invalid version")


class RtpUtilTest(TestCase):
    def test_clamp_packets_lost(self) -> None:
        self.assertEqual(clamp_packets_lost(-8388609), -8388608)
        self.assertEqual(clamp_packets_lost(-8388608), -8388608)
        self.assertEqual(clamp_packets_lost(0), 0)
        self.assertEqual(clamp_packets_lost(8388607), 8388607)
        self.assertEqual(clamp_packets_lost(8388608), 8388607)

    def test_pack_packets_lost(self) -> None:
        self.assertEqual(pack_packets_lost(-8388608), b"\x80\x00\x00")
        self.assertEqual(pack_packets_lost(-1), b"\xff\xff\xff")
        self.assertEqual(pack_packets_lost(0), b"\x00\x00\x00")
        self.assertEqual(pack_packets_lost(1), b"\x00\x00\x01")
        self.assertEqual(pack_packets_lost(8388607), b"\x7f\xff\xff")

    def test_unpack_packets_lost(self) -> None:
        self.assertEqual(unpack_packets_lost(b"\x80\x00\x00"), -8388608)
        self.assertEqual(unpack_packets_lost(b"\xff\xff\xff"), -1)
        self.assertEqual(unpack_packets_lost(b"\x00\x00\x00"), 0)
        self.assertEqual(unpack_packets_lost(b"\x00\x00\x01"), 1)
        self.assertEqual(unpack_packets_lost(b"\x7f\xff\xff"), 8388607)

    def test_unpack_header_extensions(self) -> None:
        # none
        self.assertEqual(unpack_header_extensions(0, b""), [])

        # one-byte, value
        self.assertEqual(unpack_header_extensions(0xBEDE, b"\x900"), [(9, b"0")])

        # one-byte, value, padding, value
        self.assertEqual(
            unpack_header_extensions(0xBEDE, b"\x900\x00\x00\x301"),
            [(9, b"0"), (3, b"1")],
        )

        # one-byte, value, value
        self.assertEqual(
            unpack_header_extensions(0xBEDE, b"\x10\xc18sdparta_0"),
            [(1, b"\xc1"), (3, b"sdparta_0")],
        )

        # two-byte, value
        self.assertEqual(unpack_header_extensions(0x1000, b"\xff\x010"), [(255, b"0")])

        # two-byte, value (1 byte), padding, value (2 bytes)
        self.assertEqual(
            unpack_header_extensions(0x1000, b"\xff\x010\x00\xf0\x0212"),
            [(255, b"0"), (240, b"12")],
        )

    def test_unpack_header_extensions_bad(self) -> None:
        # one-byte, value (truncated)
        with self.assertRaises(ValueError) as cm:
            unpack_header_extensions(0xBEDE, b"\x90")
        self.assertEqual(
            str(cm.exception), "RTP one-byte header extension value is truncated"
        )

        # two-byte (truncated)
        with self.assertRaises(ValueError) as cm:
            unpack_header_extensions(0x1000, b"\xff")
        self.assertEqual(
            str(cm.exception), "RTP two-byte header extension is truncated"
        )

        # two-byte, value (truncated)
        with self.assertRaises(ValueError) as cm:
            unpack_header_extensions(0x1000, b"\xff\x020")
        self.assertEqual(
            str(cm.exception), "RTP two-byte header extension value is truncated"
        )

    def test_pack_header_extensions(self) -> None:
        # none
        self.assertEqual(pack_header_extensions([]), (0, b""))

        # one-byte, single value
        self.assertEqual(
            pack_header_extensions([(9, b"0")]), (0xBEDE, b"\x900\x00\x00")
        )

        # one-byte, two values
        self.assertEqual(
            pack_header_extensions([(1, b"\xc1"), (3, b"sdparta_0")]),
            (0xBEDE, b"\x10\xc18sdparta_0"),
        )

        # two-byte, single value
        self.assertEqual(
            pack_header_extensions([(255, b"0")]), (0x1000, b"\xff\x010\x00")
        )

    def test_map_header_extensions(self) -> None:
        data = bytearray(
            [
                0x90,
                0x64,
                0x00,
                0x58,
                0x65,
                0x43,
                0x12,
                0x78,
                0x12,
                0x34,
                0x56,
                0x78,  # SSRC
                0xBE,
                0xDE,
                0x00,
                0x08,  # Extension of size 8x32bit words.
                0x40,
                0xDA,  # AudioLevel.
                0x22,
                0x01,
                0x56,
                0xCE,  # TransmissionOffset.
                0x62,
                0x12,
                0x34,
                0x56,  # AbsoluteSendTime.
                0x81,
                0xCE,
                0xAB,  # TransportSequenceNumber.
                0xA0,
                0x03,  # VideoRotation.
                0xB2,
                0x12,
                0x48,
                0x76,  # PlayoutDelayLimits.
                0xC2,
                0x72,
                0x74,
                0x78,  # RtpStreamId
                0xD5,
                0x73,
                0x74,
                0x72,
                0x65,
                0x61,
                0x6D,  # RepairedRtpStreamId
                0x00,
                0x00,  # Padding to 32bit boundary.
            ]
        )
        extensions_map = packet.HeaderExtensionsMap()
        extensions_map.configure(
            [
                (2, "urn:ietf:params:rtp-hdrext:toffset"),
                (4, "urn:ietf:params:rtp-hdrext:ssrc-audio-level"),
                (
                    6,
                    "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
                ),
                (
                    8,
                    "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01",
                ),
                (12, "urn:ietf:params:rtp-hdrext:sdes:rtp-stream-id"),
                (
                    13,
                    "urn:ietf:params:rtp-hdrext:sdes:repaired-rtp-stream-id",
                ),
            ]
        )

        pkt = RtpPacket.parse(data, extensions_map)

        # check mapped values
        self.assertEqual(pkt.extensions.abs_send_time, 0x123456)
        self.assertEqual(pkt.extensions.audio_level, (True, 90))
        self.assertEqual(pkt.extensions.mid, None)
        self.assertEqual(pkt.extensions.repaired_rtp_stream_id, "stream")
        self.assertEqual(pkt.extensions.rtp_stream_id, "rtx")
        self.assertEqual(pkt.extensions.transmission_offset, 0x156CE)
        self.assertEqual(pkt.extensions.transport_sequence_number, 0xCEAB)

        # check serialization roundtrip
        pkt.serialize(extensions_map)
