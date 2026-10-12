"""Tests for the relay that shares go2rtc connections between live viewers."""

import asyncio
import json
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from frigate.live_cache.relay import Relay, serve
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

CODECS = "avc1.640029,flac"
REPLY = '{"type":"mse","value":"video/mp4; codecs=\\"avc1.640029,flac\\""}'
ERROR = '{"type":"error","value":"mse: streams: codecs not matched: H265 => H264"}'
LIVE = '{"type": "live"}'
INIT = init_segment(track(1, b"vide"), track(2, b"soun"))
INIT_2 = init_segment(track(1, b"vide"))
KEY = fragment(VIDEO_TRACK, KEYFRAME, b"key")
KEY_2 = fragment(VIDEO_TRACK, KEYFRAME, b"key2")
DELTA = fragment(VIDEO_TRACK, NON_KEYFRAME, b"delta")
DELTA_2 = fragment(VIDEO_TRACK, NON_KEYFRAME, b"delta2")
SOUND = fragment(AUDIO_TRACK, AUDIO, b"sound")
CLOSED = (
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
)


class FakeGo2rtc:
    """Stands in for go2rtc's MSE WebSocket: records each viewer's stream and
    codec list, answers negotiation, then sends whatever the test hands it."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.finished = 0
        self.reply = REPLY

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes([web.get("/api/ws", self._handle)])
        return app

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        message = await ws.receive_json()
        self.requests.append((request.query["src"], message["value"]))
        await ws.send_str(self.reply)
        self.sockets.append(ws)

        async for _ in ws:
            pass

        self.finished += 1
        return ws


class TestRelay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.go2rtc = FakeGo2rtc()
        self.go2rtc_server = TestServer(self.go2rtc.app())
        await self.go2rtc_server.start_server()
        self.relay = Relay(
            {"cam"}, go2rtc_url=str(self.go2rtc_server.make_url("/api/ws"))
        )
        await self.relay.start()
        self.relay_server = TestServer(self.relay.app())
        await self.relay_server.start_server()
        self.client = aiohttp.ClientSession()
        self.viewers: list[aiohttp.ClientWebSocketResponse] = []

    async def asyncTearDown(self) -> None:
        for viewer in self.viewers:
            await viewer.close()

        await self.client.close()
        await self.relay_server.close()
        await self.relay.close()
        await self.go2rtc_server.close()

    async def view(
        self, stream: str = "cam", codecs: str = CODECS
    ) -> aiohttp.ClientWebSocketResponse:
        ws = await self.client.ws_connect(
            self.relay_server.make_url("/ws").with_query(src=stream)
        )
        await ws.send_json({"type": "mse", "value": codecs})
        self.viewers.append(ws)
        return ws

    async def read(self, ws: aiohttp.ClientWebSocketResponse, count: int) -> list:
        items = []

        for _ in range(count):
            message = await ws.receive(timeout=2)
            self.assertIn(
                message.type, (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY)
            )
            items.append(message.data)

        return items

    async def eventually(self, condition) -> None:
        for _ in range(300):
            if condition():
                return

            await asyncio.sleep(0.01)

        self.fail("condition was not met")

    async def upstream(self, index: int = 0) -> web.WebSocketResponse:
        await self.eventually(lambda: len(self.go2rtc.sockets) > index)
        return self.go2rtc.sockets[index]

    async def test_new_viewer_gets_the_backlog_then_live_frames(self):
        first = await self.view()
        upstream = await self.upstream()
        for data in (INIT, KEY, DELTA, SOUND):
            await upstream.send_bytes(data)

        # the first viewer had no backlog to get
        self.assertEqual(
            await self.read(first, 6), [REPLY, INIT, LIVE, KEY, DELTA, SOUND]
        )

        second = await self.view()
        self.assertEqual(
            await self.read(second, 6), [REPLY, INIT, KEY, DELTA, SOUND, LIVE]
        )

        await upstream.send_bytes(DELTA_2)
        self.assertEqual(await self.read(first, 1), [DELTA_2])
        self.assertEqual(await self.read(second, 1), [DELTA_2])
        self.assertEqual(self.go2rtc.requests, [("cam", CODECS)])

    async def test_viewer_without_a_backlog_starts_at_the_next_keyframe(self):
        first = await self.view()
        upstream = await self.upstream()
        await upstream.send_bytes(INIT)
        self.assertEqual(await self.read(first, 3), [REPLY, INIT, LIVE])

        second = await self.view()
        self.assertEqual(await self.read(second, 3), [REPLY, INIT, LIVE])

        for data in (DELTA, SOUND, KEY, DELTA_2):
            await upstream.send_bytes(data)

        self.assertEqual(await self.read(second, 2), [KEY, DELTA_2])

    async def test_another_codec_list_gets_its_own_connection(self):
        await self.view(codecs="avc1.640029")
        await self.view(codecs="hvc1.1.6.L153.B0")
        await self.view(codecs="avc1.640029")

        await self.eventually(lambda: len(self.go2rtc.requests) == 2)
        await asyncio.sleep(0.1)
        self.assertCountEqual(
            self.go2rtc.requests,
            [("cam", "avc1.640029"), ("cam", "hvc1.1.6.L153.B0")],
        )

    async def test_go2rtc_error_is_forwarded_and_the_socket_stays_open(self):
        self.go2rtc.reply = ERROR
        viewer = await self.view()

        self.assertEqual(await self.read(viewer, 1), [ERROR])
        # go2rtc leaves a viewer's socket open after an error, and the
        # player's fallback timing relies on that
        with self.assertRaises(asyncio.TimeoutError):
            await viewer.receive(timeout=0.3)

        # the failed connection is not reused
        await self.view()
        await self.eventually(lambda: len(self.go2rtc.requests) == 2)

    async def test_go2rtc_closing_closes_viewers_and_drops_the_backlog(self):
        viewer = await self.view()
        upstream = await self.upstream()
        await upstream.send_bytes(INIT)
        await upstream.send_bytes(KEY)
        await self.read(viewer, 4)

        await upstream.close()
        self.assertIn((await viewer.receive(timeout=2)).type, CLOSED)

        again = await self.view()
        upstream = await self.upstream(1)
        await upstream.send_bytes(INIT_2)
        await upstream.send_bytes(KEY_2)
        self.assertEqual(await self.read(again, 4), [REPLY, INIT_2, LIVE, KEY_2])

    async def test_stream_stays_connected_for_a_while_after_its_last_viewer(self):
        with patch("frigate.live_cache.relay.LINGER_SECONDS", 0.4):
            viewer = await self.view()
            await self.upstream()
            await viewer.close()

            await asyncio.sleep(0.1)
            again = await self.view()
            await self.read(again, 1)
            self.assertEqual(len(self.go2rtc.requests), 1)

            await again.close()
            await self.eventually(lambda: self.go2rtc.finished == 1)

    async def test_warm_request_keeps_a_stream_connected(self):
        with patch("frigate.live_cache.relay.LINGER_SECONDS", 0.05):
            warm = await self.client.ws_connect(
                self.relay_server.make_url("/warm").with_query(
                    [("src", "cam"), ("src", "other")]
                )
            )
            await warm.send_json({"type": "warm", "value": CODECS})
            await self.upstream()
            self.assertEqual(self.go2rtc.requests, [("cam", CODECS)])

            viewer = await self.view()
            await self.read(viewer, 1)
            await viewer.close()
            await asyncio.sleep(0.3)
            self.assertEqual(self.go2rtc.finished, 0)
            self.assertEqual(len(self.go2rtc.requests), 1)

            await warm.close()
            await self.eventually(lambda: self.go2rtc.finished == 1)

    async def test_warm_request_reconnects_a_stream_that_dropped(self):
        with patch("frigate.live_cache.relay.WARM_RETRY_SECONDS", 0.1):
            await self.relay.close()
            await self.relay.start()
            warm = await self.client.ws_connect(
                self.relay_server.make_url("/warm").with_query(src="cam")
            )
            await warm.send_json({"type": "warm", "value": CODECS})
            upstream = await self.upstream()

            await upstream.close()

            await self.upstream(1)
            await warm.close()

    async def test_stream_without_the_cache_is_refused(self):
        viewer = await self.view(stream="other")

        (answer,) = await self.read(viewer, 1)
        self.assertEqual(json.loads(answer)["type"], "error")
        self.assertIn("live cache", json.loads(answer)["value"])
        self.assertEqual(self.go2rtc.requests, [])

    async def test_video_is_not_compressed_again_for_a_viewer(self):
        ws = await self.client.ws_connect(
            self.relay_server.make_url("/ws").with_query(src="cam"), compress=15
        )
        self.viewers.append(ws)

        self.assertEqual(ws.compress, 0)

    async def test_stream_takes_a_few_codec_lists_only(self):
        for index in range(4):
            await self.view(codecs=f"avc1.64002{index}")

        await self.eventually(lambda: len(self.go2rtc.requests) == 4)
        refused = await self.view(codecs="avc1.640029")

        (answer,) = await self.read(refused, 1)
        self.assertIn("live cache", json.loads(answer)["value"])
        self.assertEqual(len(self.go2rtc.requests), 4)

    async def test_oversized_codec_list_is_refused(self):
        viewer = await self.view(codecs="avc1.640029," * 60)

        (answer,) = await self.read(viewer, 1)
        self.assertIn("live cache", json.loads(answer)["value"])
        self.assertEqual(self.go2rtc.requests, [])

    async def test_viewer_leaving_before_it_asks_is_not_an_error(self):
        with self.assertNoLogs("aiohttp.server", level="ERROR"):
            ws = await self.client.ws_connect(
                self.relay_server.make_url("/ws").with_query(src="cam")
            )
            await ws.close()
            await asyncio.sleep(0.2)

    async def test_sockets_are_not_written_to_the_access_log(self):
        runner = await serve(self.relay, "127.0.0.1", 0)
        self.addAsyncCleanup(runner.cleanup)
        port = runner.addresses[0][1]

        with self.assertNoLogs("aiohttp.access", level="INFO"):
            ws = await self.client.ws_connect(f"http://127.0.0.1:{port}/ws?src=other")
            await ws.send_json({"type": "mse", "value": CODECS})
            await ws.receive(timeout=2)
            await ws.close()
            await asyncio.sleep(0.2)

    async def test_viewer_that_falls_behind_is_disconnected(self):
        with patch("frigate.live_cache.relay.VIEWER_QUEUE_BYTES", 10):
            viewer = await self.view()
            upstream = await self.upstream()
            await upstream.send_bytes(INIT)
            await upstream.send_bytes(KEY)

            async def until_closed() -> None:
                async for _ in viewer:
                    pass

            await asyncio.wait_for(until_closed(), timeout=2)
            self.assertTrue(viewer.closed)


if __name__ == "__main__":
    unittest.main()
