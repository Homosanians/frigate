"""Replace saved detect stream images with frames from the main stream."""

import logging
import os
import subprocess as sp
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from queue import Empty, Full, Queue

import cv2
import numpy as np
import requests
from peewee import DoesNotExist

from frigate.config import FrigateConfig
from frigate.config.camera.image_source import ImageSourceStreamEnum
from frigate.const import STREAM_TYPE_MAIN
from frigate.models import Recordings
from frigate.util.image import get_image_from_recording
from frigate.util.live_streams import GO2RTC_API, is_transcode_stream_name

logger = logging.getLogger(__name__)

# a job is dropped when the main stream frame is still unavailable after this
JOB_TIMEOUT = 120.0
# delay before looking again for a recording that was not in the db yet
RETRY_INTERVAL = 2.0
# every upgrade spawns ffmpeg, so each consumer only keeps this many waiting
MAX_JOBS_PER_KEY = 6
MAX_FETCH_QUEUE = 32
GO2RTC_TIMEOUT = 5
# the detect stream is scaled and encoded separately from the main stream, so
# what it saw may sit a few detect pixels off. the region is matched within
# this many pixels, at a small cost per pixel so the exact position wins
MATCH_SHIFT = 3
MATCH_SHIFT_PENALTY = 0.005
# frames of a still subject only differ by noise. scores this close count as a
# tie, which goes to the frame closest to the detect frame time
MATCH_TIE_MARGIN = 0.002
# standard deviation of pixel values below which a region is too flat to match
MIN_CONTRAST = 2.0
SEGMENT_DECODE_TIMEOUT = 30

Box = tuple[int, int, int, int]

# called on the maintainer thread with the main stream frame (BGR) and the
# x and y factors that scale detect coordinates up to it
HiResHandler = Callable[[np.ndarray, float, float], None]


@dataclass
class Reference:
    """What the detect stream saw, used to find the same moment in the main stream."""

    # grayscale detect stream pixels inside the box
    image: np.ndarray
    # where those pixels sit in the detect frame
    box: Box


def scale_box(box: Box | list[int], scale_x: float, scale_y: float) -> Box:
    """Scale a box in detect coordinates to another frame size."""
    return (
        int(box[0] * scale_x),
        int(box[1] * scale_y),
        int(box[2] * scale_x),
        int(box[3] * scale_y),
    )


def expand_box(box: Box, frame_shape: tuple[int, ...], factor: float) -> Box:
    """Grow a box by a factor of its size on every axis, clipped to the frame.

    Matching the streams in time is only as fine as the detect frame rate, so
    a scaled box is used as a search area and is padded to hold the subject.
    """
    pad_x = int((box[2] - box[0]) * factor / 2)
    pad_y = int((box[3] - box[1]) * factor / 2)
    return clip_box(
        (box[0] - pad_x, box[1] - pad_y, box[2] + pad_x, box[3] + pad_y), frame_shape
    )


def clip_box(box: Box, frame_shape: tuple[int, ...]) -> Box:
    """Clip a box to the bounds of a frame."""
    height, width = frame_shape[:2]
    return (
        max(0, min(box[0], width)),
        max(0, min(box[1], height)),
        max(0, min(box[2], width)),
        max(0, min(box[3], height)),
    )


def reference_from_yuv(
    yuv_frame: np.ndarray, box: Box | list[int], detect_height: int
) -> Reference | None:
    """Build a reference from a region of a detect stream I420 frame."""
    # the luma plane is the top of an I420 frame
    luma = yuv_frame[:detect_height]
    left, top, right, bottom = clip_box((box[0], box[1], box[2], box[3]), luma.shape)

    if right <= left or bottom <= top:
        return None

    return Reference(luma[top:bottom, left:right].copy(), (left, top, right, bottom))


def reference_from_bgr(frame: np.ndarray, box: Box) -> Reference | None:
    """Build a reference from a region of a detect stream BGR frame."""
    left, top, right, bottom = clip_box(box, frame.shape)

    if right <= left or bottom <= top:
        return None

    gray = cv2.cvtColor(frame[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
    return Reference(gray, (left, top, right, bottom))


def match_score(frame: np.ndarray, reference: Reference) -> float:
    """Correlation of a detect sized grayscale frame with a reference, -1 to 1.

    Normalized correlation ignores the brightness and contrast differences
    between two encodes of the same scene, which a plain pixel difference
    counts as a mismatch.
    """
    left, top, right, bottom = reference.box
    region = frame[top:bottom, left:right]

    if region.shape != reference.image.shape or region.size == 0:
        return -1.0

    # a featureless region has no variance to correlate, and opencv reports
    # that as a perfect match
    if region.std() < MIN_CONTRAST or reference.image.std() < MIN_CONTRAST:
        return -1.0

    search_left = max(0, left - MATCH_SHIFT)
    search_top = max(0, top - MATCH_SHIFT)
    scores = cv2.matchTemplate(
        frame[search_top : bottom + MATCH_SHIFT, search_left : right + MATCH_SHIFT],
        reference.image,
        cv2.TM_CCOEFF_NORMED,
    )
    rows, columns = np.indices(scores.shape)
    shift = np.maximum(
        np.abs(rows - (top - search_top)), np.abs(columns - (left - search_left))
    )
    # a flat patch beside the region has no variance either
    scores = np.where(np.isfinite(scores), scores, -1.0)

    return float((scores - MATCH_SHIFT_PENALTY * shift).max())


def find_synced_offset(
    frames: np.ndarray,
    fps: float,
    offset: float,
    reference: Reference,
    threshold: float,
    search_before: float,
    search_after: float,
) -> tuple[float, float] | None:
    """Find the time in a segment showing the moment the reference was taken.

    frames are the segment's frames at the detect size and rate, offset is
    where the detect frame time falls in the segment and may lie outside it.
    Returns the time and its score, or None when nothing in the search
    window reaches the threshold.
    """
    matches: list[tuple[float, float]] = []

    for index, frame in enumerate(frames):
        frame_time = index / fps

        if frame_time < offset - search_before or frame_time > offset + search_after:
            continue

        score = match_score(frame, reference)

        if score >= threshold:
            matches.append((frame_time, score))

    if not matches:
        return None

    best_score = max(score for _, score in matches)

    return min(
        (match for match in matches if match[1] >= best_score - MATCH_TIE_MARGIN),
        key=lambda match: abs(match[0] - offset),
    )


@lru_cache(maxsize=4)
def decode_segment(
    ffmpeg_path: str, path: str, width: int, height: int, fps: int
) -> np.ndarray:
    """Decode a recording segment to grayscale frames at the detect size and rate.

    Several images are usually saved from the same segment, so the most recent
    ones are kept.
    """
    try:
        process = sp.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                path,
                "-vf",
                f"fps={fps},scale={width}:{height}",
                "-pix_fmt",
                "gray",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            timeout=SEGMENT_DECODE_TIMEOUT,
        )
    except sp.TimeoutExpired:
        return np.zeros((0, height, width), np.uint8)

    frame_count = len(process.stdout) // (width * height)
    return np.frombuffer(
        process.stdout[: frame_count * width * height], np.uint8
    ).reshape(frame_count, height, width)


def go2rtc_stream_name(config: FrigateConfig, camera: str) -> str | None:
    """Return the first live stream of a camera that go2rtc serves untranscoded."""
    camera_config = config.cameras.get(camera)

    if camera_config is None:
        return None

    go2rtc_streams = config.go2rtc.model_dump().get("streams") or {}

    for name in camera_config.live.streams.values():
        if name in go2rtc_streams and not is_transcode_stream_name(camera, name):
            return name

    return None


def find_recording(camera: str, frame_time: float) -> tuple[str, float] | None:
    """Return the main stream segment holding a frame time and the offset into it."""
    try:
        recording = (
            Recordings.select(Recordings.path, Recordings.start_time)
            .where(
                (frame_time >= Recordings.start_time)
                & (frame_time <= Recordings.end_time)
            )
            .where(Recordings.camera == camera)
            .where(Recordings.stream_type == STREAM_TYPE_MAIN)
            .order_by(Recordings.start_time.desc())
            .limit(1)
            .get()
        )
    except DoesNotExist:
        return None

    return str(recording.path), frame_time - float(recording.start_time)


def find_recordings(camera: str, start: float, end: float) -> list[tuple[str, float]]:
    """Return the main stream segments overlapping a time range with their start times."""
    recordings = (
        Recordings.select(Recordings.path, Recordings.start_time)
        .where((Recordings.start_time <= end) & (Recordings.end_time >= start))
        .where(Recordings.camera == camera)
        .where(Recordings.stream_type == STREAM_TYPE_MAIN)
        .order_by(Recordings.start_time.asc())
    )

    return [(str(r.path), float(r.start_time)) for r in recordings]


def decode_image(image_data: bytes | None) -> np.ndarray | None:
    """Decode encoded image bytes into a BGR frame."""
    if not image_data:
        return None

    frame = cv2.imdecode(np.frombuffer(image_data, dtype=np.uint8), cv2.IMREAD_COLOR)

    if frame is None or frame.size == 0:
        return None  # type: ignore[unreachable]

    return frame


def find_synced_frame(
    config: FrigateConfig, camera: str, frame_time: float, reference: Reference
) -> tuple[str, float] | None:
    """Find the main stream segment and offset showing what the detect stream saw.

    The search window can reach into the neighbouring segments, so every
    segment it touches is searched.
    """
    camera_config = config.cameras[camera]
    detect = camera_config.detect
    image_source = camera_config.image_source
    best: tuple[float, float, str, float] | None = None

    for path, start_time in find_recordings(
        camera,
        frame_time - image_source.search_before,
        frame_time + image_source.search_after,
    ):
        expected = frame_time - start_time
        match = find_synced_offset(
            decode_segment(
                config.ffmpeg.ffmpeg_path,
                path,
                detect.width,
                detect.height,
                detect.fps,
            ),
            detect.fps,
            expected,
            reference,
            image_source.match_threshold,
            image_source.search_before,
            image_source.search_after,
        )

        if match is None:
            continue

        offset, score = match
        drift = offset - expected

        # the best score wins, a tie goes to the frame closest to the expected time
        if (
            best is None
            or score > best[0] + MATCH_TIE_MARGIN
            or (score >= best[0] - MATCH_TIE_MARGIN and abs(drift) < abs(best[1]))
        ):
            best = (score, drift, path, offset)

    if best is None:
        logger.debug(
            f"No main stream frame of {camera} matches the detect image near {frame_time}"
        )
        return None

    logger.debug(
        f"Main stream of {camera} is {best[1]:+.1f}s from the detect stream, score {best[0]:.2f}"
    )
    return best[2], best[3]


def fetch_recording_frame(
    config: FrigateConfig,
    camera: str,
    frame_time: float,
    reference: Reference | None,
) -> np.ndarray | None:
    """Decode the main stream recording frame that a saved image was taken from.

    Recording timestamps lag or lead the detect stream by up to seconds, so
    with a reference the recording is searched around the frame time for the
    frame showing the same moment, and nothing is returned when there is none.
    """
    if reference is None:
        recording = find_recording(camera, frame_time)
    else:
        recording = find_synced_frame(config, camera, frame_time, reference)

    if recording is None:
        return None

    path, offset = recording

    # png keeps the frame lossless, mjpeg at ffmpeg defaults is visibly blocky
    return decode_image(get_image_from_recording(config.ffmpeg, path, offset, "png"))


def fetch_go2rtc_frame(
    config: FrigateConfig, camera: str, stream_name: str, reference: Reference | None
) -> np.ndarray | None:
    """Fetch the current frame of a go2rtc stream.

    go2rtc only has the present, so with a reference the frame is dropped
    unless it still shows what the detect stream saw.
    """
    try:
        response = requests.get(
            f"{GO2RTC_API}/frame.jpeg",
            params={"src": stream_name},
            timeout=GO2RTC_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.debug(f"Failed to fetch go2rtc frame for {stream_name}: {e}")
        return None

    if response.status_code != 200:
        return None

    frame = decode_image(response.content)

    if frame is None or reference is None:
        return frame

    detect = config.cameras[camera].detect
    small = cv2.cvtColor(
        cv2.resize(frame, (detect.width, detect.height), interpolation=cv2.INTER_AREA),
        cv2.COLOR_BGR2GRAY,
    )

    if (
        match_score(small, reference)
        < config.cameras[camera].image_source.match_threshold
    ):
        logger.debug(f"go2rtc frame of {camera} no longer matches the detect image")
        return None

    return frame


def replace_image(
    file: str, image: np.ndarray, params: list[int] | None = None
) -> bool:
    """Overwrite a saved image that still exists, without exposing a partial file.

    The image is skipped if it is gone, since it was trimmed, cleaned up, or
    moved elsewhere in the meantime.
    """
    if image.size == 0 or not os.path.exists(file):
        return False

    ret, encoded = cv2.imencode(os.path.splitext(file)[1], image, params or [])

    if not ret:
        return False

    # readers such as the api may open the image while it is being written
    temp_file = f"{file}.tmp"

    with open(temp_file, "wb") as f:
        f.write(encoded.tobytes())

    os.replace(temp_file, file)
    return True


@dataclass
class HiResJob:
    camera: str
    frame_time: float
    key: str
    handler: HiResHandler
    reference: Reference | None = None
    created: float = field(default_factory=time.monotonic)
    next_try: float = 0.0


class HiResUpgrader:
    """Fetches main stream frames for saved images once they become available.

    Frames come from the main stream recording, which only exists once its
    segment has been saved, so jobs wait for that. Cameras that do not record
    fall back to the current go2rtc frame. Fetching runs on a worker thread,
    handlers run on the thread that calls process().
    """

    def __init__(
        self,
        config: FrigateConfig,
        recording_fetcher: Callable[
            [FrigateConfig, str, float, Reference | None], np.ndarray | None
        ] = fetch_recording_frame,
        live_fetcher: Callable[
            [FrigateConfig, str, str, Reference | None], np.ndarray | None
        ] = fetch_go2rtc_frame,
        recording_finder: Callable[
            [str, float], tuple[str, float] | None
        ] = find_recording,
        start_worker: bool = True,
    ) -> None:
        self.config = config
        self.recording_fetcher = recording_fetcher
        self.live_fetcher = live_fetcher
        self.recording_finder = recording_finder

        self.lock = threading.Lock()
        self.pending: list[HiResJob] = []
        self.job_counts: dict[str, int] = {}
        self.completed: deque[tuple[HiResJob, np.ndarray | None]] = deque()
        self.recordings_available_through: dict[str, float] = {}

        self.fetch_queue: Queue[tuple[HiResJob, Callable[[], np.ndarray | None]]] = (
            Queue(maxsize=MAX_FETCH_QUEUE)
        )
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        if start_worker:
            self.worker = threading.Thread(
                target=self._fetch_loop, daemon=True, name="hires_upgrader_worker"
            )
            self.worker.start()

    def enabled(self, camera: str) -> bool:
        """Whether saved images of a camera should be taken from the main stream."""
        camera_config = self.config.cameras.get(camera)

        return (
            camera_config is not None
            and camera_config.image_source.stream != ImageSourceStreamEnum.detect
        )

    def submit(
        self,
        camera: str,
        frame_time: float,
        key: str,
        handler: HiResHandler,
        reference: Reference | None = None,
    ) -> bool:
        """Queue a main stream frame for a saved image. Safe to call from any thread.

        key groups the jobs of one consumer for one object so a burst of saves
        cannot flood the queue. reference is what the detect stream saw and is
        used to line the streams up in time; without one the frame at the
        detect frame time is used as is.
        """
        if not self.enabled(camera):
            return False

        job = HiResJob(camera, frame_time, key, handler, reference)

        with self.lock:
            if self.job_counts.get(key, 0) >= MAX_JOBS_PER_KEY:
                return False

            if self.config.cameras[camera].record.enabled:
                self.pending.append(job)
            else:
                # go2rtc only has the current frame, so it is fetched right away
                stream_name = go2rtc_stream_name(self.config, camera)

                if stream_name is None or not self._enqueue_fetch(
                    job,
                    lambda: self.live_fetcher(
                        self.config, camera, stream_name, reference
                    ),
                ):
                    return False

            self.job_counts[key] = self.job_counts.get(key, 0) + 1

        return True

    def update_recordings_available(
        self, camera: str, available_through: float | None
    ) -> None:
        """Record how far the saved main stream recordings of a camera reach."""
        if available_through is None:
            self.recordings_available_through.pop(camera, None)
        else:
            self.recordings_available_through[camera] = available_through

    def process(self) -> None:
        """Release jobs whose recordings are saved and run finished handlers."""
        self._release_pending()

        while True:
            with self.lock:
                if not self.completed:
                    break

                job, frame = self.completed.popleft()
                self._finish(job)

            if frame is not None:
                self._run_handler(job, frame)

    def stop(self) -> None:
        self.stop_event.set()

        if self.worker is not None:
            self.worker.join(timeout=5.0)

    def _finish(self, job: HiResJob) -> None:
        count = self.job_counts.get(job.key, 0) - 1

        if count > 0:
            self.job_counts[job.key] = count
        else:
            self.job_counts.pop(job.key, None)

    def _enqueue_fetch(
        self, job: HiResJob, fetch: Callable[[], np.ndarray | None]
    ) -> bool:
        try:
            self.fetch_queue.put_nowait((job, fetch))
            return True
        except Full:
            logger.debug("High resolution fetch queue full, dropping job")
            return False

    def _release_pending(self) -> None:
        now = time.monotonic()

        with self.lock:
            if not self.pending:
                return

            waiting: list[HiResJob] = []

            for job in self.pending:
                if now - job.created > JOB_TIMEOUT:
                    logger.debug(
                        f"No main stream recording for {job.camera} at {job.frame_time}, keeping the detect image"
                    )
                    self._finish(job)
                    continue

                available = self.recordings_available_through.get(job.camera)
                needed = job.frame_time

                if job.reference is not None and job.camera in self.config.cameras:
                    # the search for the matching frame reaches past the frame time
                    needed += self.config.cameras[job.camera].image_source.search_after

                if available is None or needed >= available:
                    waiting.append(job)
                    continue

                if now < job.next_try:
                    waiting.append(job)
                    continue

                if self.recording_finder(job.camera, job.frame_time) is None:
                    # the segment was probed but its row is not written yet
                    job.next_try = now + RETRY_INTERVAL
                    waiting.append(job)
                    continue

                if not self._enqueue_fetch(
                    job,
                    lambda job=job: self.recording_fetcher(  # type: ignore[misc]
                        self.config, job.camera, job.frame_time, job.reference
                    ),
                ):
                    self._finish(job)

            self.pending = waiting

    def _fetch_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                job, fetch = self.fetch_queue.get(timeout=0.5)
            except Empty:
                continue

            try:
                frame = fetch()
            except Exception:
                logger.exception("Failed to fetch high resolution frame")
                frame = None

            with self.lock:
                self.completed.append((job, frame))

    def _run_handler(self, job: HiResJob, frame: np.ndarray) -> None:
        camera_config = self.config.cameras.get(job.camera)

        if camera_config is None:
            return

        detect = camera_config.detect
        height, width = frame.shape[:2]

        if (
            camera_config.image_source.stream == ImageSourceStreamEnum.auto
            and width * height <= detect.width * detect.height
        ):
            logger.debug(
                f"Main stream frame for {job.camera} is not larger than detect, keeping the detect image"
            )
            return

        try:
            job.handler(frame, width / detect.width, height / detect.height)
        except Exception:
            logger.exception("Failed to apply high resolution frame")
