"""Live log of PTZ activity for the camera Debug view."""

import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from frigate.const import (
    PTZ_DEBUG_EXTERNAL_GRACE,
    PTZ_DEBUG_MAX_ENTRIES,
    PTZ_DEBUG_RETENTION,
    PTZ_DEBUG_WATCH_TIMEOUT,
)


class PtzSource(str, Enum):
    """What made Frigate send a PTZ request or record an event."""

    command = "command"
    api = "api"
    autotrack = "autotrack"
    calibration = "calibration"
    debug = "debug"
    frigate = "frigate"


# requests that move the camera, so a position change after one of them is
# Frigate's own doing rather than an outside move
MOVE_OPERATIONS = frozenset(
    {
        "AbsoluteMove",
        "ContinuousMove",
        "GotoHomePosition",
        "GotoPreset",
        "RelativeMove",
        "Stop",
    }
)


def finite_number(value: Any) -> float | None:
    """A finite float for the PTZ log, or None.

    Cameras report unbounded ranges as INF, which the API cannot serialize.
    """
    try:
        number = None if value is None else float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    return number if number is not None and math.isfinite(number) else None


def _standing_still(pan_tilt: str | None, zoom: str | None) -> bool:
    """Whether a camera reports no move on any axis.

    Cameras that report the move status per axis show PanTilt IDLE while only
    the zoom moves, and some report no zoom status at all.
    """
    return pan_tilt == "IDLE" and zoom in (None, "IDLE")


@dataclass
class _CameraLog:
    """Log state for one camera."""

    watched_until: float = 0.0
    entries: deque[dict[str, Any]] = field(default_factory=deque)
    last_key: Hashable | None = None
    # the newest seq that was dropped before every client could see it
    dropped_seq: int = 0
    status: dict[str, Any] | None = None
    status_read_at: float | None = None
    last_move_at: float = float("-inf")
    moved_since_status: bool = False


def _rounded(position: dict[str, Any] | None) -> tuple[float | None, ...] | None:
    """A position rounded to 4 decimals, so read noise does not count as a move."""
    if position is None:
        return None

    return tuple(
        None if position.get(axis) is None else round(position[axis], 4)
        for axis in ("pan", "tilt", "zoom")
    )


class PtzEventLog:
    """A short live log of PTZ activity for cameras whose Debug view is open.

    Nothing is recorded for a camera unless the Debug view polled its log in
    the last PTZ_DEBUG_WATCH_TIMEOUT seconds, so Frigate otherwise works as if
    the log did not exist. The ONVIF event loop, the autotracker and the API
    use it from different threads. Entries are replaced, never changed, once
    recorded, so the API can serialize them while recording goes on.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._cameras: dict[str, _CameraLog] = {}
        self._seq = 0
        # changes when Frigate restarts, which tells a client seq started over
        self.session = uuid.uuid4().hex

    def wall_time(self) -> float:
        """The time entries are stamped with."""
        return self._wall_clock()

    def watch(self, camera: str) -> None:
        """Keep recording for a camera for PTZ_DEBUG_WATCH_TIMEOUT seconds."""
        with self._lock:
            now = self._clock()
            log = self._cameras.get(camera)

            if log is None:
                log = self._cameras[camera] = _CameraLog()
            elif now >= log.watched_until:
                # nothing was recorded while the log was closed, so what it
                # still holds would read as if nothing happened in between,
                # and a client that saw everything must still be told of the gap
                self._clear(log)
                self._seq += 1
                log.dropped_seq = self._seq

            log.watched_until = now + PTZ_DEBUG_WATCH_TIMEOUT

    def is_watched(self, camera: str) -> bool:
        """Whether the Debug view is polling this camera's log.

        Called for every PTZ event, so it is a single comparison without the lock.
        """
        log = self._cameras.get(camera)
        return log is not None and self._clock() < log.watched_until

    def record(
        self,
        camera: str,
        source: PtzSource,
        kind: str,
        data: dict[str, Any],
        collapse_key: Hashable | None = None,
        at: float | None = None,
    ) -> None:
        """Add an entry for a watched camera.

        An entry with the same collapse_key as the camera's last entry updates
        that entry instead, so repeats show as one row with a count.
        """
        if not self.is_watched(camera):
            return

        with self._lock:
            log = self._cameras.get(camera)

            if log is None:
                return

            self._append(log, source, kind, data, collapse_key, at)

            if kind == "request" and data.get("operation") in MOVE_OPERATIONS:
                log.last_move_at = self._clock()
                log.moved_since_status = True

    def move_started(self, camera: str) -> None:
        """Note that Frigate is sending a request that moves the camera.

        The request is recorded once the camera answers it, and the camera may
        start moving before that. Without this a status read in between would
        show a position change that Frigate has no move recorded for.
        """
        if not self.is_watched(camera):
            return

        with self._lock:
            log = self._cameras.get(camera)

            if log is None:
                return

            log.last_move_at = self._clock()
            log.moved_since_status = True

    def record_status(
        self,
        camera: str,
        source: PtzSource,
        status: dict[str, Any] | None,
        error: str | None,
        at: float | None = None,
    ) -> None:
        """Add a GetStatus result for a watched camera.

        Reads collapse while the move status stays the same, so a whole move
        is one row that keeps where it started. A position change with no move
        request from Frigate since an IDLE read is recorded as an outside move.
        """
        if not self.is_watched(camera):
            return

        with self._lock:
            log = self._cameras.get(camera)

            if log is None:
                return

            now = self._clock()
            pan_tilt = status["pan_tilt"] if status else None
            zoom = status["zoom"] if status else None
            position = status["position"] if status else None
            previous = log.status

            if (
                position is not None
                and previous is not None
                and previous["position"] is not None
                and _standing_still(previous["pan_tilt"], previous["zoom"])
                and _rounded(position) != _rounded(previous["position"])
                and not log.moved_since_status
                and log.status_read_at is not None
                and log.status_read_at - log.last_move_at >= PTZ_DEBUG_EXTERNAL_GRACE
            ):
                self._append(
                    log,
                    source,
                    "external_move",
                    {"from": previous["position"], "to": position},
                    None,
                    at,
                )

            # a run of reads while standing still ends when the position
            # changes, a run while moving does not
            key = (
                "status",
                pan_tilt,
                zoom,
                error,
                _rounded(position) if _standing_still(pan_tilt, zoom) else None,
            )
            start = position

            if key == log.last_key and log.entries:
                start = log.entries[-1]["data"].get("position_start", position)

            self._append(
                log,
                source,
                "status",
                {
                    "pan_tilt": pan_tilt,
                    "zoom": zoom,
                    "position": position,
                    "position_start": start,
                    "error": error,
                },
                key,
                at,
            )

            log.status = {
                "pan_tilt": pan_tilt,
                "zoom": zoom,
                "position": position,
                "time": self._wall_clock() if at is None else at,
                "error": error,
            }
            log.status_read_at = now
            log.moved_since_status = False

    def latest_status(self, camera: str) -> tuple[dict[str, Any] | None, float | None]:
        """The last status read while the log was open, and its age in seconds."""
        with self._lock:
            log = self._cameras.get(camera)

            if log is None or log.status_read_at is None:
                return None, None

            return log.status, self._clock() - log.status_read_at

    def entries(
        self, camera: str, after: int
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Entries newer than after, the latest seq, and whether any were missed.

        A client that has seen nothing yet (after is 0) has missed nothing.
        """
        with self._lock:
            log = self._cameras.get(camera)

            if log is None:
                return [], self._seq, False

            return (
                [entry for entry in log.entries if entry["seq"] > after],
                self._seq,
                0 < after < log.dropped_seq,
            )

    def remove_camera(self, camera: str) -> None:
        """Forget a camera removed at runtime."""
        with self._lock:
            self._cameras.pop(camera, None)

    def _append(
        self,
        log: _CameraLog,
        source: PtzSource,
        kind: str,
        data: dict[str, Any],
        collapse_key: Hashable | None,
        at: float | None,
    ) -> None:
        stamp = self._wall_clock() if at is None else at
        self._seq += 1

        if collapse_key is not None and collapse_key == log.last_key and log.entries:
            last = log.entries[-1]
            log.entries[-1] = {
                **last,
                "seq": self._seq,
                "repeats": last["repeats"] + 1,
                "until": stamp,
                "data": data,
            }
        else:
            log.entries.append(
                {
                    "id": self._seq,
                    "seq": self._seq,
                    "time": stamp,
                    "source": PtzSource(source).value,
                    "kind": kind,
                    "repeats": 1,
                    "until": None,
                    "data": data,
                }
            )
            log.last_key = collapse_key

        self._prune(log)

    def _prune(self, log: _CameraLog) -> None:
        oldest = self._wall_clock() - PTZ_DEBUG_RETENTION

        while log.entries and (
            len(log.entries) > PTZ_DEBUG_MAX_ENTRIES
            or (log.entries[0]["until"] or log.entries[0]["time"]) < oldest
        ):
            dropped = log.entries.popleft()
            log.dropped_seq = max(log.dropped_seq, dropped["seq"])

        if not log.entries:
            log.last_key = None

    def _clear(self, log: _CameraLog) -> None:
        if log.entries:
            log.dropped_seq = max(entry["seq"] for entry in log.entries)

        log.entries.clear()
        log.last_key = None
        log.status = None
        log.status_read_at = None
        log.last_move_at = float("-inf")
        log.moved_since_status = False
