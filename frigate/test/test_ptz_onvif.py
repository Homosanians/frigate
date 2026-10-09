"""Tests for ONVIF state that must not depend on the autotracking config.

Regression coverage for a camera that is initialized while autotracking is off and
has it enabled later, which is the normal wizard flow: set the camera up first,
configure autotracking afterwards. get_camera_status skips its re-init branch when
init is True, so everything it reads must exist whether or not autotracking was
enabled at init time.

Also covers the inverse direction: the ptz movement timestamps must not be written
for a camera that has autotracking off, because nothing clears them back out.
"""

import asyncio
import json
import threading
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from onvif import ONVIFError

from frigate.camera import PTZMetrics
from frigate.config import FrigateConfig
from frigate.ptz.autotrack import ptz_moving_at_frame_time
from frigate.ptz.event_log import PtzEventLog, PtzSource
from frigate.ptz.onvif import (
    OnvifCommandEnum,
    OnvifController,
    OnvifRequestError,
    OnvifUnavailableError,
    describe_relative_spaces,
    describe_status,
)

CAMERA = "ptz_cam"


def _config(autotracking_enabled: bool) -> FrigateConfig:
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
                    "zones": {"zone": {"coordinates": "0,0,1,0,1,1,0,1"}},
                    "onvif": {
                        "host": "10.0.0.1",
                        "autotracking": {
                            "enabled": autotracking_enabled,
                            "required_zones": ["zone"],
                        },
                    },
                }
            },
        }
    )


def _make_profile() -> MagicMock:
    profile = MagicMock()
    profile.token = "profile_1"
    profile.Name = "MainStream"
    profile.VideoEncoderConfiguration = MagicMock()
    ptz_config = MagicMock()
    ptz_config.token = "ptz_config_1"
    ptz_config.DefaultContinuousPanTiltVelocitySpace = "space"
    ptz_config.DefaultContinuousZoomVelocitySpace = "space"
    profile.PTZConfiguration = ptz_config
    return profile


def _make_onvif_camera() -> MagicMock:
    """A camera that supports PTZ but nothing optional, so init takes the simplest
    path through the feature detection below."""
    onvif = MagicMock()
    onvif.update_xaddrs = AsyncMock()

    video_source = MagicMock()
    video_source.token = "video_source_1"

    media = MagicMock()
    media.GetProfiles = AsyncMock(return_value=[_make_profile()])
    media.GetVideoSources = AsyncMock(return_value=[video_source])
    onvif.create_media_service = AsyncMock(return_value=media)
    onvif.get_definition = MagicMock(return_value={"ptz": "definition"})

    ptz = MagicMock()
    # create_type is a local WSDL lookup, so tag the result to assert on it later
    ptz.create_type = MagicMock(side_effect=lambda name: MagicMock(request_type=name))
    ptz.GetConfigurationOptions = AsyncMock(side_effect=Exception("not supported"))
    onvif.create_ptz_service = AsyncMock(return_value=ptz)
    onvif.create_imaging_service = AsyncMock(side_effect=Exception("not supported"))
    return onvif


def _make_controller(autotracking_enabled: bool) -> OnvifController:
    """Build a controller without invoking __init__, which would start an event loop
    thread and reach out to the camera."""
    config = _config(autotracking_enabled)
    controller = OnvifController.__new__(OnvifController)
    controller.config = config
    controller.cams = {CAMERA: {"onvif": _make_onvif_camera(), "init": False}}
    controller.failed_cams = {}
    controller.device_locks = {}
    controller.time_sync_tasks = {}
    controller.ptz_metrics = {CAMERA: MagicMock()}
    return controller


def _make_move_controller(autotracking_enabled: bool) -> OnvifController:
    """Build an already initialized controller for a camera that supports relative
    FOV movement, with real metrics so the timestamp writes can be asserted on."""
    config = _config(autotracking_enabled)
    controller = OnvifController.__new__(OnvifController)
    controller.config = config
    controller.failed_cams = {}
    controller.device_locks = {}
    controller.time_sync_tasks = {}

    ptz = MagicMock()
    ptz.RelativeMove = AsyncMock()
    controller.cams = {
        CAMERA: {
            "init": True,
            "active": False,
            "ptz": ptz,
            "features": ["pt", "pt-r-fov"],
            "relative_move_request": MagicMock(),
            "relative_fov_range": {
                "XRange": {"Min": -1.0, "Max": 1.0},
                "YRange": {"Min": -1.0, "Max": 1.0},
            },
        }
    }
    controller.ptz_metrics = {CAMERA: PTZMetrics()}
    return controller


class TestOnvifInitRequests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_status_independent_of_autotracking_at_init(self) -> None:
        # the wizard flow: onvif configured first, autotracking enabled later
        for autotracking_enabled in (True, False):
            with self.subTest(autotracking_enabled=autotracking_enabled):
                controller = _make_controller(autotracking_enabled)
                controller.status_locks = {CAMERA: asyncio.Lock()}

                self.assertTrue(await controller._init_onvif(CAMERA))

                status = MagicMock()
                status.MoveStatus.PanTilt = "IDLE"
                status.MoveStatus.Zoom = "IDLE"
                ptz = controller.cams[CAMERA]["ptz"]
                ptz.GetStatus = AsyncMock(return_value=status)

                await controller.get_camera_status(CAMERA, source=PtzSource.autotrack)

                ptz.GetStatus.assert_awaited_once_with({"ProfileToken": "profile_1"})
                self.assertFalse(controller.cams[CAMERA]["active"])

    async def test_requests_built_without_contacting_camera(self) -> None:
        # create_type is a local WSDL lookup; cameras that do not implement
        # GetServiceCapabilities must not be asked about it during init
        controller = _make_controller(autotracking_enabled=False)

        await controller._init_onvif(CAMERA)

        ptz = controller.cams[CAMERA]["ptz"]
        ptz.GetServiceCapabilities.assert_not_called()
        ptz.GetStatus.assert_not_called()


class TestManualRelativeMoveMetrics(unittest.IsolatedAsyncioTestCase):
    """A manual move from the UI (click to move, drag to zoom) sends move_relative
    for any camera that advertises pt-r-fov, autotracking or not."""

    async def test_metrics_untouched_when_autotracking_disabled(self) -> None:
        # only camera_maintenance polls get_camera_status, and only for autotracking
        # cameras, so a manual move that starts the clock here is never stopped
        controller = _make_move_controller(autotracking_enabled=False)
        metrics = controller.ptz_metrics[CAMERA]
        metrics.frame_time.value = 1000.0

        await controller._move_relative(
            CAMERA, 0.25, -0.25, 0, 1, source=PtzSource.command
        )

        controller.cams[CAMERA]["ptz"].RelativeMove.assert_awaited_once()
        self.assertEqual(metrics.start_time.value, 0)
        self.assertEqual(metrics.stop_time.value, 0)
        self.assertTrue(metrics.motor_stopped.is_set())

    async def test_detection_regions_not_suppressed_after_manual_move(self) -> None:
        # the symptom of the bug: object detection stops entirely because motion
        # boxes are never promoted to detection regions again
        controller = _make_move_controller(autotracking_enabled=False)
        metrics = controller.ptz_metrics[CAMERA]
        metrics.frame_time.value = 1000.0

        await controller._move_relative(
            CAMERA, 0.25, -0.25, 0, 1, source=PtzSource.command
        )

        for later_frame_time in (1001.0, 1060.0, 4600.0):
            with self.subTest(frame_time=later_frame_time):
                self.assertFalse(
                    ptz_moving_at_frame_time(
                        later_frame_time,
                        metrics.start_time.value,
                        metrics.stop_time.value,
                    )
                )

    async def test_metrics_written_when_autotracking_enabled(self) -> None:
        # get_camera_status resets stop_time once the camera reports IDLE, so the
        # autotracking path keeps its motion estimation timestamps
        controller = _make_move_controller(autotracking_enabled=True)
        metrics = controller.ptz_metrics[CAMERA]
        metrics.frame_time.value = 1000.0

        with patch("frigate.ptz.onvif.time.time", return_value=1000.1):
            await controller._move_relative(
                CAMERA, 0.25, -0.25, 0, 1, source=PtzSource.command
            )

        self.assertEqual(metrics.start_time.value, 1000.1)
        self.assertEqual(metrics.stop_time.value, 0)
        self.assertFalse(metrics.motor_stopped.is_set())
        self.assertTrue(
            ptz_moving_at_frame_time(
                1001.0, metrics.start_time.value, metrics.stop_time.value
            )
        )


class TestMoveTimestamps(unittest.IsolatedAsyncioTestCase):
    """Frames are stamped when Frigate receives them. The newest frame can be a
    frame interval or more older than the moment the move starts or stops, so
    the move is timed with the clock, not with the newest frame time."""

    async def test_move_start_uses_the_clock(self) -> None:
        controller = _make_move_controller(autotracking_enabled=True)
        metrics = controller.ptz_metrics[CAMERA]
        metrics.frame_time.value = 1000.0

        with patch("frigate.ptz.onvif.time.time", return_value=1000.15):
            await controller._move_relative(
                CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
            )

        self.assertEqual(metrics.start_time.value, 1000.15)

    async def test_move_reports_whether_it_was_sent(self) -> None:
        controller = _make_move_controller(autotracking_enabled=True)

        self.assertTrue(
            await controller._move_relative(
                CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
            )
        )

        # a camera that is still busy with another move is not sent the command
        controller.cams[CAMERA]["active"] = True
        self.assertFalse(
            await controller._move_relative(
                CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
            )
        )
        controller.cams[CAMERA]["ptz"].RelativeMove.assert_awaited_once()

    async def test_failed_move_does_not_leave_the_camera_busy(self) -> None:
        # otherwise every later move is refused as already in progress
        controller = _make_move_controller(autotracking_enabled=True)
        controller.cams[CAMERA]["ptz"].RelativeMove = AsyncMock(
            side_effect=ConnectionError("camera unreachable")
        )

        with self.assertRaises(ConnectionError):
            await controller._move_relative(
                CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
            )

        self.assertFalse(controller.cams[CAMERA]["active"])

    async def test_move_clears_the_previous_video_stop(self) -> None:
        # the camera process sets it again once the video shows this move ended
        controller = _make_move_controller(autotracking_enabled=True)
        metrics = controller.ptz_metrics[CAMERA]
        metrics.video_stop_time.value = 999.5

        await controller._move_relative(CAMERA, 0.25, 0, 0, 1, source=PtzSource.command)

        self.assertEqual(metrics.video_stop_time.value, 0)

    async def test_move_publishes_the_distance_to_expect_in_the_video(self) -> None:
        controller = _make_move_controller(autotracking_enabled=True)
        metrics = controller.ptz_metrics[CAMERA]

        await controller._move_relative(
            CAMERA, 0.25, -0.1, 0, 1, source=PtzSource.command
        )

        self.assertEqual(metrics.move_pan.value, 0.25)
        self.assertEqual(metrics.move_tilt.value, -0.1)

    async def test_move_stop_uses_the_clock(self) -> None:
        controller = _make_move_controller(autotracking_enabled=True)
        controller.status_locks = {CAMERA: asyncio.Lock()}
        controller.cams[CAMERA]["move_request"] = MagicMock(ProfileToken="profile")
        status = MagicMock()
        status.MoveStatus.PanTilt = "IDLE"
        status.MoveStatus.Zoom = "IDLE"
        controller.cams[CAMERA]["ptz"].GetStatus = AsyncMock(return_value=status)

        metrics = controller.ptz_metrics[CAMERA]
        metrics.start_time.value = 1000.0
        metrics.motor_stopped.clear()
        # the newest frame was received before the camera reported IDLE
        metrics.frame_time.value = 1001.0

        with patch("frigate.ptz.onvif.time.time", return_value=1001.18):
            await controller.get_camera_status(CAMERA, source=PtzSource.autotrack)

        self.assertTrue(metrics.motor_stopped.is_set())
        self.assertEqual(metrics.stop_time.value, 1001.18)


class _Preset(dict):
    """zeep preset objects support both item and attribute access."""

    def __init__(self, token: str, name: str) -> None:
        super().__init__(token=token)
        self.Name = name


def _make_node(
    home_supported: bool = True, fixed_home: bool | None = None
) -> SimpleNamespace:
    # SimpleNamespace rather than MagicMock, whose missing attributes are truthy
    node = SimpleNamespace(
        token="node_1", HomeSupported=home_supported, MaximumNumberOfPresets=8
    )
    if fixed_home is not None:
        node.FixedHomePosition = fixed_home
    return node


async def _make_preset_controller(
    presets: list[_Preset], node: SimpleNamespace | None = None
) -> OnvifController:
    """An initialized controller whose camera serves presets like a real one:
    SetPreset and RemovePreset change what GetPresets returns next."""
    controller = _make_controller(autotracking_enabled=False)
    controller.cams[CAMERA].update(
        {"presets": {}, "preset_details": [], "max_presets": None}
    )
    ptz = controller.cams[CAMERA]["onvif"].create_ptz_service.return_value
    stored = list(presets)

    async def set_preset(request: dict) -> str:
        token = request.get("PresetToken")
        if token is None:
            token = f"new_{len(stored)}"
            stored.append(_Preset(token, request["PresetName"]))
        else:
            index = next(i for i, p in enumerate(stored) if p["token"] == token)
            stored[index] = _Preset(token, request["PresetName"])
        return token

    async def remove_preset(request: dict) -> None:
        stored[:] = [p for p in stored if p["token"] != request["PresetToken"]]

    ptz.GetPresets = AsyncMock(side_effect=lambda _: list(stored))
    ptz.SetPreset = AsyncMock(side_effect=set_preset)
    ptz.RemovePreset = AsyncMock(side_effect=remove_preset)
    ptz.SetHomePosition = AsyncMock()
    ptz.GotoHomePosition = AsyncMock()
    ptz.GetNodes = AsyncMock(return_value=[node or _make_node()])

    assert await controller._init_onvif(CAMERA)
    return controller


class TestPresetManagement(unittest.IsolatedAsyncioTestCase):
    async def test_presets_loaded_with_original_names(self) -> None:
        controller = await _make_preset_controller(
            [_Preset("1", "Driveway"), _Preset("2", "Gate")]
        )
        cam = controller.cams[CAMERA]

        self.assertEqual(cam["presets"], {"driveway": "1", "gate": "2"})
        self.assertEqual(
            cam["preset_details"],
            [{"token": "1", "name": "Driveway"}, {"token": "2", "name": "Gate"}],
        )
        self.assertEqual(cam["max_presets"], 8)

    async def test_create_preset_refreshes_cache(self) -> None:
        controller = await _make_preset_controller([_Preset("1", "Driveway")])
        ptz = controller.cams[CAMERA]["ptz"]

        token = await controller.set_preset(CAMERA, "  Porch ")

        ptz.SetPreset.assert_awaited_once_with(
            {"ProfileToken": "profile_1", "PresetName": "Porch"}
        )
        self.assertEqual(token, "new_1")
        self.assertEqual(controller.cams[CAMERA]["presets"]["porch"], "new_1")

    async def test_overwrite_keeps_name_and_sends_token(self) -> None:
        controller = await _make_preset_controller([_Preset("1", "Driveway")])
        ptz = controller.cams[CAMERA]["ptz"]

        token = await controller.set_preset(CAMERA, None, "1")

        ptz.SetPreset.assert_awaited_once_with(
            {"ProfileToken": "profile_1", "PresetName": "Driveway", "PresetToken": "1"}
        )
        self.assertEqual(token, "1")

    async def test_overwrite_with_new_name(self) -> None:
        controller = await _make_preset_controller([_Preset("1", "Driveway")])

        await controller.set_preset(CAMERA, "Garage", "1")

        self.assertEqual(controller.cams[CAMERA]["presets"], {"garage": "1"})

    async def test_rejected_without_contacting_camera(self) -> None:
        controller = await _make_preset_controller(
            [_Preset("1", "Driveway"), _Preset("2", "Gate")]
        )
        ptz = controller.cams[CAMERA]["ptz"]

        for name, token in (
            ("driveway", None),  # duplicate, recalled by lowercase name
            ("Gate", "1"),  # renaming onto another preset
            ("   ", None),
            ("Porch", "missing"),
        ):
            with self.subTest(name=name, token=token):
                with self.assertRaises(OnvifRequestError):
                    await controller.set_preset(CAMERA, name, token)

        with self.assertRaises(OnvifRequestError):
            await controller.remove_preset(CAMERA, "missing")

        ptz.SetPreset.assert_not_awaited()
        ptz.RemovePreset.assert_not_awaited()

    async def test_camera_fault_propagates(self) -> None:
        controller = await _make_preset_controller([])
        controller.cams[CAMERA]["ptz"].SetPreset.side_effect = Exception("full")

        with self.assertRaisesRegex(Exception, "full"):
            await controller.set_preset(CAMERA, "Porch")

    async def test_remove_preset_refreshes_cache(self) -> None:
        controller = await _make_preset_controller(
            [_Preset("1", "Driveway"), _Preset("2", "Gate")]
        )

        await controller.remove_preset(CAMERA, "1")

        controller.cams[CAMERA]["ptz"].RemovePreset.assert_awaited_once_with(
            {"ProfileToken": "profile_1", "PresetToken": "1"}
        )
        self.assertEqual(controller.cams[CAMERA]["presets"], {"gate": "2"})

    async def test_unconfigured_camera(self) -> None:
        controller = await _make_preset_controller([])

        with self.assertRaises(OnvifUnavailableError):
            await controller.set_preset("other_cam", "Porch")

    async def test_home_features(self) -> None:
        for node, expected in (
            (_make_node(home_supported=True), ["home", "home-set"]),
            (_make_node(home_supported=True, fixed_home=True), ["home"]),
            (_make_node(home_supported=False), []),
        ):
            with self.subTest(node=node):
                controller = await _make_preset_controller([], node)
                features = controller.cams[CAMERA]["features"]
                self.assertEqual(
                    [f for f in features if f.startswith("home")], expected
                )

    async def test_home_commands(self) -> None:
        controller = await _make_preset_controller([])
        controller.ptz_metrics = {CAMERA: PTZMetrics()}
        ptz = controller.cams[CAMERA]["ptz"]

        await controller.set_home(CAMERA)
        await controller.handle_command_async(CAMERA, OnvifCommandEnum.home)

        ptz.SetHomePosition.assert_awaited_once_with({"ProfileToken": "profile_1"})
        ptz.GotoHomePosition.assert_awaited_once_with({"ProfileToken": "profile_1"})
        self.assertFalse(controller.cams[CAMERA]["active"])


def _make_no_ptz_controller(no_ptz_service: bool) -> OnvifController:
    """A fixed camera configured for ONVIF, for its device info and time sync.
    Its device either has no PTZ service, or has one that no media profile uses."""
    controller = _make_controller(autotracking_enabled=False)
    controller.max_retries = 5
    controller.reset_timeout = 900
    controller.status_locks = {CAMERA: asyncio.Lock()}
    controller.ptz_metrics = {CAMERA: PTZMetrics()}
    # the state _init_single_camera creates
    controller.cams[CAMERA].update(
        {
            "active": False,
            "features": [],
            "presets": {},
            "preset_details": [],
            "max_presets": None,
            "profiles": [],
        }
    )
    onvif = controller.cams[CAMERA]["onvif"]

    if no_ptz_service:
        onvif.get_definition.side_effect = ONVIFError(
            "Device doesn`t support service: ptz"
        )
    else:
        profile = _make_profile()
        profile.PTZConfiguration = None
        onvif.create_media_service.return_value.GetProfiles = AsyncMock(
            return_value=[profile]
        )

    return controller


class TestCameraWithoutPtz(unittest.IsolatedAsyncioTestCase):
    """The live view requests PTZ info for every camera with an ONVIF host, so a
    camera without PTZ must be reported as such instead of failing to initialize
    and being retried with errors on every visit."""

    async def test_info_reports_no_features(self) -> None:
        for no_ptz_service in (True, False):
            with self.subTest(no_ptz_service=no_ptz_service):
                controller = _make_no_ptz_controller(no_ptz_service)

                with self.assertNoLogs("frigate.ptz.onvif", level="WARNING"):
                    info = await controller.get_camera_info(CAMERA)

                self.assertEqual(
                    info,
                    {
                        "name": CAMERA,
                        "features": [],
                        "presets": [],
                        "preset_details": [],
                        "max_presets": None,
                        "profiles": [],
                    },
                )
                self.assertEqual(controller.failed_cams, {})
                onvif = controller.cams[CAMERA]["onvif"]
                onvif.create_ptz_service.assert_not_awaited()

    async def test_not_retried(self) -> None:
        for no_ptz_service in (True, False):
            with self.subTest(no_ptz_service=no_ptz_service):
                controller = _make_no_ptz_controller(no_ptz_service)

                for _ in range(controller.max_retries + 1):
                    info = await controller.get_camera_info(CAMERA)
                    self.assertEqual(info.get("features"), [])

                controller.cams[CAMERA]["onvif"].update_xaddrs.assert_awaited_once()

    async def test_ptz_commands_ignored(self) -> None:
        for no_ptz_service in (True, False):
            with self.subTest(no_ptz_service=no_ptz_service):
                controller = _make_no_ptz_controller(no_ptz_service)
                await controller.get_camera_info(CAMERA)

                with self.assertNoLogs("frigate.ptz.onvif", level="WARNING"):
                    for command in OnvifCommandEnum:
                        await controller.handle_command_async(CAMERA, command)

                controller.cams[CAMERA]["onvif"].update_xaddrs.assert_awaited_once()

    async def test_preset_management_rejected(self) -> None:
        for no_ptz_service in (True, False):
            with self.subTest(no_ptz_service=no_ptz_service):
                controller = _make_no_ptz_controller(no_ptz_service)

                with self.assertRaises(OnvifRequestError):
                    await controller.set_preset(CAMERA, "Porch")

                with self.assertRaises(OnvifRequestError):
                    await controller.remove_preset(CAMERA, "1")

                with self.assertRaises(OnvifRequestError):
                    await controller.set_home(CAMERA)

    async def test_camera_status_without_ptz(self) -> None:
        for no_ptz_service in (True, False):
            with self.subTest(no_ptz_service=no_ptz_service):
                controller = _make_no_ptz_controller(no_ptz_service)
                await controller.get_camera_info(CAMERA)

                with self.assertNoLogs("frigate.ptz.onvif", level="WARNING"):
                    await controller.get_camera_status(
                        CAMERA, source=PtzSource.autotrack
                    )

                metrics = controller.ptz_metrics[CAMERA]
                self.assertTrue(metrics.motor_stopped.is_set())

    async def test_debug_snapshot_without_ptz(self) -> None:
        # the PTZ log in the Debug view polls every camera with an ONVIF host
        for no_ptz_service in (True, False):
            with self.subTest(no_ptz_service=no_ptz_service):
                controller = _make_no_ptz_controller(no_ptz_service)
                controller.debug_reads = set()
                controller.event_log = PtzEventLog()
                await controller.get_camera_info(CAMERA)
                controller._ptz_request = AsyncMock()

                snapshot = await controller.debug_snapshot(CAMERA, 0)

                self.assertTrue(snapshot["connected"])
                self.assertEqual(snapshot["capabilities"]["features"], [])
                self.assertIsNone(snapshot["status"])
                controller._ptz_request.assert_not_called()


class TestOnvifClose(unittest.TestCase):
    """close() must release everything on the loop, since whatever it leaves is
    garbage collected during interpreter shutdown, where the resulting warnings
    fail to log and fill the shutdown output with logging errors."""

    def setUp(self) -> None:
        self.controller = _make_controller(autotracking_enabled=False)
        self.onvif = self.controller.cams[CAMERA]["onvif"]
        self.onvif.close = AsyncMock()
        self.controller.config_subscriber = MagicMock()
        self.controller.loop = asyncio.new_event_loop()
        self.controller.loop_thread = threading.Thread(
            target=self.controller._run_event_loop, daemon=True
        )
        self.controller.loop_thread.start()
        self.addCleanup(self.controller.loop.close)

    def test_close_closes_camera_sessions(self) -> None:
        self.controller.close()

        self.onvif.close.assert_awaited_once()

    def test_close_cancels_tasks_left_on_the_loop(self) -> None:
        async def forever() -> None:
            while True:
                await asyncio.sleep(1)

        poll = asyncio.run_coroutine_threadsafe(forever(), self.controller.loop)

        self.controller.close()

        self.assertTrue(poll.cancelled())
        self.assertFalse(self.controller.loop_thread.is_alive())


def _watch(controller: OnvifController) -> PtzEventLog:
    """Open the PTZ log for the test camera, as the Debug view does."""
    controller.event_log = PtzEventLog()
    controller.event_log.watch(CAMERA)
    return controller.event_log


def _logged(log: PtzEventLog) -> list[dict]:
    return log.entries(CAMERA, 0)[0]


def _status(pan_tilt: str = "IDLE") -> SimpleNamespace:
    return SimpleNamespace(
        MoveStatus=SimpleNamespace(PanTilt=pan_tilt, Zoom="IDLE"),
        Position=SimpleNamespace(
            PanTilt=SimpleNamespace(x=0.25, y=-0.5), Zoom=SimpleNamespace(x=0.0)
        ),
    )


class TestPtzRequestLog(unittest.IsolatedAsyncioTestCase):
    async def test_relative_move_logs_the_values_sent(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        controller.cams[CAMERA]["relative_fov_range"] = {
            "XRange": {"Min": -0.5, "Max": 0.5},
            "YRange": {"Min": -2.0, "Max": 2.0},
        }
        log = _watch(controller)

        await controller._move_relative(
            CAMERA, 0.5, -0.25, 0, 1, source=PtzSource.autotrack
        )

        (entry,) = _logged(log)
        data = entry["data"]
        self.assertEqual(entry["kind"], "request")
        self.assertEqual(entry["source"], "autotrack")
        self.assertEqual(data["operation"], "RelativeMove")
        self.assertEqual(data["space"], "fov")
        self.assertEqual((data["pan"], data["tilt"]), (0.5, -0.25))
        # the request object is zeroed after sending, the log keeps what was sent
        self.assertEqual((data["x"], data["y"]), (0.25, -0.5))
        self.assertIsNone(data["error"])
        self.assertIsInstance(data["duration_ms"], int)
        json.dumps(entry, allow_nan=False)

    async def test_unbounded_relative_range_logs_no_infinities(self) -> None:
        # ONVIF lets a camera report its range as INF, which JSON cannot carry
        controller = _make_move_controller(autotracking_enabled=False)
        controller.cams[CAMERA]["relative_fov_range"] = {
            "XRange": {"Min": float("-inf"), "Max": float("inf")},
            "YRange": {"Min": float("-inf"), "Max": float("inf")},
        }
        log = _watch(controller)

        await controller._move_relative(
            CAMERA, 0.5, -0.25, 0, 1, source=PtzSource.autotrack
        )

        controller.cams[CAMERA]["ptz"].RelativeMove.assert_awaited_once()
        (entry,) = _logged(log)
        data = entry["data"]
        self.assertEqual((data["pan"], data["tilt"]), (0.5, -0.25))
        self.assertEqual((data["x"], data["y"]), (None, None))
        json.dumps(entry, allow_nan=False)

    async def test_unbounded_zoom_range_logs_no_infinities(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        controller.cams[CAMERA].update(
            {
                "features": ["zoom-a"],
                "move_request": MagicMock(ProfileToken="profile"),
                "absolute_zoom_range": {
                    "XRange": {"Min": float("-inf"), "Max": float("inf")}
                },
            }
        )
        controller.cams[CAMERA]["ptz"].AbsoluteMove = AsyncMock()
        log = _watch(controller)

        await controller._zoom_absolute(CAMERA, 0.5, 1, source=PtzSource.autotrack)

        controller.cams[CAMERA]["ptz"].AbsoluteMove.assert_awaited_once()
        (entry,) = _logged(log)
        data = entry["data"]
        self.assertEqual(data["operation"], "AbsoluteMove")
        self.assertEqual((data["zoom"], data["sent"]), (0.5, None))
        json.dumps(entry, allow_nan=False)

    async def test_failed_request_is_logged_and_raised(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        controller.cams[CAMERA]["ptz"].RelativeMove = AsyncMock(
            side_effect=ConnectionError("camera unreachable")
        )
        log = _watch(controller)

        with self.assertRaises(ConnectionError):
            await controller._move_relative(
                CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
            )

        self.assertEqual(_logged(log)[0]["data"]["error"], "camera unreachable")

    async def test_a_failure_with_no_message_is_still_an_error(self) -> None:
        # str(error) is empty for these, which the Debug view reads as success
        controller = _make_move_controller(autotracking_enabled=False)
        controller.cams[CAMERA]["ptz"].RelativeMove = AsyncMock(
            side_effect=ConnectionError()
        )
        log = _watch(controller)

        with self.assertRaises(ConnectionError):
            await controller._move_relative(
                CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
            )

        self.assertEqual(_logged(log)[0]["data"]["error"], "ConnectionError")

    async def test_a_status_read_failing_with_no_message_is_still_an_error(
        self,
    ) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        ptz = controller.cams[CAMERA]["ptz"]
        ptz.GetStatus = AsyncMock(side_effect=TimeoutError())
        log = _watch(controller)

        with self.assertRaises(TimeoutError):
            await controller._ptz_request(
                CAMERA, PtzSource.debug, ptz, "GetStatus", {"ProfileToken": "profile"}
            )

        (entry,) = _logged(log)
        self.assertEqual(entry["kind"], "status")
        self.assertEqual(entry["data"]["error"], "TimeoutError")

    async def test_a_zeep_fault_is_shown_by_its_message(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        fault = Exception("ignored")
        fault.message = "Sender not authorized"
        controller.cams[CAMERA]["ptz"].RelativeMove = AsyncMock(side_effect=fault)
        log = _watch(controller)

        with self.assertRaises(Exception):
            await controller._move_relative(
                CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
            )

        self.assertEqual(_logged(log)[0]["data"]["error"], "Sender not authorized")

    async def test_a_move_whose_answer_is_pending_is_not_an_outside_move(self) -> None:
        # a debug read lands after the camera started moving but before it
        # answered the move request
        controller = _make_move_controller(autotracking_enabled=False)
        controller.debug_reads = set()
        ptz = controller.cams[CAMERA]["ptz"]
        controller.cams[CAMERA]["move_request"] = MagicMock(ProfileToken="profile")
        started = asyncio.Event()
        answer = asyncio.Event()

        async def slow_move(request):
            started.set()
            await answer.wait()

        ptz.RelativeMove = AsyncMock(side_effect=slow_move)
        ptz.GetStatus = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    MoveStatus=SimpleNamespace(PanTilt="IDLE", Zoom=None),
                    Position=SimpleNamespace(
                        PanTilt=SimpleNamespace(x=0.0, y=0.0), Zoom=None
                    ),
                ),
                SimpleNamespace(
                    MoveStatus=SimpleNamespace(PanTilt="MOVING", Zoom=None),
                    Position=SimpleNamespace(
                        PanTilt=SimpleNamespace(x=0.2, y=0.0), Zoom=None
                    ),
                ),
            ]
        )
        log = _watch(controller)

        with patch("frigate.ptz.onvif.PTZ_DEBUG_STATUS_INTERVAL", 0):
            await controller.debug_snapshot(CAMERA, 0)
            move = asyncio.create_task(
                controller._move_relative(
                    CAMERA, 0.25, 0, 0, 1, source=PtzSource.command
                )
            )
            await started.wait()
            await controller.debug_snapshot(CAMERA, 0)

        answer.set()
        await move

        kinds = [entry["kind"] for entry in _logged(log)]
        self.assertNotIn("external_move", kinds)
        self.assertEqual(kinds, ["status", "status", "request"])

    async def test_move_refused_while_busy_is_logged(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        controller.cams[CAMERA]["active"] = True
        log = _watch(controller)

        sent = await controller._move_relative(
            CAMERA, 0.25, 0, 0, 1, source=PtzSource.autotrack
        )

        self.assertFalse(sent)
        (entry,) = _logged(log)
        self.assertEqual(entry["kind"], "refused")
        self.assertEqual(entry["data"], {"operation": "RelativeMove", "reason": "busy"})

    async def test_nothing_logged_while_the_log_is_closed(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        controller.event_log = PtzEventLog()

        await controller._move_relative(CAMERA, 0.25, 0, 0, 1, source=PtzSource.command)

        controller.cams[CAMERA]["ptz"].RelativeMove.assert_awaited_once()
        self.assertEqual(_logged(controller.event_log), [])

    async def test_click_to_move_is_logged_as_a_command(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        log = _watch(controller)

        await controller.handle_command_async(
            CAMERA, OnvifCommandEnum.move_relative, "relative_0.1_-0.2"
        )

        (entry,) = _logged(log)
        self.assertEqual(entry["source"], "command")
        self.assertEqual((entry["data"]["pan"], entry["data"]["tilt"]), (0.1, -0.2))

    async def test_unknown_preset_is_logged(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        controller.cams[CAMERA]["presets"] = {}
        log = _watch(controller)

        await controller._move_to_preset(CAMERA, "Porch", source=PtzSource.command)

        (entry,) = _logged(log)
        self.assertEqual(entry["kind"], "refused")
        self.assertEqual(entry["data"]["reason"], "unknown_preset")
        self.assertEqual(entry["data"]["preset"], "porch")

    async def test_status_read_is_logged_with_the_position(self) -> None:
        controller = _make_move_controller(autotracking_enabled=False)
        controller.status_locks = {CAMERA: asyncio.Lock()}
        controller.cams[CAMERA]["move_request"] = MagicMock(ProfileToken="profile")
        controller.cams[CAMERA]["ptz"].GetStatus = AsyncMock(return_value=_status())
        log = _watch(controller)

        await controller.get_camera_status(CAMERA, source=PtzSource.autotrack)

        (entry,) = _logged(log)
        self.assertEqual(entry["kind"], "status")
        self.assertEqual(entry["data"]["pan_tilt"], "IDLE")
        self.assertEqual(
            entry["data"]["position"], {"pan": 0.25, "tilt": -0.5, "zoom": 0.0}
        )

    async def test_saving_a_preset_is_logged_as_api(self) -> None:
        controller = await _make_preset_controller([])
        log = _watch(controller)

        await controller.set_preset(CAMERA, "Porch")

        (entry,) = _logged(log)
        self.assertEqual(entry["source"], "api")
        self.assertEqual(entry["data"]["operation"], "SetPreset")
        self.assertEqual(entry["data"]["preset"], "Porch")

    async def test_connecting_is_logged_with_what_the_camera_offers(self) -> None:
        controller = _make_controller(autotracking_enabled=False)
        log = _watch(controller)

        self.assertTrue(await controller._init_onvif(CAMERA))

        (entry,) = _logged(log)
        self.assertEqual(entry["kind"], "connection")
        self.assertEqual(entry["source"], "frigate")
        self.assertTrue(entry["data"]["connected"])
        self.assertIn("pt", entry["data"]["features"])
        self.assertEqual(entry["data"]["relative_spaces"], [])
        json.dumps(entry, allow_nan=False)


class TestDescribeStatus(unittest.TestCase):
    def test_camera_without_position(self) -> None:
        status = SimpleNamespace(
            MoveStatus=SimpleNamespace(PanTilt="MOVING", Zoom=None)
        )

        self.assertEqual(
            describe_status(status),
            {"pan_tilt": "MOVING", "zoom": None, "position": None},
        )

    def test_move_status_reported_as_text(self) -> None:
        status = SimpleNamespace(
            MoveStatus="IDLE", Position=SimpleNamespace(Zoom=SimpleNamespace(x=0.5))
        )

        self.assertEqual(
            describe_status(status),
            {
                "pan_tilt": "IDLE",
                "zoom": None,
                "position": {"pan": None, "tilt": None, "zoom": 0.5},
            },
        )

    def test_values_are_plain_numbers(self) -> None:
        status = SimpleNamespace(
            MoveStatus=SimpleNamespace(PanTilt="IDLE", Zoom=None),
            Position=SimpleNamespace(
                PanTilt=SimpleNamespace(x=Decimal("0.125"), y=Decimal("-1")),
                Zoom=None,
            ),
        )

        described = describe_status(status)

        self.assertEqual(
            described["position"], {"pan": 0.125, "tilt": -1.0, "zoom": None}
        )
        json.dumps(described, allow_nan=False)

    def test_relative_spaces(self) -> None:
        def space(uri: str) -> dict:
            return {
                "URI": f"http://www.onvif.org/ver10/tptz/PanTiltSpaces/{uri}",
                "XRange": {"Min": -1, "Max": 1},
                "YRange": {"Min": -0.5, "Max": 0.5},
            }

        options = SimpleNamespace(
            Spaces=SimpleNamespace(
                RelativePanTiltTranslationSpace=[
                    space("TranslationGenericSpace"),
                    space("TranslationSpaceFov"),
                ]
            )
        )

        self.assertEqual(
            describe_relative_spaces(options),
            [
                {"space": "generic", "x": [-1.0, 1.0], "y": [-0.5, 0.5]},
                {"space": "fov", "x": [-1.0, 1.0], "y": [-0.5, 0.5]},
            ],
        )
        self.assertEqual(describe_relative_spaces(None), [])

    def test_unbounded_relative_space(self) -> None:
        options = SimpleNamespace(
            Spaces=SimpleNamespace(
                RelativePanTiltTranslationSpace=[
                    {
                        "URI": "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov",
                        "XRange": {"Min": float("-inf"), "Max": float("inf")},
                        "YRange": {"Min": -0.5, "Max": float("nan")},
                    }
                ]
            )
        )

        described = describe_relative_spaces(options)

        self.assertEqual(
            described,
            [{"space": "fov", "x": [None, None], "y": [-0.5, None]}],
        )
        json.dumps(described, allow_nan=False)

    def test_non_finite_position_is_left_out(self) -> None:
        status = SimpleNamespace(
            MoveStatus=SimpleNamespace(PanTilt="IDLE", Zoom=None),
            Position=SimpleNamespace(
                PanTilt=SimpleNamespace(x=float("nan"), y=float("inf")),
                Zoom=SimpleNamespace(x=0.5),
            ),
        )

        described = describe_status(status)

        self.assertEqual(
            described["position"], {"pan": None, "tilt": None, "zoom": 0.5}
        )
        json.dumps(described, allow_nan=False)

        status.Position.Zoom = SimpleNamespace(x=float("-inf"))

        self.assertIsNone(describe_status(status)["position"])


def _debug_controller() -> tuple[OnvifController, PtzEventLog]:
    controller = _make_move_controller(autotracking_enabled=True)
    controller.debug_reads = set()
    controller.cams[CAMERA]["move_request"] = MagicMock(ProfileToken="profile")
    controller.cams[CAMERA]["ptz"].GetStatus = AsyncMock(
        return_value=SimpleNamespace(
            MoveStatus=SimpleNamespace(PanTilt="MOVING", Zoom=None),
            Position=SimpleNamespace(PanTilt=SimpleNamespace(x=0.1, y=0.2), Zoom=None),
        )
    )
    controller.event_log = PtzEventLog()
    return controller, controller.event_log


class TestPtzDebugSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_poll_opens_the_log_and_reads_the_status(self) -> None:
        controller, log = _debug_controller()

        snapshot = await controller.debug_snapshot(CAMERA, 0)

        self.assertTrue(log.is_watched(CAMERA))
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["session"], log.session)
        self.assertEqual(snapshot["status"]["pan_tilt"], "MOVING")
        self.assertEqual(snapshot["status"]["position"]["pan"], 0.1)
        self.assertEqual([e["source"] for e in snapshot["entries"]], ["debug"])
        self.assertEqual(snapshot["capabilities"]["features"], ["pt", "pt-r-fov"])
        json.dumps(snapshot)

    async def test_status_is_read_at_most_once_a_second(self) -> None:
        controller, _ = _debug_controller()

        await controller.debug_snapshot(CAMERA, 0)
        await controller.debug_snapshot(CAMERA, 0)

        controller.cams[CAMERA]["ptz"].GetStatus.assert_awaited_once()

    async def test_a_status_read_by_autotracking_counts(self) -> None:
        # the autotracker polls the status while the camera moves, so the
        # Debug view adds no requests then
        controller, log = _debug_controller()
        controller.status_locks = {CAMERA: asyncio.Lock()}
        log.watch(CAMERA)

        await controller.get_camera_status(CAMERA, source=PtzSource.autotrack)
        await controller.debug_snapshot(CAMERA, 0)

        controller.cams[CAMERA]["ptz"].GetStatus.assert_awaited_once()

    async def test_status_read_is_never_doubled(self) -> None:
        controller, _ = _debug_controller()
        release = asyncio.Event()

        async def slow_status(request):
            await release.wait()
            return SimpleNamespace(
                MoveStatus=SimpleNamespace(PanTilt="IDLE", Zoom=None)
            )

        controller.cams[CAMERA]["ptz"].GetStatus = AsyncMock(side_effect=slow_status)

        first = asyncio.create_task(controller.debug_snapshot(CAMERA, 0))
        await asyncio.sleep(0.01)
        # a second browser tab polls while the camera has not answered yet
        second = await controller.debug_snapshot(CAMERA, 0)
        release.set()
        await first

        controller.cams[CAMERA]["ptz"].GetStatus.assert_awaited_once()
        self.assertIsNone(second["status"])

    async def test_status_read_changes_nothing_autotracking_uses(self) -> None:
        controller, _ = _debug_controller()
        controller.get_camera_status = AsyncMock()
        metrics = controller.ptz_metrics[CAMERA]
        metrics.start_time.value = 1000.0
        metrics.stop_time.value = 1001.0
        metrics.video_stop_time.value = 1001.5

        # the camera reports MOVING, as if something else were moving it
        await controller.debug_snapshot(CAMERA, 0)

        controller.get_camera_status.assert_not_awaited()
        self.assertFalse(controller.cams[CAMERA]["active"])
        self.assertTrue(metrics.motor_stopped.is_set())
        self.assertEqual(
            (
                metrics.start_time.value,
                metrics.stop_time.value,
                metrics.video_stop_time.value,
            ),
            (1000.0, 1001.0, 1001.5),
        )

    async def test_camera_that_does_not_answer(self) -> None:
        controller, _ = _debug_controller()

        async def never(request):
            await asyncio.Event().wait()

        controller.cams[CAMERA]["ptz"].GetStatus = AsyncMock(side_effect=never)

        with patch("frigate.ptz.onvif.PTZ_DEBUG_STATUS_TIMEOUT", 0.05):
            snapshot = await controller.debug_snapshot(CAMERA, 0)

        self.assertEqual(snapshot["status"]["error"], "timeout")
        self.assertEqual(controller.debug_reads, set())

    async def test_camera_error_is_shown(self) -> None:
        controller, _ = _debug_controller()
        controller.cams[CAMERA]["ptz"].GetStatus = AsyncMock(
            side_effect=ConnectionError("unreachable")
        )

        snapshot = await controller.debug_snapshot(CAMERA, 0)

        self.assertEqual(snapshot["status"]["error"], "unreachable")

    async def test_camera_not_connected(self) -> None:
        controller, _ = _debug_controller()
        controller.cams[CAMERA]["init"] = False
        controller._init_onvif = AsyncMock()

        snapshot = await controller.debug_snapshot(CAMERA, 0)

        self.assertFalse(snapshot["connected"])
        self.assertIsNone(snapshot["capabilities"])
        controller._init_onvif.assert_not_awaited()
        controller.cams[CAMERA]["ptz"].GetStatus.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
