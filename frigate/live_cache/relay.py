"""Shares one go2rtc MSE connection per stream between live viewers and gives
each new viewer the frames since the last keyframe."""

import asyncio
import json
import logging

import aiohttp
from aiohttp import web

from frigate.live_cache.cache import GopCache
from frigate.live_cache.fmp4 import SegmentReader

logger = logging.getLogger(__name__)

GO2RTC_WS_URL = "http://127.0.0.1:1984/api/ws"
# how long a stream stays connected after its last viewer or warm request
LINGER_SECONDS = 30
# a viewer further behind than this is disconnected and connects again
VIEWER_QUEUE_BYTES = 32 * 1024 * 1024
# how long a browser gets to send its codec list
HANDSHAKE_SECONDS = 10
# how often a stream a dashboard asked for is tried again after it dropped
WARM_RETRY_SECONDS = 15
# browsers send a list of about 80 characters, and only a handful of
# different ones exist. each list costs a go2rtc connection and a cache
MAX_CODECS_LENGTH = 512
MAX_CODEC_LISTS_PER_STREAM = 4
LIVE_MARKER = json.dumps({"type": "live"})


def _error(message: str) -> str:
    return json.dumps({"type": "error", "value": f"live cache: {message}"})


class Viewer:
    """What is still to be sent to one browser."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self.queued = 0
        self.started = False
        # no backlog was sent, so frames before the next keyframe are no use
        self.needs_keyframe = False

    def put(self, item: bytes | str | None) -> None:
        if isinstance(item, bytes):
            self.queued += len(item)

        self.queue.put_nowait(item)

    async def drain(self, ws: web.WebSocketResponse) -> None:
        while True:
            item = await self.queue.get()

            if item is None:
                return

            if isinstance(item, bytes):
                self.queued -= len(item)
                await ws.send_bytes(item)
            else:
                await ws.send_str(item)


class Upstream:
    """One go2rtc connection and the viewers that share it."""

    def __init__(self, relay: "Relay", stream: str, codecs: str) -> None:
        self.relay = relay
        self.key = (stream, codecs)
        self.cache = GopCache()
        self.reply: str | None = None
        self.error: str | None = None
        self.closed = False
        self.viewers: set[Viewer] = set()
        self._reader = SegmentReader()
        self._linger: asyncio.TimerHandle | None = None
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        stream, codecs = self.key

        try:
            async with self.relay.session.ws_connect(
                self.relay.go2rtc_url, params={"src": stream}, max_msg_size=0
            ) as ws:
                await ws.send_str(json.dumps({"type": "mse", "value": codecs}))

                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        self._on_data(message.data)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        if not self._on_text(message.data):
                            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Live cache lost {stream}: {e}")
            self.error = self.error or _error(f"go2rtc connection failed: {e}")
        finally:
            self._finish()

    def _on_text(self, text: str) -> bool:
        """Handle a go2rtc message, returning False once the stream is over."""
        try:
            kind = json.loads(text).get("type")
        except (ValueError, AttributeError):
            return True

        if kind == "error":
            self.error = text
            return False

        if kind == "mse" and self.reply is None:
            self.reply = text

            for viewer in self.viewers:
                viewer.put(text)

        return True

    def _on_data(self, data: bytes) -> None:
        for segment in self._reader.feed(data):
            if segment.init:
                self.cache.add_init(segment.data)

                for viewer in self.viewers:
                    self._start(viewer)

                continue

            keyframe = self.cache.add_fragment(
                segment.data, segment.track_id, segment.sample_flags
            )

            for viewer in list(self.viewers):
                if not viewer.started:
                    continue

                if viewer.needs_keyframe:
                    if not keyframe:
                        continue

                    viewer.needs_keyframe = False

                viewer.put(segment.data)

                if viewer.queued > VIEWER_QUEUE_BYTES:
                    self.viewers.discard(viewer)
                    viewer.put(None)

    def _start(self, viewer: Viewer) -> None:
        """Queue the init segment and the backlog, which has no await in it so
        the viewer can neither miss nor repeat a fragment."""
        backlog = self.cache.backlog()
        viewer.put(self.cache.init)

        for fragment in backlog:
            viewer.put(fragment)

        viewer.put(LIVE_MARKER)
        viewer.started = True
        viewer.needs_keyframe = not backlog and self.cache.has_video

    def attach(self, viewer: Viewer) -> None:
        self.cancel_linger()
        self.viewers.add(viewer)

        if self.reply is not None:
            viewer.put(self.reply)

            if self.cache.init is not None:
                self._start(viewer)

    def detach(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        self.relay.release(self)

    def linger(self) -> None:
        self.cancel_linger()
        self._linger = asyncio.get_running_loop().call_later(
            LINGER_SECONDS, self._expire
        )

    def cancel_linger(self) -> None:
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None

    def _expire(self) -> None:
        self._linger = None

        if not self.viewers and not self.relay.wanted.get(self.key):
            self.close()

    def close(self) -> None:
        self._task.cancel()

    async def wait_closed(self) -> None:
        await asyncio.gather(self._task, return_exceptions=True)

    def _finish(self) -> None:
        self.closed = True
        self.cancel_linger()

        for viewer in self.viewers:
            if self.error is not None:
                # go2rtc keeps a viewer's socket open after an error, and the
                # player's fallback timing relies on that
                viewer.put(self.error)
            else:
                viewer.put(None)

        self.viewers.clear()
        self.relay.forget(self)


async def serve(relay: "Relay", host: str, port: int) -> web.AppRunner:
    # the access log would get a line for every viewer that leaves
    runner = web.AppRunner(relay.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner


class Relay:
    def __init__(self, streams: set[str], go2rtc_url: str = GO2RTC_WS_URL) -> None:
        # the go2rtc streams the cache may serve
        self.streams = streams
        self.go2rtc_url = go2rtc_url
        self.upstreams: dict[tuple[str, str], Upstream] = {}
        # how many dashboards asked for each (stream, codecs) to stay connected
        self.wanted: dict[tuple[str, str], int] = {}
        self.session: aiohttp.ClientSession
        self._retry: asyncio.Task | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
        self._retry = asyncio.create_task(self._retry_wanted())

    async def close(self) -> None:
        if self._retry is not None:
            self._retry.cancel()
            await asyncio.gather(self._retry, return_exceptions=True)
            self._retry = None

        upstreams = list(self.upstreams.values())

        for upstream in upstreams:
            upstream.close()

        for upstream in upstreams:
            await upstream.wait_closed()

        await self.session.close()

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes([web.get("/ws", self.playback), web.get("/warm", self.warm)])
        return app

    def acquire(self, stream: str, codecs: str) -> Upstream | None:
        """The connection for a stream and codec list, or None when the
        stream already has as many codec lists as it may."""
        key = (stream, codecs)
        upstream = self.upstreams.get(key)

        if upstream is None or upstream.closed:
            others = sum(1 for name, _ in self.upstreams if name == stream)

            if len(codecs) > MAX_CODECS_LENGTH or others >= MAX_CODEC_LISTS_PER_STREAM:
                return None

            upstream = Upstream(self, stream, codecs)
            self.upstreams[key] = upstream

        upstream.cancel_linger()
        return upstream

    def release(self, upstream: Upstream) -> None:
        """Let a stream nothing uses any more go after the linger."""
        if upstream.closed or upstream.viewers or self.wanted.get(upstream.key):
            return

        upstream.linger()

    def forget(self, upstream: Upstream) -> None:
        if self.upstreams.get(upstream.key) is upstream:
            del self.upstreams[upstream.key]

    async def _retry_wanted(self) -> None:
        while True:
            await asyncio.sleep(WARM_RETRY_SECONDS)

            for stream, codecs in list(self.wanted):
                self.acquire(stream, codecs)

    async def _codecs(self, ws: web.WebSocketResponse, kind: str) -> str | None:
        """The codec list a browser opens its socket with."""
        try:
            message = await ws.receive(timeout=HANDSHAKE_SECONDS)
            data = json.loads(message.data)
        except (TimeoutError, TypeError, ValueError):
            return None

        if (
            not isinstance(data, dict)
            or data.get("type") != kind
            or not isinstance(data.get("value"), str)
        ):
            return None

        return data["value"]

    async def playback(self, request: web.Request) -> web.WebSocketResponse:
        # the video is compressed already
        ws = web.WebSocketResponse(heartbeat=30, compress=False)
        await ws.prepare(request)
        stream = request.query.get("src", "")
        codecs = await self._codecs(ws, "mse")
        upstream = (
            self.acquire(stream, codecs)
            if codecs is not None and stream in self.streams
            else None
        )

        if upstream is None:
            # a browser that left before asking has nobody to tell
            if not ws.closed:
                await ws.send_str(_error("stream not available"))

            await ws.close()
            return ws

        viewer = Viewer()
        upstream.attach(viewer)
        tasks = {
            asyncio.create_task(viewer.drain(ws)),
            asyncio.create_task(self._until_closed(ws)),
        }

        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)
            upstream.detach(viewer)
            await ws.close()

        return ws

    async def warm(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, compress=False)
        await ws.prepare(request)
        codecs = await self._codecs(ws, "warm")
        keys = (
            []
            if codecs is None
            else [
                (stream, codecs)
                for stream in dict.fromkeys(request.query.getall("src", []))
                if stream in self.streams
            ]
        )

        # a stream that is out of codec lists is simply not kept warm
        keys = [key for key in keys if self.acquire(*key) is not None]

        for key in keys:
            self.wanted[key] = self.wanted.get(key, 0) + 1

        try:
            await self._until_closed(ws)
        finally:
            for key in keys:
                self.wanted[key] -= 1

                if not self.wanted[key]:
                    del self.wanted[key]

                upstream = self.upstreams.get(key)

                if upstream is not None:
                    self.release(upstream)

        return ws

    @staticmethod
    async def _until_closed(ws: web.WebSocketResponse) -> None:
        async for _ in ws:
            pass
