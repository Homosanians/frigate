"""Tests for asking a camera for a keyframe over ONVIF."""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from onvif import ONVIFError
from zeep.exceptions import Fault

from frigate.config import FrigateConfig
from frigate.ptz.onvif import OnvifController, OnvifUnavailableError

CAMERA = "fixed_cam"
MEDIA_NAMESPACE = "http://www.onvif.org/ver10/media/wsdl"


def _config(onvif_host: str = "10.0.0.1") -> FrigateConfig:
    return FrigateConfig(
        **{
            "mqtt": {"enabled": False},
            "cameras": {
                CAMERA: {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://10.0.0.1:554/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {"width": 1920, "height": 1080},
                    "onvif": {"host": onvif_host},
                }
            },
        }
    )


def _profile(token: str, video: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        token=token,
        Name=token,
        VideoEncoderConfiguration=SimpleNamespace(Encoding="H264") if video else None,
    )


def _make_media_service() -> MagicMock:
    media = MagicMock()
    media.GetProfiles = AsyncMock(
        return_value=[
            _profile("main"),
            _profile("sub"),
            _profile("audio_only", video=False),
        ]
    )
    media.SetSynchronizationPoint = AsyncMock()
    return media


def _make_controller(
    media: MagicMock, config: FrigateConfig | None = None
) -> OnvifController:
    """Build a controller without invoking __init__, which would start an event loop
    thread and reach out to the camera. The camera has no PTZ service, and its
    session has not read the service addresses yet."""
    onvif = MagicMock()
    onvif.adjust_time = False
    onvif.dt_diff = None
    onvif.xaddrs = {}
    onvif.get_definition = MagicMock(side_effect=Exception("no ptz service"))

    async def update_xaddrs() -> None:
        onvif.xaddrs = {MEDIA_NAMESPACE: "http://10.0.0.1/onvif/media"}

    # the library only knows the media service address once it has read them
    async def create_media_service() -> MagicMock:
        if MEDIA_NAMESPACE not in onvif.xaddrs:
            raise ONVIFError("Device doesn`t support service: media")

        return media

    onvif.update_xaddrs = AsyncMock(side_effect=update_xaddrs)
    onvif.create_media_service = AsyncMock(side_effect=create_media_service)

    controller = OnvifController.__new__(OnvifController)
    controller.config = config or _config()
    controller.cams = {CAMERA: {"onvif": onvif, "init": False}}
    controller.failed_cams = {}
    controller.device_locks = {}
    return controller


def _asked_profiles(media: MagicMock) -> list[str]:
    return [
        call.args[0]["ProfileToken"]
        for call in media.SetSynchronizationPoint.await_args_list
    ]


class TestRequestSyncPoint(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # only the controller's view of the clock, which the event loop reads too
        self.clock = MagicMock(wraps=time)
        self.clock.monotonic.return_value = 1000.0
        clock = patch("frigate.ptz.onvif.time", self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def _advance(self, seconds: float) -> None:
        self.clock.monotonic.return_value += seconds

    async def test_every_video_profile_is_asked(self):
        media = _make_media_service()
        controller = _make_controller(media)

        requested = await controller.request_sync_point(CAMERA)

        self.assertTrue(requested)
        self.assertCountEqual(_asked_profiles(media), ["main", "sub"])

    async def test_camera_without_ptz_is_not_ptz_initialized(self):
        media = _make_media_service()
        controller = _make_controller(media)

        self.assertTrue(await controller.request_sync_point(CAMERA))

        self.assertFalse(controller.cams[CAMERA]["init"])
        controller.cams[CAMERA]["onvif"].create_ptz_service.assert_not_called()

    async def test_request_right_after_another_is_skipped(self):
        media = _make_media_service()
        controller = _make_controller(media)

        self.assertTrue(await controller.request_sync_point(CAMERA))
        self._advance(0.1)
        self.assertFalse(await controller.request_sync_point(CAMERA))

        self.assertCountEqual(_asked_profiles(media), ["main", "sub"])

    async def test_later_request_asks_again_without_rereading_the_camera(self):
        media = _make_media_service()
        controller = _make_controller(media)

        self.assertTrue(await controller.request_sync_point(CAMERA))
        self._advance(3600)
        self.assertTrue(await controller.request_sync_point(CAMERA))

        self.assertCountEqual(_asked_profiles(media), ["main", "sub", "main", "sub"])
        media.GetProfiles.assert_awaited_once()
        controller.cams[CAMERA]["onvif"].update_xaddrs.assert_awaited_once()

    async def test_camera_that_rejects_is_not_asked_again(self):
        media = _make_media_service()
        media.SetSynchronizationPoint.side_effect = Fault("Action not supported")
        controller = _make_controller(media)

        self.assertFalse(await controller.request_sync_point(CAMERA))
        self._advance(3600)
        self.assertFalse(await controller.request_sync_point(CAMERA))

        self.assertCountEqual(_asked_profiles(media), ["main", "sub"])

    async def test_camera_that_rejects_reading_profiles_is_not_asked_again(self):
        media = _make_media_service()
        media.GetProfiles.side_effect = Fault("Sender not authorized")
        controller = _make_controller(media)

        self.assertFalse(await controller.request_sync_point(CAMERA))
        self._advance(3600)
        self.assertFalse(await controller.request_sync_point(CAMERA))

        media.GetProfiles.assert_awaited_once()
        media.SetSynchronizationPoint.assert_not_awaited()

    async def test_one_profile_rejecting_does_not_stop_the_others(self):
        media = _make_media_service()

        async def set_sync_point(request: dict) -> None:
            if request["ProfileToken"] == "sub":
                raise Fault("Profile has no active encoder")

        media.SetSynchronizationPoint.side_effect = set_sync_point
        controller = _make_controller(media)

        self.assertTrue(await controller.request_sync_point(CAMERA))
        self._advance(3600)
        self.assertTrue(await controller.request_sync_point(CAMERA))

        self.assertEqual(_asked_profiles(media).count("main"), 2)

    async def test_unreachable_camera_is_left_alone_for_a_while(self):
        media = _make_media_service()
        media.SetSynchronizationPoint.side_effect = TimeoutError("timed out")
        controller = _make_controller(media)

        self.assertFalse(await controller.request_sync_point(CAMERA))
        # a viewer opening the stream again a few seconds later
        self._advance(5)
        self.assertFalse(await controller.request_sync_point(CAMERA))
        self.assertCountEqual(_asked_profiles(media), ["main", "sub"])

        media.SetSynchronizationPoint.side_effect = None
        self._advance(3600)
        self.assertTrue(await controller.request_sync_point(CAMERA))

    async def test_camera_that_does_not_answer_is_given_up_on(self):
        media = _make_media_service()

        async def never_answers(request: dict) -> None:
            await asyncio.Event().wait()

        media.SetSynchronizationPoint.side_effect = never_answers
        controller = _make_controller(media)

        with patch("frigate.ptz.onvif.SYNC_POINT_TIMEOUT_SECONDS", 0.05):
            requested = await asyncio.wait_for(
                controller.request_sync_point(CAMERA), timeout=5
            )

        self.assertFalse(requested)
        self._advance(5)
        self.assertFalse(await controller.request_sync_point(CAMERA))
        self.assertCountEqual(_asked_profiles(media), ["main", "sub"])

    async def test_camera_with_no_video_profiles_is_not_asked(self):
        media = _make_media_service()
        media.GetProfiles.return_value = [_profile("audio_only", video=False)]
        controller = _make_controller(media)

        self.assertFalse(await controller.request_sync_point(CAMERA))
        self._advance(3600)
        self.assertFalse(await controller.request_sync_point(CAMERA))

        media.GetProfiles.assert_awaited_once()
        media.SetSynchronizationPoint.assert_not_awaited()

    async def test_camera_without_onvif(self):
        media = _make_media_service()
        controller = _make_controller(media, config=_config(onvif_host=""))
        controller.cams = {}

        with self.assertRaises(OnvifUnavailableError):
            await controller.request_sync_point(CAMERA)

        media.SetSynchronizationPoint.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
