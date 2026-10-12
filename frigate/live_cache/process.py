"""The process that serves live streams from the keyframe cache."""

import asyncio
from multiprocessing.synchronize import Event as MpEvent

from frigate.config import FrigateConfig
from frigate.const import PROCESS_PRIORITY_MED
from frigate.live_cache.relay import Relay, serve
from frigate.util.live_streams import is_transcode_stream_name
from frigate.util.process import FrigateProcess

LIVE_CACHE_PORT = 8083


def cached_streams(config: FrigateConfig) -> set[str]:
    """The go2rtc streams of the cameras that have the live cache on."""
    return {
        stream
        for name, camera in config.cameras.items()
        if camera.enabled and camera.live.cache.enabled
        for stream in camera.live.streams.values()
        # transcoding only runs while someone watches, and the linger and the
        # warm-up would keep it running
        if not is_transcode_stream_name(name, stream)
    }


class LiveCacheProcess(FrigateProcess):
    def __init__(self, config: FrigateConfig, stop_event: MpEvent) -> None:
        super().__init__(
            stop_event, PROCESS_PRIORITY_MED, name="frigate.live_cache", daemon=True
        )
        self.config = config

    def run(self) -> None:
        self.pre_run_setup(self.config.logger)
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        relay = Relay(cached_streams(self.config))
        await relay.start()
        runner = await serve(relay, "127.0.0.1", LIVE_CACHE_PORT)
        self.logger.info(f"Live cache listening on port {LIVE_CACHE_PORT}")

        while not self.stop_event.is_set():
            await asyncio.sleep(1)

        await runner.cleanup()
        await relay.close()
