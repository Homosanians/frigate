"""Unit tests for the PTZ preset/home management and ONVIF device endpoints."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from frigate.models import Event, Recordings
from frigate.ptz.onvif import OnvifRequestError, OnvifUnavailableError
from frigate.test.http_api.base_http_test import AuthTestClient, BaseTestHttp

CAMERA = "front_door"
VIEWER = {"remote-user": "viewer", "remote-role": "viewer"}
DEVICE_INFO = {
    "manufacturer": "Acme",
    "model": "Dome 4",
    "firmware_version": "1.2.3",
    "conformance_profiles": ["S", "T"],
    "date_time": None,
    "ntp": None,
}


class CameraFault(Exception):
    """Mimics zeep.exceptions.Fault, which carries the camera's text in .message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TestHttpPtz(BaseTestHttp):
    def setUp(self):
        super().setUp([Event, Recordings])
        self.app = super().create_app()

        # the endpoints hand their work to the OnvifController loop thread
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        def stop_loop() -> None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()

        self.addCleanup(stop_loop)

        self.onvif = SimpleNamespace(
            loop=loop,
            set_preset=AsyncMock(return_value="token_1"),
            remove_preset=AsyncMock(),
            set_home=AsyncMock(),
            get_device_info=AsyncMock(return_value=DEVICE_INFO),
            sync_time=AsyncMock(),
        )
        self.app.onvif = self.onvif

    def test_viewer_cannot_manage_presets(self):
        with AuthTestClient(self.app) as client:
            for response in (
                client.post(
                    f"/{CAMERA}/ptz/presets", json={"name": "Porch"}, headers=VIEWER
                ),
                client.put(f"/{CAMERA}/ptz/presets/1", json={}, headers=VIEWER),
                client.delete(f"/{CAMERA}/ptz/presets/1", headers=VIEWER),
                client.post(f"/{CAMERA}/ptz/home", headers=VIEWER),
            ):
                assert response.status_code == 403

        self.onvif.set_preset.assert_not_awaited()
        self.onvif.remove_preset.assert_not_awaited()
        self.onvif.set_home.assert_not_awaited()

    def test_create_preset(self):
        with AuthTestClient(self.app) as client:
            response = client.post(f"/{CAMERA}/ptz/presets", json={"name": "Porch"})

        assert response.status_code == 200
        assert response.json()["token"] == "token_1"
        self.onvif.set_preset.assert_awaited_once_with(CAMERA, "Porch")

    def test_overwrite_preset_keeps_name_when_omitted(self):
        with AuthTestClient(self.app) as client:
            response = client.put(f"/{CAMERA}/ptz/presets/tok%201", json={})

        assert response.status_code == 200
        self.onvif.set_preset.assert_awaited_once_with(CAMERA, None, "tok 1")

    def test_delete_preset_and_set_home(self):
        with AuthTestClient(self.app) as client:
            assert client.delete(f"/{CAMERA}/ptz/presets/1").status_code == 200
            assert client.post(f"/{CAMERA}/ptz/home").status_code == 200

        self.onvif.remove_preset.assert_awaited_once_with(CAMERA, "1")
        self.onvif.set_home.assert_awaited_once_with(CAMERA)

    def test_errors_mapped_to_status_codes(self):
        for error, status in (
            (OnvifRequestError("A preset named Porch already exists"), 400),
            (OnvifUnavailableError("ONVIF is not configured"), 404),
            (CameraFault("Preset table full"), 502),
        ):
            with self.subTest(error=error):
                self.onvif.set_preset.side_effect = error

                with AuthTestClient(self.app) as client:
                    response = client.post(
                        f"/{CAMERA}/ptz/presets", json={"name": "Porch"}
                    )

                assert response.status_code == status
                assert str(error) in response.json()["message"]

    def test_unknown_camera(self):
        with AuthTestClient(self.app) as client:
            response = client.post("/missing/ptz/presets", json={"name": "Porch"})

        assert response.status_code in (403, 404)
        self.onvif.set_preset.assert_not_awaited()

    def test_empty_name_rejected(self):
        with AuthTestClient(self.app) as client:
            response = client.post(f"/{CAMERA}/ptz/presets", json={"name": ""})

        assert response.status_code == 422
        self.onvif.set_preset.assert_not_awaited()

    def test_device_info_returned_as_body(self):
        for headers in ({}, VIEWER):
            with self.subTest(headers=headers):
                with AuthTestClient(self.app) as client:
                    response = client.get(f"/{CAMERA}/onvif/info", headers=headers)

                assert response.status_code == 200
                assert response.json() == DEVICE_INFO

        self.onvif.get_device_info.assert_awaited_with(CAMERA)

    def test_device_info_errors_mapped_to_status_codes(self):
        for error, status in (
            (OnvifUnavailableError("ONVIF is not configured"), 404),
            (CameraFault("Cannot connect to host"), 502),
        ):
            with self.subTest(error=error):
                self.onvif.get_device_info.side_effect = error

                with AuthTestClient(self.app) as client:
                    response = client.get(f"/{CAMERA}/onvif/info")

                assert response.status_code == status
                assert str(error) in response.json()["message"]

    def test_device_info_unknown_camera(self):
        with AuthTestClient(self.app) as client:
            response = client.get("/missing/onvif/info")

        assert response.status_code in (403, 404)
        self.onvif.get_device_info.assert_not_awaited()

    def test_sync_time(self):
        with AuthTestClient(self.app) as client:
            response = client.post(f"/{CAMERA}/onvif/time_sync")

        assert response.status_code == 200
        assert response.json()["success"] is True
        self.onvif.sync_time.assert_awaited_once_with(CAMERA)

    def test_viewer_cannot_sync_time(self):
        with AuthTestClient(self.app) as client:
            response = client.post(f"/{CAMERA}/onvif/time_sync", headers=VIEWER)

        assert response.status_code == 403
        self.onvif.sync_time.assert_not_awaited()

    def test_sync_time_errors_mapped_to_status_codes(self):
        for error, status in (
            (OnvifRequestError("Time sync is not enabled for front_door"), 400),
            (OnvifUnavailableError("ONVIF is not configured"), 404),
            (CameraFault("Sender not authorized"), 502),
        ):
            with self.subTest(error=error):
                self.onvif.sync_time.side_effect = error

                with AuthTestClient(self.app) as client:
                    response = client.post(f"/{CAMERA}/onvif/time_sync")

                assert response.status_code == status
                assert str(error) in response.json()["message"]
