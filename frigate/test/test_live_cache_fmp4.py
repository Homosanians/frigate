"""Tests for reading the fragmented MP4 stream go2rtc sends to MSE viewers."""

import struct
import unittest

from frigate.live_cache.fmp4 import (
    BoxReader,
    Segment,
    SegmentReader,
    fragment_info,
    video_track_id,
)
from frigate.test.live_cache_boxes import (
    AUDIO,
    AUDIO_TRACK,
    KEYFRAME,
    NON_KEYFRAME,
    VIDEO_TRACK,
    box,
    fragment,
    init_segment,
    track,
)


def _moof(data: bytes) -> bytes:
    return data[: struct.unpack(">I", data[:4])[0]]


class TestBoxReader(unittest.TestCase):
    def test_boxes_split_across_chunks(self):
        first = box(b"ftyp", b"abcd")
        second = box(b"moov", b"xyz")
        data = first + second
        reader = BoxReader()

        boxes = []
        for i in range(len(data)):
            boxes += reader.feed(data[i : i + 1])

        self.assertEqual(boxes, [("ftyp", first), ("moov", second)])

    def test_several_boxes_in_one_chunk(self):
        first = box(b"moof", b"a")
        second = box(b"mdat", b"bb")
        third = box(b"moof", b"ccc")

        self.assertEqual(
            BoxReader().feed(first + second + third),
            [("moof", first), ("mdat", second), ("moof", third)],
        )

    def test_incomplete_box_is_held_back(self):
        first = box(b"moof", b"a")
        second = box(b"mdat", b"bb")
        reader = BoxReader()

        self.assertEqual(reader.feed(first + second[:5]), [("moof", first)])
        self.assertEqual(reader.feed(second[5:]), [("mdat", second)])

    def test_box_with_a_64_bit_size(self):
        large = struct.pack(">I4sQ", 1, b"mdat", 19) + b"abc"

        self.assertEqual(BoxReader().feed(large + b"\x00"), [("mdat", large)])

    def test_impossible_size_is_an_error(self):
        with self.assertRaises(ValueError):
            BoxReader().feed(struct.pack(">I4s", 4, b"free"))


class TestVideoTrackId(unittest.TestCase):
    def test_video_listed_after_audio(self):
        init = init_segment(track(1, b"soun"), track(2, b"vide"))
        self.assertEqual(video_track_id(init), 2)

    def test_video_listed_first(self):
        init = init_segment(track(1, b"vide"), track(2, b"soun"))
        self.assertEqual(video_track_id(init), 1)

    def test_version_1_track_header(self):
        self.assertEqual(video_track_id(init_segment(track(7, b"vide", version=1))), 7)

    def test_audio_only(self):
        self.assertIsNone(video_track_id(init_segment(track(1, b"soun"))))


class TestFragmentInfo(unittest.TestCase):
    def test_track_and_flags(self):
        self.assertEqual(
            fragment_info(_moof(fragment(VIDEO_TRACK, KEYFRAME))), (1, 0x02000000)
        )
        self.assertEqual(
            fragment_info(_moof(fragment(VIDEO_TRACK, NON_KEYFRAME))), (1, 0x01010000)
        )
        self.assertEqual(
            fragment_info(_moof(fragment(AUDIO_TRACK, AUDIO))), (2, 0x02000000)
        )

    def test_optional_fields_before_the_flags_are_skipped(self):
        # base data offset, sample description index, duration, size, flags
        tfhd = box(
            b"tfhd",
            b"\x00\x00\x00\x3b"
            + struct.pack(">I", 5)
            + b"\x11" * 8
            + b"\x22" * 4
            + b"\x33" * 4
            + b"\x44" * 4
            + struct.pack(">I", 0x01010000),
        )
        moof = box(b"moof", box(b"traf", tfhd))

        self.assertEqual(fragment_info(moof), (5, 0x01010000))

    def test_no_default_flags(self):
        tfhd = box(b"tfhd", b"\x00\x02\x00\x00" + struct.pack(">I", 3))
        moof = box(b"moof", box(b"traf", tfhd))

        self.assertEqual(fragment_info(moof), (3, None))

    def test_no_track_fragment(self):
        self.assertIsNone(fragment_info(box(b"moof", box(b"mfhd", b"\x00" * 8))))


class TestSegmentReader(unittest.TestCase):
    def test_init_then_fragments(self):
        init = init_segment(track(1, b"vide"), track(2, b"soun"))
        key = fragment(VIDEO_TRACK, KEYFRAME, b"key")
        sound = fragment(AUDIO_TRACK, AUDIO, b"sound")

        self.assertEqual(
            SegmentReader().feed(init + key + sound),
            [
                Segment(init, init=True),
                Segment(key, 1, 0x02000000),
                Segment(sound, 2, 0x02000000),
            ],
        )

    def test_init_is_ready_before_any_fragment(self):
        init = init_segment(track(1, b"vide"))
        self.assertEqual(SegmentReader().feed(init), [Segment(init, init=True)])

    def test_fragment_split_across_messages(self):
        init = init_segment(track(1, b"vide"))
        delta = fragment(VIDEO_TRACK, NON_KEYFRAME, b"delta")
        reader = SegmentReader()
        reader.feed(init)

        self.assertEqual(reader.feed(delta[:10]), [])
        self.assertEqual(reader.feed(delta[10:]), [Segment(delta, 1, 0x01010000)])


if __name__ == "__main__":
    unittest.main()
