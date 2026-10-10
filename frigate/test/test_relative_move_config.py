"""Tests for the ONVIF relative move setting."""

import unittest

from pydantic import ValidationError

from frigate.config import FrigateConfig, RelativeMoveConfig, RelativeMoveModeEnum


def _camera_config(relative_move: dict) -> FrigateConfig:
    return FrigateConfig(
        **{
            "mqtt": {"enabled": False},
            "cameras": {
                "ptz_cam": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://10.0.0.1:554/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {"width": 1920, "height": 1080},
                    "onvif": {"host": "10.0.0.1", "relative_move": relative_move},
                }
            },
        }
    )


class TestRelativeMoveConfig(unittest.TestCase):
    def test_defaults_to_field_of_view_moves(self) -> None:
        config = RelativeMoveConfig()

        self.assertEqual(config.mode, RelativeMoveModeEnum.fov)
        self.assertIsNone(config.pan_scale)
        self.assertIsNone(config.tilt_scale)

    def test_generic_mode_with_scales(self) -> None:
        config = RelativeMoveConfig(mode="generic", pan_scale=-0.24, tilt_scale=-0.27)

        self.assertEqual(config.mode, RelativeMoveModeEnum.generic)
        self.assertEqual((config.pan_scale, config.tilt_scale), (-0.24, -0.27))

    def test_generic_mode_needs_both_scales(self) -> None:
        for scales in ({}, {"pan_scale": -0.24}, {"tilt_scale": -0.27}):
            with self.subTest(scales=scales):
                with self.assertRaises(ValidationError):
                    RelativeMoveConfig(mode="generic", **scales)

    def test_zero_scale_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RelativeMoveConfig(mode="generic", pan_scale=0, tilt_scale=-0.27)

    def test_scale_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RelativeMoveConfig(mode="generic", pan_scale=-2.5, tilt_scale=-0.27)

    def test_field_of_view_mode_keeps_scales(self) -> None:
        # switching back to fov must not mean deleting the measured scales
        config = RelativeMoveConfig(pan_scale=-0.24, tilt_scale=-0.27)

        self.assertEqual(config.mode, RelativeMoveModeEnum.fov)
        self.assertEqual(config.pan_scale, -0.24)

    def test_camera_config(self) -> None:
        config = _camera_config(
            {"mode": "generic", "pan_scale": -0.24, "tilt_scale": -0.27}
        )

        relative_move = config.cameras["ptz_cam"].onvif.relative_move
        self.assertEqual(relative_move.mode, RelativeMoveModeEnum.generic)
        self.assertEqual(relative_move.tilt_scale, -0.27)

    def test_camera_without_the_setting(self) -> None:
        config = _camera_config({})

        self.assertEqual(
            config.cameras["ptz_cam"].onvif.relative_move.mode,
            RelativeMoveModeEnum.fov,
        )
