"""The frames a new live viewer needs to start without waiting for a keyframe."""

import time
from collections.abc import Callable

from frigate.live_cache.fmp4 import NON_SYNC_SAMPLE, video_track_id

# a stream that goes this large or this long between keyframes is not held.
# the player trims its buffer to 15 seconds, which would cut the keyframe off
# a backlog longer than that
MAX_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 10
# a stream whose frames stopped arriving has nothing current to start from
STALE_SECONDS = 2


class GopCache:
    """The init segment and every fragment since the last video keyframe."""

    def __init__(
        self,
        max_bytes: int = MAX_BYTES,
        max_seconds: float = MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.init: bytes | None = None
        self.has_video = False
        self._video_track: int | None = None
        self._max_bytes = max_bytes
        self._max_seconds = max_seconds
        self._clock = clock
        self._fragments: list[bytes] = []
        self._bytes = 0
        self._started = 0.0
        self._latest = 0.0

    def add_init(self, init: bytes) -> None:
        self.init = init
        self._video_track = video_track_id(init)
        self.has_video = self._video_track is not None
        self._clear()

    def add_fragment(
        self, fragment: bytes, track_id: int | None, sample_flags: int | None
    ) -> bool:
        """Store a fragment and report whether it is a video keyframe."""
        keyframe = (
            self.has_video
            and track_id == self._video_track
            and sample_flags is not None
            and not sample_flags & NON_SYNC_SAMPLE
        )

        if keyframe:
            self._fragments = [fragment]
            self._bytes = len(fragment)
            self._started = self._latest = self._clock()
            return True

        # frames with no keyframe before them cannot be decoded
        if not self._fragments:
            return False

        if (
            self._bytes + len(fragment) > self._max_bytes
            or self._clock() - self._started > self._max_seconds
        ):
            self._clear()
            return False

        self._fragments.append(fragment)
        self._bytes += len(fragment)
        self._latest = self._clock()
        return False

    def backlog(self) -> list[bytes]:
        if self._clock() - self._latest > STALE_SECONDS:
            return []

        return list(self._fragments)

    def _clear(self) -> None:
        self._fragments = []
        self._bytes = 0
