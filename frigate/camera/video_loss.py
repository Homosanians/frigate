"""Track whether Frigate is still receiving frames from each camera."""

from typing import NamedTuple


class VideoLossUpdate(NamedTuple):
    lost: bool
    # the last frame received, or when the camera was started or turned on if
    # it has not sent one since
    last_frame_time: float


class VideoLossTracker:
    """Decide when each camera's frames stop and resume.

    A camera is in video loss once no frame has arrived for its ffmpeg
    retry_interval, the same threshold latest.jpg uses before it falls back to
    an offline frame.
    """

    def __init__(self) -> None:
        self.baselines: dict[str, float] = {}
        self.states: dict[str, bool] = {}

    def update(
        self,
        camera: str,
        now: float,
        last_frame_time: float,
        enabled: bool,
        retry_interval: float,
    ) -> VideoLossUpdate | None:
        """Return the camera's state when it changes, including the first call."""
        if enabled:
            # a camera that has not sent a frame yet is measured from now
            baseline = self.baselines.setdefault(
                camera, 0.0 if last_frame_time > 0 else now
            )
            since = max(last_frame_time, baseline)
            lost = now - since > retry_interval
        else:
            # a turned off camera sends no frames by design, and gets a full
            # interval to deliver one once it is turned back on
            self.baselines[camera] = now
            since = now
            lost = False

        if self.states.get(camera) == lost:
            return None

        self.states[camera] = lost
        return VideoLossUpdate(lost, since)

    def remove(self, camera: str) -> None:
        self.baselines.pop(camera, None)
        self.states.pop(camera, None)
