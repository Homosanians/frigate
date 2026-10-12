"""Tests for the live stream cache config option."""

import unittest

from frigate.config import FrigateConfig
from frigate.live_cache.process import cached_streams


def _camera(**extra) -> dict:
    return {
        "ffmpeg": {
            "inputs": [{"path": "rtsp://10.0.0.1:554/video", "roles": ["detect"]}]
        },
        "detect": {"height": 1080, "width": 1920, "fps": 5},
        **extra,
    }


def _config(live: dict | None = None, **cameras: dict) -> FrigateConfig:
    config = {"mqtt": {"enabled": False}, "cameras": cameras}

    if live is not None:
        config["live"] = live

    return FrigateConfig(**config)


class TestLiveCacheConfig(unittest.TestCase):
    def test_off_by_default(self):
        config = _config(front=_camera())

        self.assertFalse(config.cameras["front"].live.cache.enabled)
        self.assertEqual(cached_streams(config), set())

    def test_global_setting_reaches_every_camera(self):
        config = _config({"cache": {"enabled": True}}, front=_camera(), back=_camera())

        self.assertTrue(config.cameras["front"].live.cache.enabled)
        self.assertEqual(cached_streams(config), {"front", "back"})

    def test_camera_can_opt_out(self):
        config = _config(
            {"cache": {"enabled": True}},
            front=_camera(),
            back=_camera(live={"cache": {"enabled": False}}),
        )

        self.assertEqual(cached_streams(config), {"front"})

    def test_every_live_stream_of_a_camera_is_cached(self):
        config = _config(
            front=_camera(
                live={
                    "cache": {"enabled": True},
                    "streams": {"Main": "front_main", "Sub": "front_sub"},
                }
            ),
            back=_camera(),
        )

        self.assertEqual(cached_streams(config), {"front_main", "front_sub"})

    def test_transcoded_streams_are_left_out(self):
        # they only run while someone watches, which the cache would prolong
        config = FrigateConfig(
            mqtt={"enabled": False},
            go2rtc={"streams": {"front": ["rtsp://10.0.0.1:554/video"]}},
            cameras={
                "front": _camera(
                    live={"cache": {"enabled": True}, "transcode": {"enabled": True}}
                )
            },
        )

        self.assertIn(
            "front_transcode_720p", config.cameras["front"].live.streams.values()
        )
        self.assertEqual(cached_streams(config), {"front"})

    def test_disabled_camera_is_left_out(self):
        config = _config(
            {"cache": {"enabled": True}}, front=_camera(enabled=False), back=_camera()
        )

        self.assertEqual(cached_streams(config), {"back"})


if __name__ == "__main__":
    unittest.main()
