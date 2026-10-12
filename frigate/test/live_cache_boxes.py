"""Builders for the fragmented MP4 boxes go2rtc sends, for live cache tests."""

import struct

VIDEO_TRACK = 1
AUDIO_TRACK = 2
# default sample flags go2rtc writes into tfhd
KEYFRAME = 0x02000000
NON_KEYFRAME = 0x01010000
AUDIO = 0x02000000


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def track(track_id: int, handler: bytes, version: int = 0) -> bytes:
    times = b"\x00" * (16 if version else 8)
    tkhd = box(
        b"tkhd",
        bytes([version, 0, 0, 0]) + times + struct.pack(">I", track_id) + b"\x00" * 8,
    )
    hdlr = box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 13)
    return box(b"trak", tkhd + box(b"mdia", hdlr))


def init_segment(*tracks: bytes) -> bytes:
    return box(b"ftyp", b"iso5\x00\x00\x02\x00iso5") + box(
        b"moov", box(b"mvhd", b"\x00" * 100) + b"".join(tracks)
    )


def fragment(track_id: int, flags: int, payload: bytes = b"frame") -> bytes:
    # tfhd as go2rtc writes it: default duration, size and flags, base is moof
    tfhd = box(
        b"tfhd",
        b"\x00\x02\x00\x38" + struct.pack(">IIII", track_id, 3000, len(payload), flags),
    )
    tfdt = box(b"tfdt", b"\x01\x00\x00\x00" + struct.pack(">Q", 0))
    trun = box(b"trun", b"\x00\x00\x00\x01" + struct.pack(">II", 1, 0))
    moof = box(
        b"moof",
        box(b"mfhd", struct.pack(">II", 0, 1)) + box(b"traf", tfhd + tfdt + trun),
    )
    return moof + box(b"mdat", payload)
