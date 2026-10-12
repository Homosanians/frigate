"""Tests for the frames kept for new live viewers."""

import unittest

from frigate.live_cache.cache import GopCache
from frigate.test.live_cache_boxes import (
    AUDIO,
    AUDIO_TRACK,
    KEYFRAME,
    NON_KEYFRAME,
    VIDEO_TRACK,
    fragment,
    init_segment,
    track,
)

INIT = init_segment(track(1, b"vide"), track(2, b"soun"))
KEY = fragment(VIDEO_TRACK, KEYFRAME, b"key")
KEY_2 = fragment(VIDEO_TRACK, KEYFRAME, b"key2")
DELTA = fragment(VIDEO_TRACK, NON_KEYFRAME, b"delta")
SOUND = fragment(AUDIO_TRACK, AUDIO, b"sound")
BIG_KEY = fragment(VIDEO_TRACK, KEYFRAME, b"k" * 400)
BIG_DELTA = fragment(VIDEO_TRACK, NON_KEYFRAME, b"d" * 400)


class TestGopCache(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.cache = GopCache(max_bytes=1200, max_seconds=10, clock=lambda: self.now)
        self.cache.add_init(INIT)

    def add(self, data: bytes, track_id: int, flags: int) -> bool:
        return self.cache.add_fragment(data, track_id, flags)

    def test_backlog_starts_at_the_keyframe(self):
        self.add(DELTA, 1, NON_KEYFRAME)
        self.assertEqual(self.cache.backlog(), [])

        self.add(KEY, 1, KEYFRAME)
        self.add(DELTA, 1, NON_KEYFRAME)
        self.assertEqual(self.cache.backlog(), [KEY, DELTA])

    def test_new_keyframe_replaces_the_backlog(self):
        self.add(KEY, 1, KEYFRAME)
        self.add(DELTA, 1, NON_KEYFRAME)
        self.add(KEY_2, 1, KEYFRAME)

        self.assertEqual(self.cache.backlog(), [KEY_2])

    def test_audio_fragment_does_not_reset_the_backlog(self):
        self.add(KEY, 1, KEYFRAME)
        self.add(SOUND, 2, AUDIO)
        self.add(DELTA, 1, NON_KEYFRAME)

        self.assertEqual(self.cache.backlog(), [KEY, SOUND, DELTA])

    def test_only_video_keyframes_are_reported(self):
        self.assertTrue(self.add(KEY, 1, KEYFRAME))
        self.assertFalse(self.add(DELTA, 1, NON_KEYFRAME))
        self.assertFalse(self.add(SOUND, 2, AUDIO))

    def test_fragment_without_flags_is_not_a_keyframe(self):
        self.assertFalse(self.cache.add_fragment(KEY, 1, None))
        self.assertEqual(self.cache.backlog(), [])

    def test_backlog_over_the_size_limit_waits_for_the_next_keyframe(self):
        self.add(BIG_KEY, 1, KEYFRAME)
        self.add(BIG_DELTA, 1, NON_KEYFRAME)
        self.assertEqual(self.cache.backlog(), [BIG_KEY, BIG_DELTA])

        self.add(BIG_DELTA, 1, NON_KEYFRAME)
        self.assertEqual(self.cache.backlog(), [])

        self.add(DELTA, 1, NON_KEYFRAME)
        self.assertEqual(self.cache.backlog(), [])

        self.add(KEY_2, 1, KEYFRAME)
        self.assertEqual(self.cache.backlog(), [KEY_2])

    def test_backlog_over_the_time_limit_waits_for_the_next_keyframe(self):
        self.add(KEY, 1, KEYFRAME)
        self.now += 11
        self.add(DELTA, 1, NON_KEYFRAME)

        self.assertEqual(self.cache.backlog(), [])

    def test_backlog_of_a_stream_that_went_silent_is_not_served(self):
        self.add(KEY, 1, KEYFRAME)
        self.now += 0.5
        self.add(DELTA, 1, NON_KEYFRAME)

        self.now += 3
        self.assertEqual(self.cache.backlog(), [])

        # frames that pick up again make it current
        self.add(DELTA, 1, NON_KEYFRAME)
        self.assertEqual(self.cache.backlog(), [KEY, DELTA, DELTA])

    def test_default_limit_stays_inside_what_the_player_keeps(self):
        # the player trims its buffer to 15 seconds, which would cut the
        # keyframe off a longer backlog
        cache = GopCache(clock=lambda: self.now)
        cache.add_init(INIT)
        cache.add_fragment(KEY, 1, KEYFRAME)

        self.now += 9
        cache.add_fragment(DELTA, 1, NON_KEYFRAME)
        self.assertEqual(cache.backlog(), [KEY, DELTA])

        self.now += 2
        cache.add_fragment(DELTA, 1, NON_KEYFRAME)
        self.assertEqual(cache.backlog(), [])

    def test_new_init_segment_clears_the_backlog(self):
        self.add(KEY, 1, KEYFRAME)
        self.cache.add_init(INIT)

        self.assertEqual(self.cache.backlog(), [])

    def test_audio_only_stream_keeps_no_backlog(self):
        cache = GopCache()
        cache.add_init(init_segment(track(1, b"soun")))

        self.assertFalse(cache.has_video)
        self.assertFalse(cache.add_fragment(SOUND, 1, AUDIO))
        self.assertEqual(cache.backlog(), [])


if __name__ == "__main__":
    unittest.main()
