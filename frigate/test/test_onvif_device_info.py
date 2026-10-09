"""Tests for reading ONVIF device information without PTZ initialization."""

import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from zeep.exceptions import Fault

from frigate.config import FrigateConfig
from frigate.ptz.onvif import (
    OnvifController,
    OnvifUnavailableError,
    parse_conformance_profiles,
)

CAMERA = "fixed_cam"


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


def _scope(item: str) -> SimpleNamespace:
    return SimpleNamespace(ScopeDef="Fixed", ScopeItem=item)


def _system_date(utc: datetime.datetime) -> SimpleNamespace:
    return SimpleNamespace(
        DateTimeType="NTP",
        DaylightSavings=False,
        TimeZone=SimpleNamespace(TZ="MSK-3"),
        UTCDateTime=SimpleNamespace(
            Date=SimpleNamespace(Year=utc.year, Month=utc.month, Day=utc.day),
            Time=SimpleNamespace(Hour=utc.hour, Minute=utc.minute, Second=utc.second),
        ),
    )


def _make_device_service(camera_utc: datetime.datetime) -> MagicMock:
    device = MagicMock()
    device.GetDeviceInformation = AsyncMock(
        return_value=SimpleNamespace(
            Manufacturer="Acme", Model="Dome 4", FirmwareVersion="1.2.3"
        )
    )
    device.GetScopes = AsyncMock(
        return_value=[
            _scope("onvif://www.onvif.org/type/video_encoder"),
            _scope("onvif://www.onvif.org/Profile/Streaming"),
            _scope("onvif://www.onvif.org/Profile/T"),
            _scope("onvif://www.onvif.org/name/Dome"),
        ]
    )
    device.authless_GetSystemDateAndTime = AsyncMock(
        return_value=_system_date(camera_utc)
    )
    device.GetSystemDateAndTime = AsyncMock(return_value=_system_date(camera_utc))
    device.GetNTP = AsyncMock(
        return_value=SimpleNamespace(
            FromDHCP=False,
            NTPFromDHCP=None,
            NTPManual=[
                SimpleNamespace(
                    Type="DNS",
                    DNSname="pool.ntp.org",
                    IPv4Address=None,
                    IPv6Address=None,
                )
            ],
        )
    )
    return device


def _make_controller(
    device: MagicMock, adjust_time: bool = False, config: FrigateConfig | None = None
) -> OnvifController:
    """Build a controller without invoking __init__, which would start an event loop
    thread and reach out to the camera. The camera has no PTZ service."""
    onvif = MagicMock()
    onvif.adjust_time = adjust_time
    onvif.dt_diff = None
    onvif.update_xaddrs = AsyncMock()
    onvif.create_devicemgmt_service = AsyncMock(return_value=device)
    onvif.get_definition = MagicMock(side_effect=Exception("no ptz service"))

    controller = OnvifController.__new__(OnvifController)
    controller.config = config or _config()
    controller.cams = {CAMERA: {"onvif": onvif, "init": False}}
    controller.failed_cams = {}
    controller.device_locks = {}
    return controller


class TestParseConformanceProfiles(unittest.TestCase):
    def test_profiles_from_scopes(self):
        scopes = [
            _scope("onvif://www.onvif.org/Profile/Streaming"),
            _scope("onvif://www.onvif.org/Profile/T"),
            _scope("onvif://www.onvif.org/profile/g"),
            _scope("onvif://www.onvif.org/Profile/Q/Operational"),
            _scope("onvif://www.onvif.org/Profile/T"),
            _scope("onvif://www.onvif.org/hardware/T"),
            _scope("odm:name:Profile/M"),
            SimpleNamespace(ScopeDef="Fixed", ScopeItem=None),
        ]

        self.assertEqual(parse_conformance_profiles(scopes), ["G", "Q", "S", "T"])

    def test_no_scopes(self):
        self.assertEqual(parse_conformance_profiles(None), [])
        self.assertEqual(parse_conformance_profiles([]), [])


class TestGetDeviceInfo(unittest.IsolatedAsyncioTestCase):
    async def test_reports_device_profiles_clock_and_ntp(self):
        camera_utc = datetime.datetime.now(datetime.UTC).replace(
            microsecond=0
        ) + datetime.timedelta(seconds=30)
        device = _make_device_service(camera_utc)
        controller = _make_controller(device)

        info = await controller.get_device_info(CAMERA)

        self.assertEqual(info["manufacturer"], "Acme")
        self.assertEqual(info["model"], "Dome 4")
        self.assertEqual(info["firmware_version"], "1.2.3")
        self.assertEqual(info["conformance_profiles"], ["S", "T"])
        self.assertEqual(info["date_time"]["type"], "NTP")
        self.assertEqual(info["date_time"]["timezone"], "MSK-3")
        self.assertFalse(info["date_time"]["daylight_savings"])
        self.assertEqual(
            info["date_time"]["utc_time"],
            camera_utc.isoformat().replace("+00:00", "Z"),
        )
        self.assertAlmostEqual(info["date_time"]["offset_seconds"], 30, delta=2)
        self.assertEqual(info["ntp"], {"from_dhcp": False, "servers": ["pool.ntp.org"]})

    async def test_camera_without_ptz_is_not_ptz_initialized(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        controller = _make_controller(device)
        onvif = controller.cams[CAMERA]["onvif"]

        await controller.get_device_info(CAMERA)

        onvif.get_definition.assert_not_called()
        onvif.create_media_service.assert_not_called()
        self.assertFalse(controller.cams[CAMERA]["init"])

    async def test_ntp_servers_from_dhcp(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        device.GetNTP = AsyncMock(
            return_value=SimpleNamespace(
                FromDHCP=True,
                NTPFromDHCP=[
                    SimpleNamespace(
                        Type="IPv4",
                        DNSname=None,
                        IPv4Address="192.168.1.1",
                        IPv6Address=None,
                    )
                ],
                NTPManual=None,
            )
        )
        controller = _make_controller(device)

        info = await controller.get_device_info(CAMERA)

        self.assertEqual(info["ntp"], {"from_dhcp": True, "servers": ["192.168.1.1"]})

    async def test_failed_requests_leave_other_fields(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        device.GetScopes = AsyncMock(side_effect=Fault("not supported"))
        device.GetNTP = AsyncMock(side_effect=Fault("not supported"))
        controller = _make_controller(device)

        info = await controller.get_device_info(CAMERA)

        self.assertEqual(info["manufacturer"], "Acme")
        self.assertIsNone(info["conformance_profiles"])
        self.assertIsNone(info["ntp"])
        self.assertEqual(info["date_time"]["type"], "NTP")

    async def test_clock_read_falls_back_to_authenticated_request(self):
        camera_utc = datetime.datetime.now(datetime.UTC)
        device = _make_device_service(camera_utc)
        device.authless_GetSystemDateAndTime = AsyncMock(
            side_effect=Fault("not authorized")
        )
        controller = _make_controller(device)

        info = await controller.get_device_info(CAMERA)

        device.GetSystemDateAndTime.assert_awaited_once()
        self.assertEqual(info["date_time"]["timezone"], "MSK-3")

    async def test_unreachable_camera_raises(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        error = OSError("Cannot connect to host")
        for name in (
            "GetDeviceInformation",
            "GetScopes",
            "authless_GetSystemDateAndTime",
            "GetSystemDateAndTime",
            "GetNTP",
        ):
            setattr(device, name, AsyncMock(side_effect=error))
        controller = _make_controller(device)

        with self.assertRaises(OSError):
            await controller.get_device_info(CAMERA)

    async def test_time_offset_is_measured_before_device_requests(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        controller = _make_controller(device, adjust_time=True)
        onvif = controller.cams[CAMERA]["onvif"]

        await controller.get_device_info(CAMERA)

        onvif.update_xaddrs.assert_awaited_once()

    async def test_no_time_offset_needed_without_adjust_time(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        controller = _make_controller(device)
        onvif = controller.cams[CAMERA]["onvif"]

        await controller.get_device_info(CAMERA)

        onvif.update_xaddrs.assert_not_awaited()

    async def test_camera_without_onvif(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        controller = _make_controller(device, config=_config(onvif_host=""))
        controller.cams = {}

        with self.assertRaises(OnvifUnavailableError):
            await controller.get_device_info(CAMERA)

    async def test_requests_are_serialized_per_camera(self):
        device = _make_device_service(datetime.datetime.now(datetime.UTC))
        controller = _make_controller(device)
        running = 0
        overlap = False

        async def slow_info():
            nonlocal running, overlap
            running += 1
            overlap = overlap or running > 1
            await asyncio.sleep(0.01)
            running -= 1
            return SimpleNamespace(Manufacturer="Acme", Model="", FirmwareVersion="")

        device.GetDeviceInformation = AsyncMock(side_effect=slow_info)

        await asyncio.gather(
            controller.get_device_info(CAMERA), controller.get_device_info(CAMERA)
        )

        self.assertFalse(overlap)


if __name__ == "__main__":
    unittest.main()
