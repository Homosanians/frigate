"""Tests for setting a camera's NTP server and timezone over ONVIF."""

import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from zeep.exceptions import Fault

from frigate.config import FrigateConfig
from frigate.ptz.onvif import OnvifController, OnvifRequestError

CAMERA = "fixed_cam"


def _config(
    time_sync: dict | None, ignore_time_mismatch: bool = False
) -> FrigateConfig:
    onvif = {"host": "10.0.0.1", "ignore_time_mismatch": ignore_time_mismatch}
    if time_sync is not None:
        onvif["time_sync"] = time_sync
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
                    "onvif": onvif,
                }
            },
        }
    )


def _system_date(
    date_time_type: str = "NTP",
    tz: str | None = "GMT0",
    dst: bool = False,
    offset: int = 0,
) -> SimpleNamespace:
    utc = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=offset)
    return SimpleNamespace(
        DateTimeType=date_time_type,
        DaylightSavings=dst,
        TimeZone=SimpleNamespace(TZ=tz) if tz is not None else None,
        UTCDateTime=SimpleNamespace(
            Date=SimpleNamespace(Year=utc.year, Month=utc.month, Day=utc.day),
            Time=SimpleNamespace(Hour=utc.hour, Minute=utc.minute, Second=utc.second),
        ),
    )


def _make_onvif_camera(current: SimpleNamespace, adjust_time: bool = False):
    device = MagicMock()
    device.SetNTP = AsyncMock()
    device.SetSystemDateAndTime = AsyncMock()
    device.authless_GetSystemDateAndTime = AsyncMock(return_value=current)
    device.GetSystemDateAndTime = AsyncMock(return_value=current)

    onvif = MagicMock()
    onvif.adjust_time = adjust_time
    onvif.dt_diff = datetime.timedelta(0) if adjust_time else None
    onvif.update_xaddrs = AsyncMock()
    onvif.create_devicemgmt_service = AsyncMock(return_value=device)
    onvif.close = AsyncMock()
    return onvif, device


def _make_controller(
    time_sync: dict | None,
    current: SimpleNamespace | None = None,
    ignore_time_mismatch: bool = False,
) -> tuple[OnvifController, MagicMock]:
    """Build a controller without invoking __init__, which would start an event loop
    thread and reach out to the camera."""
    onvif, device = _make_onvif_camera(
        current or _system_date(), adjust_time=ignore_time_mismatch
    )
    controller = OnvifController.__new__(OnvifController)
    controller.config = _config(time_sync, ignore_time_mismatch)
    controller.cams = {CAMERA: {"onvif": onvif, "init": False}}
    controller.failed_cams = {}
    controller.status_locks = {}
    controller.device_locks = {}
    controller.time_sync_tasks = {}
    controller.time_sync_results = {}
    return controller, device


ENABLED = {"enabled": True, "ntp_server": "pool.ntp.org", "timezone": "Europe/Moscow"}


class TestSyncTime(unittest.IsolatedAsyncioTestCase):
    async def test_sets_ntp_server_and_timezone(self):
        controller, device = _make_controller(ENABLED)

        await controller.sync_time(CAMERA)

        device.SetNTP.assert_awaited_once_with(
            {
                "FromDHCP": False,
                "NTPManual": [{"Type": "DNS", "DNSname": "pool.ntp.org"}],
            }
        )
        device.SetSystemDateAndTime.assert_awaited_once_with(
            {
                "DateTimeType": "NTP",
                "DaylightSavings": False,
                "TimeZone": {"TZ": "MSK-3"},
            }
        )

    async def test_ntp_server_address_types(self):
        for address, host in (
            ("192.168.1.1", {"Type": "IPv4", "IPv4Address": "192.168.1.1"}),
            ("fe80::1", {"Type": "IPv6", "IPv6Address": "fe80::1"}),
        ):
            with self.subTest(address=address):
                controller, device = _make_controller(
                    {"enabled": True, "ntp_server": address}
                )

                await controller.sync_time(CAMERA)

                device.SetNTP.assert_awaited_once_with(
                    {"FromDHCP": False, "NTPManual": [host]}
                )

    async def test_timezone_only_on_manual_clock_sets_frigate_time(self):
        controller, device = _make_controller(
            {"enabled": True, "timezone": "America/New_York"},
            _system_date("Manual", offset=3600),
        )

        await controller.sync_time(CAMERA)

        device.SetNTP.assert_not_awaited()
        request = device.SetSystemDateAndTime.await_args.args[0]
        self.assertEqual(request["DateTimeType"], "Manual")
        self.assertTrue(request["DaylightSavings"])
        self.assertEqual(request["TimeZone"], {"TZ": "EST5EDT,M3.2.0,M11.1.0"})
        sent = request["UTCDateTime"]
        sent_time = datetime.datetime(
            sent["Date"]["Year"],
            sent["Date"]["Month"],
            sent["Date"]["Day"],
            sent["Time"]["Hour"],
            sent["Time"]["Minute"],
            sent["Time"]["Second"],
            tzinfo=datetime.UTC,
        )
        self.assertLess(
            abs((sent_time - datetime.datetime.now(datetime.UTC)).total_seconds()), 5
        )

    async def test_ntp_only_keeps_the_camera_timezone(self):
        controller, device = _make_controller(
            {"enabled": True, "ntp_server": "pool.ntp.org"},
            _system_date("Manual", tz="CET-1CEST,M3.5.0,M10.5.0/3", dst=True),
        )

        await controller.sync_time(CAMERA)

        device.SetSystemDateAndTime.assert_awaited_once_with(
            {
                "DateTimeType": "NTP",
                "DaylightSavings": True,
                "TimeZone": {"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"},
            }
        )

    async def test_requires_enabled_and_configured(self):
        for time_sync in (
            None,
            {"enabled": False, "ntp_server": "pool.ntp.org"},
            {"enabled": True},
        ):
            with self.subTest(time_sync=time_sync):
                controller, device = _make_controller(time_sync)

                with self.assertRaises(OnvifRequestError):
                    await controller.sync_time(CAMERA)

                device.SetNTP.assert_not_awaited()
                device.SetSystemDateAndTime.assert_not_awaited()

    async def test_result_recorded(self):
        controller, device = _make_controller(ENABLED)

        await controller.sync_time(CAMERA)
        self.assertTrue(controller.time_sync_results[CAMERA]["success"])
        self.assertIsNone(controller.time_sync_results[CAMERA]["message"])

        device.SetNTP.side_effect = Fault("Sender not authorized")
        with self.assertRaises(Fault):
            await controller.sync_time(CAMERA)

        result = controller.time_sync_results[CAMERA]
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "Sender not authorized")

    async def test_device_info_reports_time_sync(self):
        controller, device = _make_controller(ENABLED)
        device.GetDeviceInformation = AsyncMock(
            return_value=SimpleNamespace(
                Manufacturer="Acme", Model="Dome", FirmwareVersion="1"
            )
        )
        device.GetScopes = AsyncMock(return_value=[])
        device.GetNTP = AsyncMock(side_effect=Fault("not supported"))
        await controller.sync_time(CAMERA)

        info = await controller.get_device_info(CAMERA)

        self.assertEqual(
            info["time_sync"],
            {
                "enabled": True,
                "ntp_server": "pool.ntp.org",
                "timezone": "Europe/Moscow",
                "posix_timezone": "MSK-3",
                "last_result": controller.time_sync_results[CAMERA],
            },
        )


class TestClockOffsetRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_session_reconnected_once_after_clock_settles(self):
        controller, device = _make_controller(ENABLED, ignore_time_mismatch=True)
        old_onvif = controller.cams[CAMERA]["onvif"]
        # the session measured the camera clock an hour ahead
        old_onvif.dt_diff = datetime.timedelta(hours=1)
        # the camera's NTP client needs a moment before its clock is corrected
        device.authless_GetSystemDateAndTime.side_effect = [
            _system_date(offset=3600),
            _system_date(offset=3600),
            _system_date(offset=1),
        ]
        new_onvif = MagicMock()

        with (
            patch("frigate.ptz.onvif.CLOCK_POLL_SECONDS", 0),
            patch("frigate.ptz.onvif.ONVIFCamera", return_value=new_onvif) as create,
            patch.object(controller, "_time_sync_loop", new=AsyncMock()) as loop,
        ):
            await controller.sync_time(CAMERA)
            await controller.time_sync_tasks[CAMERA]

        create.assert_called_once()
        old_onvif.close.assert_awaited_once()
        self.assertIs(controller.cams[CAMERA]["onvif"], new_onvif)
        self.assertEqual(device.authless_GetSystemDateAndTime.await_count, 3)
        # reconnecting must not start another sync
        loop.assert_not_called()
        self.assertTrue(controller.time_sync_results[CAMERA]["success"])

    async def test_no_reconnect_without_ignore_time_mismatch(self):
        controller, _ = _make_controller(ENABLED)

        with patch("frigate.ptz.onvif.ONVIFCamera") as create:
            await controller.sync_time(CAMERA)

        create.assert_not_called()
        self.assertNotIn(CAMERA, controller.time_sync_tasks)

    async def test_no_reconnect_when_clock_was_already_right(self):
        # the common case on every startup and save, which must leave PTZ alone
        controller, _ = _make_controller(ENABLED, ignore_time_mismatch=True)
        controller.cams[CAMERA]["onvif"].dt_diff = datetime.timedelta(seconds=1)

        with patch("frigate.ptz.onvif.ONVIFCamera") as create:
            await controller.sync_time(CAMERA)

        create.assert_not_called()
        self.assertNotIn(CAMERA, controller.time_sync_tasks)

    async def test_no_reconnect_when_clock_did_not_move(self):
        controller, device = _make_controller(ENABLED, ignore_time_mismatch=True)
        old_onvif = controller.cams[CAMERA]["onvif"]
        old_onvif.dt_diff = datetime.timedelta(hours=1)
        # e.g. the camera cannot reach the NTP server
        device.authless_GetSystemDateAndTime.side_effect = None
        device.authless_GetSystemDateAndTime.return_value = _system_date(offset=3600)

        with (
            patch("frigate.ptz.onvif.CLOCK_POLL_SECONDS", 0),
            patch("frigate.ptz.onvif.CLOCK_SETTLE_SECONDS", 0.05),
            patch("frigate.ptz.onvif.ONVIFCamera") as create,
        ):
            await controller.sync_time(CAMERA)
            await controller.time_sync_tasks[CAMERA]

        create.assert_not_called()
        old_onvif.close.assert_not_awaited()
        self.assertIs(controller.cams[CAMERA]["onvif"], old_onvif)


class TestTimeSyncLoop(unittest.IsolatedAsyncioTestCase):
    async def test_retries_while_camera_is_unreachable(self):
        controller, _ = _make_controller(ENABLED)
        controller.sync_time = AsyncMock(
            side_effect=[OSError("Cannot connect to host"), None]
        )

        with (
            patch("frigate.ptz.onvif.TIME_SYNC_RETRY_SECONDS", 0),
            self.assertLogs("frigate.ptz.onvif", level="DEBUG") as logs,
        ):
            await controller._time_sync_loop(CAMERA)

        self.assertEqual(controller.sync_time.await_count, 2)
        self.assertEqual(len([r for r in logs.records if r.levelname == "WARNING"]), 1)

    async def test_warns_only_once_while_retrying(self):
        controller, _ = _make_controller(ENABLED)
        controller.sync_time = AsyncMock(
            side_effect=[OSError("down"), OSError("down"), OSError("down"), None]
        )

        with (
            patch("frigate.ptz.onvif.TIME_SYNC_RETRY_SECONDS", 0),
            self.assertLogs("frigate.ptz.onvif", level="DEBUG") as logs,
        ):
            await controller._time_sync_loop(CAMERA)

        self.assertEqual(len([r for r in logs.records if r.levelname == "WARNING"]), 1)

    async def test_stops_when_camera_rejects(self):
        controller, _ = _make_controller(ENABLED)
        controller.sync_time = AsyncMock(side_effect=Fault("Sender not authorized"))

        with self.assertLogs("frigate.ptz.onvif", level="WARNING") as logs:
            await controller._time_sync_loop(CAMERA)

        controller.sync_time.assert_awaited_once()
        self.assertIn("Sender not authorized", logs.output[0])
        self.assertIn("administrator", logs.output[0])


class TestTimeSyncTasks(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_only_when_enabled(self):
        for time_sync, scheduled in (
            (ENABLED, True),
            ({"enabled": False, "ntp_server": "pool.ntp.org"}, False),
        ):
            with self.subTest(time_sync=time_sync):
                controller, _ = _make_controller(time_sync)
                controller.cams = {}

                with (
                    patch("frigate.ptz.onvif.ONVIFCamera"),
                    patch.object(controller, "_time_sync_loop", new=AsyncMock()),
                ):
                    self.assertTrue(await controller._init_single_camera(CAMERA))
                    if scheduled:
                        await controller.time_sync_tasks[CAMERA]

                self.assertEqual(CAMERA in controller.time_sync_tasks, scheduled)

    async def test_manual_sync_does_not_start_background_sync(self):
        # a camera without a session yet, e.g. after its startup init failed
        controller, _ = _make_controller(ENABLED)
        onvif, _ = _make_onvif_camera(_system_date())
        controller.cams = {}

        with (
            patch("frigate.ptz.onvif.ONVIFCamera", return_value=onvif),
            patch.object(controller, "_time_sync_loop", new=AsyncMock()) as loop,
        ):
            await controller.sync_time(CAMERA)

        loop.assert_not_called()
        self.assertTrue(controller.time_sync_results[CAMERA]["success"])

    async def test_not_scheduled_when_asked_not_to(self):
        controller, _ = _make_controller(ENABLED)
        controller.cams = {}

        with patch("frigate.ptz.onvif.ONVIFCamera"):
            await controller._init_single_camera(CAMERA, schedule_time_sync=False)

        self.assertNotIn(CAMERA, controller.time_sync_tasks)

    async def test_close_cancels_running_task(self):
        controller, _ = _make_controller(ENABLED)
        task = asyncio.create_task(asyncio.sleep(60))
        controller.time_sync_tasks[CAMERA] = task

        await controller._close_camera(CAMERA)
        await asyncio.sleep(0)

        self.assertTrue(task.cancelled())
        self.assertNotIn(CAMERA, controller.time_sync_tasks)

    async def test_close_from_the_task_itself_does_not_cancel_it(self):
        controller, _ = _make_controller(ENABLED)

        async def close_from_task():
            await controller._close_camera(CAMERA)
            return "finished"

        task = asyncio.create_task(close_from_task())
        controller.time_sync_tasks[CAMERA] = task

        self.assertEqual(await task, "finished")

    async def test_reinit_ignores_cameras_without_onvif(self):
        # a global onvif change is published to every camera, ONVIF or not
        controller, _ = _make_controller(ENABLED)
        controller.config.cameras[CAMERA].onvif.host = ""
        controller.cams = {}

        with (
            patch("frigate.ptz.onvif.ONVIFCamera") as create,
            self.assertNoLogs("frigate.ptz.onvif", level="INFO"),
        ):
            await controller._reinit_camera(CAMERA)

        create.assert_not_called()

    async def test_results_survive_reinit(self):
        controller, _ = _make_controller(ENABLED)
        await controller.sync_time(CAMERA)

        with (
            patch("frigate.ptz.onvif.ONVIFCamera"),
            patch.object(controller, "_time_sync_loop", new=AsyncMock()),
        ):
            await controller._reinit_camera(CAMERA)

        self.assertTrue(controller.time_sync_results[CAMERA]["success"])


if __name__ == "__main__":
    unittest.main()
