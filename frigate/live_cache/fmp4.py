"""Reading the fragmented MP4 stream go2rtc sends to MSE viewers."""

import struct
from collections.abc import Iterator
from typing import NamedTuple

HEADER_SIZE = 8
# sample_is_non_sync_sample in ISO BMFF sample flags
NON_SYNC_SAMPLE = 0x10000

# (flag, size) of the optional tfhd fields ahead of the default sample flags
_TFHD_OPTIONAL_FIELDS = (
    (0x000001, 8),  # base data offset
    (0x000002, 4),  # sample description index
    (0x000008, 4),  # default sample duration
    (0x000010, 4),  # default sample size
)
_TFHD_DEFAULT_SAMPLE_FLAGS = 0x000020


class BoxReader:
    """Splits a byte stream into complete top-level boxes, however it is chunked."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        self._buffer += data
        boxes: list[tuple[str, bytes]] = []

        while len(self._buffer) >= HEADER_SIZE:
            size, kind = struct.unpack_from(">I4s", self._buffer)

            if size == 1:
                if len(self._buffer) < 16:
                    break

                size = struct.unpack_from(">Q", self._buffer, 8)[0]

            if size < HEADER_SIZE:
                raise ValueError(f"invalid box size {size}")

            if len(self._buffer) < size:
                break

            boxes.append((kind.decode("latin-1"), bytes(self._buffer[:size])))
            del self._buffer[:size]

        return boxes


def _children(data: bytes, start: int, end: int) -> Iterator[tuple[str, int, int]]:
    """Yield (type, offset, size) for each box in data[start:end]."""
    offset = start

    while offset + HEADER_SIZE <= end:
        size, kind = struct.unpack_from(">I4s", data, offset)

        if size < HEADER_SIZE or offset + size > end:
            return

        yield kind.decode("latin-1"), offset, size
        offset += size


def _find(data: bytes, start: int, end: int, kind: str) -> tuple[int, int] | None:
    for name, offset, size in _children(data, start, end):
        if name == kind:
            return offset, size

    return None


def video_track_id(init: bytes) -> int | None:
    """The track id of the video track in an init segment, if it has one."""
    moov = _find(init, 0, len(init), "moov")

    if moov is None:
        return None

    for kind, trak, size in _children(init, moov[0] + HEADER_SIZE, sum(moov)):
        if kind != "trak":
            continue

        tkhd = _find(init, trak + HEADER_SIZE, trak + size, "tkhd")
        mdia = _find(init, trak + HEADER_SIZE, trak + size, "mdia")

        if tkhd is None or mdia is None:
            continue

        hdlr = _find(init, mdia[0] + HEADER_SIZE, sum(mdia), "hdlr")

        # the handler type follows the version, flags and a reserved word
        if hdlr is None or init[hdlr[0] + 16 : hdlr[0] + 20] != b"vide":
            continue

        # creation and modification times are 8 bytes each in version 1
        version = init[tkhd[0] + HEADER_SIZE]
        return struct.unpack_from(">I", init, tkhd[0] + (28 if version == 1 else 20))[0]

    return None


def fragment_info(moof: bytes) -> tuple[int, int | None] | None:
    """A fragment's track id and default sample flags, read from its tfhd."""
    traf = _find(moof, HEADER_SIZE, len(moof), "traf")

    if traf is None:
        return None

    tfhd = _find(moof, traf[0] + HEADER_SIZE, sum(traf), "tfhd")

    if tfhd is None:
        return None

    offset = tfhd[0] + HEADER_SIZE
    flags = int.from_bytes(moof[offset + 1 : offset + 4], "big")
    track_id = struct.unpack_from(">I", moof, offset + 4)[0]

    if not flags & _TFHD_DEFAULT_SAMPLE_FLAGS:
        return track_id, None

    offset += 8

    for flag, size in _TFHD_OPTIONAL_FIELDS:
        if flags & flag:
            offset += size

    return track_id, struct.unpack_from(">I", moof, offset)[0]


class Segment(NamedTuple):
    data: bytes
    # both None for the init segment
    track_id: int | None = None
    sample_flags: int | None = None
    init: bool = False


class SegmentReader:
    """Turns go2rtc's byte stream into an init segment and media fragments."""

    def __init__(self) -> None:
        self._boxes = BoxReader()
        self._pending = bytearray()
        self._moof: bytes | None = None

    def feed(self, data: bytes) -> list[Segment]:
        segments: list[Segment] = []

        for kind, box in self._boxes.feed(data):
            self._pending += box

            if kind == "moov":
                segments.append(Segment(bytes(self._pending), init=True))
                self._pending.clear()
            elif kind == "moof":
                self._moof = box
            elif kind == "mdat" and self._moof is not None:
                track_id, sample_flags = fragment_info(self._moof) or (None, None)
                segments.append(Segment(bytes(self._pending), track_id, sample_flags))
                self._pending.clear()
                self._moof = None

        return segments
