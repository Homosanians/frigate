"""Tests for autotracker state that must survive runtime config changes.

Regression coverage for a family of bugs where per-camera autotracker state was
built once at startup and never revisited. A camera that is added or enabled
after startup, or has autotracking enabled from the UI, would either raise a
KeyError on the autotracker thread or silently keep the wrong state:

- autotracker_init only got an entry for cameras enabled when PtzAutoTracker was
  constructed, so runtime-enabled cameras raised KeyError on lookup.
- _disable only changed the main process config, so the camera process kept
  running its motion estimator for a camera that could not autotrack.
"""

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from norfair.camera_motion import HomographyTransformation, TranslationTransformation

from frigate.camera import PTZMetrics
from frigate.config import FrigateConfig
from frigate.config.camera.updater import CameraConfigUpdateEnum
from frigate.ptz.autotrack import (
    PtzAutoTracker,
    PtzMotionEstimator,
    PtzSettleObserver,
    calculate_max_target_box,
    frame_captured_after_ptz_move,
    frame_shift,
    ptz_moving_at_frame_time,
)

CAMERA = "ptz_cam"


def _config(
    autotracking_enabled: bool, stream_latency: float | None = None
) -> FrigateConfig:
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
                    "zones": {"zone": {"coordinates": "0,0,1,0,1,1,0,1"}},
                    "onvif": {
                        "host": "10.0.0.1",
                        "autotracking": {
                            "enabled": autotracking_enabled,
                            "required_zones": ["zone"],
                            "stream_latency": stream_latency,
                        },
                    },
                }
            },
        }
    )


def _make_tracker(
    autotracking_enabled: bool = True, stream_latency: float | None = None
) -> PtzAutoTracker:
    """Build a PtzAutoTracker without invoking __init__, which would try to set up
    onvif over the network. Only the config/metrics state is relevant here."""
    tracker = PtzAutoTracker.__new__(PtzAutoTracker)
    tracker.config = _config(autotracking_enabled, stream_latency)
    tracker.ptz_metrics = {CAMERA: PTZMetrics()}
    tracker.onvif = MagicMock()
    tracker.dispatcher = MagicMock()
    tracker.config_subscriber = MagicMock()
    tracker.autotracker_init = {}
    tracker.calibrating = {}
    tracker.tracked_object = {}
    return tracker


class TestAutotrackerInitGuards(unittest.IsolatedAsyncioTestCase):
    async def test_camera_maintenance_returns_early_when_not_initialized(self) -> None:
        # a camera enabled at runtime has no autotracker_init entry, which used to
        # raise KeyError and kill the autotracker thread for every camera
        tracker = _make_tracker()
        self.assertNotIn(CAMERA, tracker.autotracker_init)

        await tracker.camera_maintenance(CAMERA)

        tracker.onvif.get_camera_status.assert_not_called()

    async def test_camera_maintenance_returns_early_when_init_incomplete(self) -> None:
        # autotracker_init is seeded False for enabled cameras before setup runs
        tracker = _make_tracker()
        tracker.autotracker_init[CAMERA] = False

        await tracker.camera_maintenance(CAMERA)

        tracker.onvif.get_camera_status.assert_not_called()


class TestAutotrackerEnqueueMove(unittest.TestCase):
    def _enqueue(self, pan: float, tilt: float, zoom: float) -> MagicMock:
        tracker = _make_tracker()
        tracker.move_queues = {CAMERA: MagicMock()}
        tracker.move_queue_locks = {CAMERA: MagicMock()}
        tracker.move_queue_locks[CAMERA].locked.return_value = False

        tracker._enqueue_move(CAMERA, 1000.0, pan, tilt, zoom)

        return tracker.onvif.loop.call_soon_threadsafe

    def test_move_is_clipped_to_the_onvif_range(self) -> None:
        # velocity estimates can push the predicted centroid outside the frame
        call_soon = self._enqueue(1.7, -2.5, 0.4)

        call_soon.assert_called_once()
        self.assertEqual(call_soon.call_args.args[1], (1000.0, 1.0, -1.0, 0.4))

    def test_empty_move_is_not_enqueued(self) -> None:
        self._enqueue(0, 0, 0).assert_not_called()


# a move that started at 1000 and that the camera reported finished at 1001.
# frames are stamped when Frigate receives them, so the video can keep showing
# the move after 1001
MOVE_START = 1000.0
MOVE_STOP = 1001.0


class TestVideoStopTime(unittest.TestCase):
    """video_stop_time is the receive time of the last frame that still showed
    the move, 0 until the video has settled."""

    def test_frame_after_stop_is_moving_until_the_video_settles(self) -> None:
        self.assertTrue(ptz_moving_at_frame_time(1001.5, MOVE_START, MOVE_STOP, 0.0))

    def test_frame_after_video_stop_is_not_moving(self) -> None:
        self.assertTrue(ptz_moving_at_frame_time(1001.6, MOVE_START, MOVE_STOP, 1001.6))
        self.assertFalse(
            ptz_moving_at_frame_time(1001.8, MOVE_START, MOVE_STOP, 1001.6)
        )

    def test_frame_before_the_move_is_not_moving(self) -> None:
        self.assertFalse(ptz_moving_at_frame_time(999.8, MOVE_START, 0.0, 0.0))

    def test_video_stop_of_a_previous_move_is_ignored(self) -> None:
        self.assertTrue(ptz_moving_at_frame_time(1001.5, MOVE_START, MOVE_STOP, 999.0))

    def test_without_video_stop_the_reported_stop_ends_the_move(self) -> None:
        self.assertTrue(ptz_moving_at_frame_time(1000.5, MOVE_START, MOVE_STOP))
        self.assertFalse(ptz_moving_at_frame_time(1001.5, MOVE_START, MOVE_STOP))

    def test_frame_captured_after_move(self) -> None:
        self.assertFalse(
            frame_captured_after_ptz_move(1001.5, MOVE_START, MOVE_STOP, 0.0)
        )
        self.assertFalse(
            frame_captured_after_ptz_move(1001.3, MOVE_START, MOVE_STOP, 1001.4)
        )
        self.assertTrue(
            frame_captured_after_ptz_move(1001.5, MOVE_START, MOVE_STOP, 1001.4)
        )

    def test_video_stop_of_a_previous_move_is_not_after_this_one(self) -> None:
        self.assertFalse(
            frame_captured_after_ptz_move(1005.0, MOVE_START, MOVE_STOP, 999.0)
        )

    def test_no_frame_is_after_a_move_in_progress(self) -> None:
        self.assertFalse(frame_captured_after_ptz_move(1005.0, MOVE_START, 0.0, 0.0))

    def test_any_frame_is_after_no_move(self) -> None:
        self.assertTrue(frame_captured_after_ptz_move(1.0, 0.0, 0.0, 0.0))


class TestStaleFramesDoNotMove(unittest.IsolatedAsyncioTestCase):
    """A frame received before the video settled shows the object where it was
    before the last move corrected for it. Moving again from that frame applies
    the same correction twice and swings the camera past the object."""

    def _tracker(self, video_stop: float) -> PtzAutoTracker:
        tracker = _make_tracker()
        metrics = tracker.ptz_metrics[CAMERA]
        metrics.start_time.value = MOVE_START
        metrics.stop_time.value = MOVE_STOP
        metrics.video_stop_time.value = video_stop
        return tracker

    def test_enqueue_rejects_frame_received_before_the_video_settled(self) -> None:
        tracker = self._tracker(video_stop=1001.8)
        tracker.move_queues = {CAMERA: MagicMock()}
        tracker.move_queue_locks = {CAMERA: MagicMock()}
        tracker.move_queue_locks[CAMERA].locked.return_value = False

        tracker._enqueue_move(CAMERA, 1001.5, 0.3, 0, 0)
        tracker.onvif.loop.call_soon_threadsafe.assert_not_called()

        tracker._enqueue_move(CAMERA, 1002.0, 0.3, 0, 0)
        tracker.onvif.loop.call_soon_threadsafe.assert_called_once()

    async def _process_queued_move(self, tracker: PtzAutoTracker, frame_time: float):
        tracker.stop_event = threading.Event()
        tracker.move_queues = {CAMERA: asyncio.Queue()}
        tracker.move_queue_locks = {CAMERA: asyncio.Lock()}
        tracker.onvif._move_relative = AsyncMock()
        tracker.move_queues[CAMERA].put_nowait((frame_time, 0.3, 0.0, 0.0))

        task = asyncio.create_task(tracker._process_move_queue(CAMERA))
        await asyncio.sleep(0.05)
        tracker.stop_event.set()
        await task

        return tracker.onvif._move_relative

    async def test_queue_drops_move_from_frame_received_before_video_settled(
        self,
    ) -> None:
        tracker = self._tracker(video_stop=1001.8)

        move = await self._process_queued_move(tracker, 1001.5)

        move.assert_not_awaited()

    async def test_queue_drops_move_from_frame_before_last_move(self) -> None:
        # queued before the last move started, so it is stale even with no latency
        tracker = self._tracker(video_stop=1001.0)

        move = await self._process_queued_move(tracker, 999.5)

        move.assert_not_awaited()


class TestPtzSettleObserver(unittest.TestCase):
    """The camera process watches the video after each move to find the last
    frame that still shows it. Until then, frames show a stale scene.

    Frames are given as how far the camera has panned in pixels, the way the
    motion estimator sees it. The test frame is 1920 wide, so a quarter FOV pan
    is expected to move the frame by 240 pixels.
    """

    def _observer(self, stream_latency: float | None = None) -> PtzSettleObserver:
        config = _config(True, stream_latency).cameras[CAMERA]
        self.metrics = PTZMetrics()
        return PtzSettleObserver(config, self.metrics)

    def _move(self, start: float, pan: float = 0.25) -> None:
        # what OnvifController does when it sends a move
        self.metrics.start_time.value = start
        self.metrics.stop_time.value = 0.0
        self.metrics.video_stop_time.value = 0.0
        self.metrics.move_pan.value = pan
        self.metrics.move_tilt.value = 0.0

    def _feed(self, observer: PtzSettleObserver, frames) -> None:
        for frame_time, panned in frames:
            observer.update(
                frame_time,
                None
                if panned is None
                else TranslationTransformation(np.array([-panned, 0.0])),
            )

    def test_settles_on_the_first_quiet_frame_after_the_move(self) -> None:
        observer = self._observer()
        self._move(1000.0)
        self._feed(observer, [(1000.2, 0), (1000.4, 60), (1000.6, 120)])
        self.metrics.stop_time.value = 1000.9
        self._feed(observer, [(1000.8, 180), (1001.0, 240)])
        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

        self._feed(observer, [(1001.2, 241)])

        self.assertEqual(self.metrics.video_stop_time.value, 1001.0)
        self.assertAlmostEqual(observer.measured_latency, 0.1)

    def test_waits_for_motion_that_has_not_reached_the_video_yet(self) -> None:
        # a short move that ended before the frames showing it arrived
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.8, 0), (1001.0, 0), (1001.2, 0)])
        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

        self._feed(observer, [(1001.4, 120), (1001.6, 240), (1001.8, 240)])

        self.assertEqual(self.metrics.video_stop_time.value, 1001.6)
        self.assertAlmostEqual(observer.measured_latency, 1.0)

    def test_a_slow_pan_is_still_moving(self) -> None:
        # a slow motor moves the frame a little each frame, but keeps moving it
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        frames = [(1000.8 + 0.1 * i, 8.0 * i) for i in range(31)]
        self._feed(observer, frames)
        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

        self._feed(observer, [(1004.0, 240), (1004.1, 240)])

        self.assertAlmostEqual(self.metrics.video_stop_time.value, 1003.8)

    def test_jitter_before_the_move_arrives_is_not_the_move(self) -> None:
        # estimator noise or a person walking can shift a frame a little
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.8, 0), (1001.0, 30), (1001.2, 30)])
        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

        self._feed(observer, [(1001.4, 150), (1001.6, 270), (1001.8, 270)])

        self.assertEqual(self.metrics.video_stop_time.value, 1001.6)

    def test_jitter_is_not_a_tiny_move(self) -> None:
        # a share of a tiny move is no more than the jitter of a still camera
        observer = self._observer()
        self._move(1000.0, pan=0.01)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.8, 0), (1001.0, 25), (1001.2, 25)])

        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

    def test_only_moves_of_known_size_measure_the_latency(self) -> None:
        # a preset return settles on any motion, which jitter can fake
        observer = self._observer()
        self._move(1000.0, pan=0.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.8, 0), (1001.0, 240), (1001.2, 240)])

        self.assertEqual(self.metrics.video_stop_time.value, 1001.0)
        self.assertIsNone(observer.measured_latency)

    def test_unknown_motion_does_not_settle(self) -> None:
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.8, 0), (1001.0, 240), (1001.2, None)])

        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

    def test_a_move_of_unknown_size_settles_once_the_frame_clearly_moved(
        self,
    ) -> None:
        # a zoom only move or a preset return has no distance to compare against
        observer = self._observer()
        self._move(1000.0, pan=0.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.8, 0), (1001.0, 8), (1001.2, 8)])
        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

        self._feed(observer, [(1001.4, 60), (1001.6, 60)])

        self.assertEqual(self.metrics.video_stop_time.value, 1001.4)

    def test_times_out_without_seeing_the_move(self) -> None:
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        frame_time = 1000.8
        while self.metrics.video_stop_time.value == 0.0 and frame_time < 1010:
            self._feed(observer, [(frame_time, 0)])
            frame_time += 0.2

        self.assertGreater(self.metrics.video_stop_time.value, 1000.6 + 2)
        self.assertIsNone(observer.measured_latency)

    def test_a_longer_latency_than_measured_so_far_is_still_measured(self) -> None:
        # the stream can get slower, so a short measurement must not cut off a
        # later, longer one
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.7, 0), (1000.8, 240), (1001.0, 240)])
        self.assertAlmostEqual(observer.measured_latency, 0.2)

        self._move(1002.0)
        self.metrics.stop_time.value = 1002.6
        self._feed(observer, [(1002.8 + 0.2 * i, 240) for i in range(10)])
        self._feed(observer, [(1004.8, 480), (1005.0, 480)])

        self.assertEqual(self.metrics.video_stop_time.value, 1004.8)

    def test_a_move_the_video_hardly_shows_waits_for_the_measured_latency(
        self,
    ) -> None:
        # a camera that pans less than asked, or a tiny move, would otherwise
        # pause tracking for the whole timeout
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.7, 0), (1000.8, 240), (1001.0, 240)])

        self._move(1002.0)
        self.metrics.stop_time.value = 1002.6
        frame_time = 1002.8
        while self.metrics.video_stop_time.value == 0.0 and frame_time < 1010:
            self._feed(observer, [(frame_time, 240 + (frame_time - 1002.8) * 10)])
            frame_time += 0.2

        self.assertLess(self.metrics.video_stop_time.value, 1002.6 + 1)

    def test_configured_latency_is_used_instead_of_watching(self) -> None:
        observer = self._observer(stream_latency=0.8)
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6

        self._feed(observer, [(1000.8, 0)])

        self.assertAlmostEqual(self.metrics.video_stop_time.value, 1001.4)
        self.assertIsNone(observer.measured_latency)

    def test_a_new_move_needs_its_own_motion(self) -> None:
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.7, 0), (1000.8, 240), (1001.0, 240)])
        self.assertEqual(self.metrics.video_stop_time.value, 1000.8)

        self._move(1002.0)
        self.metrics.stop_time.value = 1002.6
        self._feed(observer, [(1002.8, 240), (1003.0, 240)])

        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

    def test_the_measured_latency_is_not_used_before_anything_arrives(
        self,
    ) -> None:
        # if the stream got slower, the move is still on its way
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.7, 0), (1000.8, 240), (1001.0, 240)])

        self._move(1002.0)
        self.metrics.stop_time.value = 1002.6
        self._feed(observer, [(1002.8 + 0.2 * i, 240) for i in range(8)])

        self.assertEqual(self.metrics.video_stop_time.value, 0.0)

    def test_a_motion_estimator_reset_is_not_camera_motion(self) -> None:
        # after a reset the estimator measures from a new reference frame
        observer = self._observer()
        self._move(1000.0)
        self.metrics.stop_time.value = 1000.6
        self._feed(observer, [(1000.8, 0), (1001.0, 240)])

        observer.reset_reference()
        self._feed(observer, [(1001.2, 0), (1001.4, 0)])

        self.assertEqual(self.metrics.video_stop_time.value, 1001.0)

    def test_publishes_the_median_latency(self) -> None:
        observer = self._observer()
        panned = 0
        for start, lag in ((1000.0, 0.4), (1010.0, 0.6), (1020.0, 2.0)):
            self._move(start)
            stop = start + 0.5
            self.metrics.stop_time.value = stop
            self._feed(
                observer,
                [
                    (stop, panned),
                    (stop + lag, panned + 240),
                    (stop + lag + 0.2, panned + 240),
                ],
            )
            panned += 240

        self.assertAlmostEqual(observer.measured_latency, 0.6)
        self.assertAlmostEqual(self.metrics.stream_latency.value, 0.6)


class TestMotionEstimatorKeepsWatching(unittest.TestCase):
    def test_estimates_until_the_move_is_measured(self) -> None:
        # a move that settled on the measured latency can still show up in the
        # video later, and that is what keeps the measurement up to date
        metrics = PTZMetrics()
        estimator = PtzMotionEstimator(_config(True).cameras[CAMERA], metrics)
        estimator._estimate = MagicMock()
        metrics.start_time.value = 1000.0
        metrics.stop_time.value = 1000.5
        metrics.video_stop_time.value = 1001.0

        estimator.motion_estimator([], "frame", 1001.5, CAMERA)

        estimator._estimate.assert_called_once()

    def test_no_estimates_without_a_move(self) -> None:
        metrics = PTZMetrics()
        estimator = PtzMotionEstimator(_config(True).cameras[CAMERA], metrics)
        estimator._estimate = MagicMock()

        estimator.motion_estimator([], "frame", 1001.5, CAMERA)

        estimator._estimate.assert_not_called()


class TestWaitUntilVideoSettled(unittest.IsolatedAsyncioTestCase):
    async def test_no_wait_without_video(self) -> None:
        # calibration on startup runs before the camera processes deliver frames
        tracker = _make_tracker()
        metrics = tracker.ptz_metrics[CAMERA]
        metrics.start_time.value = 1000.0

        with (
            patch("frigate.ptz.autotrack.time.time", return_value=1000.5),
            patch("frigate.ptz.autotrack.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            await tracker._wait_until_video_settled(CAMERA)

        sleep.assert_not_awaited()

    async def test_waits_while_frames_arrive(self) -> None:
        tracker = _make_tracker()
        metrics = tracker.ptz_metrics[CAMERA]
        metrics.start_time.value = 1000.0
        metrics.frame_time.value = 1000.4
        clock = iter([1000.5] + [1000.5 + i for i in range(20)])

        with (
            patch("frigate.ptz.autotrack.time.time", side_effect=lambda: next(clock)),
            patch("frigate.ptz.autotrack.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            await tracker._wait_until_video_settled(CAMERA)

        sleep.assert_awaited()


class TestReturnToPreset(unittest.IsolatedAsyncioTestCase):
    async def test_return_is_timed_like_a_move(self) -> None:
        # the video keeps showing the return after the camera reports it done,
        # so tracking must not restart from those frames
        tracker = _make_tracker()
        metrics = tracker.ptz_metrics[CAMERA]
        metrics.video_stop_time.value = 990.0
        metrics.move_pan.value = 0.3
        tracker.onvif._move_to_preset = AsyncMock()

        async def report_idle(camera):
            metrics.stop_time.value = 1001.0
            metrics.motor_stopped.set()

        tracker.onvif.get_camera_status = AsyncMock(side_effect=report_idle)

        with patch("frigate.ptz.autotrack.time.time", return_value=1000.0):
            await tracker._return_to_preset(CAMERA)

        tracker.onvif._move_to_preset.assert_awaited_once_with(CAMERA, "home")
        self.assertEqual(metrics.start_time.value, 1000.0)
        self.assertEqual(metrics.stop_time.value, 1001.0)
        self.assertEqual(metrics.video_stop_time.value, 0.0)
        # how far a preset is from here is unknown
        self.assertEqual(metrics.move_pan.value, 0.0)

    async def test_return_gives_up_on_a_camera_that_never_reports_idle(self) -> None:
        # waiting forever would stall the autotracker thread for every camera
        tracker = _make_tracker()
        metrics = tracker.ptz_metrics[CAMERA]
        tracker.onvif._move_to_preset = AsyncMock()
        tracker.onvif.get_camera_status = AsyncMock()
        clock = iter(range(1000, 2000))

        with patch(
            "frigate.ptz.autotrack.time.time", side_effect=lambda: float(next(clock))
        ):
            await tracker._return_to_preset(CAMERA)

        self.assertTrue(metrics.motor_stopped.is_set())
        self.assertGreater(metrics.stop_time.value, metrics.start_time.value)
        self.assertLess(tracker.onvif.get_camera_status.await_count, 30)


class TestFrameShift(unittest.TestCase):
    def test_zoom_moves_the_corners_but_not_the_center(self) -> None:
        # measuring at the center would miss a pure zoom
        width, height = 640, 360
        before = HomographyTransformation(np.eye(3))
        zoomed = HomographyTransformation(
            np.array([[1.2, 0, -0.1 * width], [0, 1.2, -0.1 * height], [0, 0, 1]])
        )

        shift = frame_shift(before, zoomed, width, height)

        self.assertGreater(shift, 30)

    def test_static_camera_has_no_shift(self) -> None:
        move = TranslationTransformation(np.array([-120.0, 0.0]))

        self.assertAlmostEqual(frame_shift(move, move, 640, 360), 0.0)

    def test_pan_shift(self) -> None:
        before = TranslationTransformation(np.array([-120.0, 0.0]))
        after = TranslationTransformation(np.array([-160.0, 0.0]))

        self.assertAlmostEqual(frame_shift(before, after, 640, 360), 40.0)


class TestAutotrackerDisable(unittest.TestCase):
    def test_disable_publishes_to_camera_process(self) -> None:
        tracker = _make_tracker(autotracking_enabled=True)

        tracker._disable(CAMERA, "onvif connection failed")

        autotracking = tracker.config.cameras[CAMERA].onvif.autotracking
        self.assertFalse(autotracking.enabled)

        publish = tracker.dispatcher.config_updater.publish_update
        publish.assert_called_once()
        topic, payload = publish.call_args.args
        self.assertEqual(topic.update_type, CameraConfigUpdateEnum.autotracking)
        self.assertEqual(topic.camera, CAMERA)
        self.assertIs(payload, autotracking)


class TestMaxTargetBox(unittest.TestCase):
    def test_follows_zoom_factor(self) -> None:
        self.assertAlmostEqual(calculate_max_target_box(0.5), 0.6**2)
        self.assertAlmostEqual(calculate_max_target_box(0.25), 0.6**4)


if __name__ == "__main__":
    unittest.main()
