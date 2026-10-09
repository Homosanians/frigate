"""Tests for tracking when Frigate stops receiving frames from a camera."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.camera.video_loss import VideoLossTracker, VideoLossUpdate
from frigate.const import UPDATE_CAMERA_ACTIVITY
from frigate.track.object_processing import TrackedObjectProcessor

RETRY = 10.0


class TestVideoLossTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = VideoLossTracker()

    def update(
        self, now: float, last_frame: float, enabled: bool = True
    ) -> VideoLossUpdate | None:
        return self.tracker.update("front", now, last_frame, enabled, RETRY)

    def test_first_update_reports_a_healthy_camera(self) -> None:
        self.assertEqual(
            self.update(now=100.0, last_frame=99.5),
            VideoLossUpdate(lost=False, last_frame_time=99.5),
        )

    def test_healthy_camera_reports_nothing_further(self) -> None:
        self.update(now=100.0, last_frame=99.5)

        self.assertIsNone(self.update(now=101.0, last_frame=100.5))
        self.assertIsNone(self.update(now=110.0, last_frame=100.5))

    def test_frames_stopping_reports_loss_after_retry_interval(self) -> None:
        self.update(now=100.0, last_frame=99.5)

        self.assertIsNone(self.update(now=109.0, last_frame=99.5))
        self.assertEqual(
            self.update(now=110.0, last_frame=99.5),
            VideoLossUpdate(lost=True, last_frame_time=99.5),
        )
        self.assertIsNone(self.update(now=200.0, last_frame=99.5))

    def test_frames_resuming_clears_loss(self) -> None:
        self.update(now=100.0, last_frame=99.5)
        self.update(now=110.0, last_frame=99.5)

        self.assertEqual(
            self.update(now=120.0, last_frame=119.8),
            VideoLossUpdate(lost=False, last_frame_time=119.8),
        )

    def test_camera_without_a_frame_since_start_is_measured_from_start(
        self,
    ) -> None:
        self.assertEqual(
            self.update(now=100.0, last_frame=0.0),
            VideoLossUpdate(lost=False, last_frame_time=100.0),
        )
        self.assertIsNone(self.update(now=110.0, last_frame=0.0))
        self.assertEqual(
            self.update(now=111.0, last_frame=0.0),
            VideoLossUpdate(lost=True, last_frame_time=100.0),
        )

    def test_turning_a_camera_off_clears_loss_and_never_reports_it(self) -> None:
        self.update(now=100.0, last_frame=99.5)
        self.update(now=110.0, last_frame=99.5)

        self.assertEqual(
            self.update(now=111.0, last_frame=99.5, enabled=False),
            VideoLossUpdate(lost=False, last_frame_time=111.0),
        )
        self.assertIsNone(self.update(now=500.0, last_frame=99.5, enabled=False))

    def test_turning_a_camera_back_on_gives_it_a_full_interval(self) -> None:
        self.update(now=100.0, last_frame=99.5)
        self.update(now=200.0, last_frame=99.5, enabled=False)

        self.assertIsNone(self.update(now=201.0, last_frame=99.5))
        self.assertIsNone(self.update(now=210.0, last_frame=99.5))
        self.assertEqual(
            self.update(now=211.0, last_frame=99.5),
            VideoLossUpdate(lost=True, last_frame_time=200.0),
        )

    def test_cameras_are_tracked_separately(self) -> None:
        self.tracker.update("front", 100.0, 99.5, True, RETRY)
        self.tracker.update("back", 100.0, 99.5, True, RETRY)

        self.assertIsNotNone(self.tracker.update("front", 110.0, 99.5, True, RETRY))
        self.assertIsNone(self.tracker.update("back", 110.0, 109.5, True, RETRY))

    def test_removed_camera_starts_over(self) -> None:
        self.update(now=100.0, last_frame=99.5)
        self.tracker.remove("front")

        self.assertEqual(
            self.update(now=300.0, last_frame=0.0),
            VideoLossUpdate(lost=False, last_frame_time=300.0),
        )


class TestCheckVideoLoss(unittest.TestCase):
    """The processor publishes changes and keeps the activity snapshot in step."""

    def setUp(self) -> None:
        self.cameras = {
            name: SimpleNamespace(
                enabled=True,
                enabled_in_config=True,
                ffmpeg=SimpleNamespace(retry_interval=10),
            )
            for name in ("front", "_replay_front")
        }
        self.processor = SimpleNamespace(
            config=SimpleNamespace(cameras=self.cameras),
            camera_states={
                name: SimpleNamespace(current_frame_time=99.5) for name in self.cameras
            },
            camera_activity={"front": {"motion": False, "objects": []}},
            video_loss=VideoLossTracker(),
            dispatcher=MagicMock(),
            requestor=MagicMock(),
        )

    def check_at(self, now: float) -> None:
        with patch("frigate.track.object_processing.datetime") as mock_datetime:
            mock_datetime.datetime.now.return_value.timestamp.return_value = now
            TrackedObjectProcessor.check_video_loss(self.processor)

    def test_publishes_initial_state_without_touching_activity(self) -> None:
        self.check_at(100.0)

        self.processor.dispatcher.publish_local.assert_called_once_with(
            "front/video_loss", "OFF"
        )
        self.processor.requestor.send_data.assert_not_called()

    def test_loss_is_published_and_added_to_the_activity_snapshot(self) -> None:
        self.check_at(100.0)
        self.processor.dispatcher.reset_mock()

        self.check_at(110.0)

        self.processor.dispatcher.publish_local.assert_called_once_with(
            "front/video_loss", "ON"
        )
        self.assertEqual(
            self.processor.camera_activity["front"],
            {"motion": False, "objects": [], "video_loss_since": 99.5},
        )
        self.processor.requestor.send_data.assert_called_once_with(
            UPDATE_CAMERA_ACTIVITY, self.processor.camera_activity
        )

    def test_snapshot_updates_before_the_topic(self) -> None:
        # a snapshot built for a client that connects mid-change must not
        # arrive after the topic and contradict it
        order: list[str] = []
        self.processor.requestor.send_data.side_effect = lambda *_: order.append(
            "snapshot"
        )
        self.processor.dispatcher.publish_local.side_effect = lambda *_: order.append(
            "topic"
        )
        self.check_at(100.0)
        order.clear()

        self.check_at(110.0)

        self.assertEqual(order, ["snapshot", "topic"])

    def test_recovery_clears_the_activity_snapshot(self) -> None:
        self.check_at(100.0)
        self.check_at(110.0)
        self.processor.dispatcher.reset_mock()
        self.processor.requestor.reset_mock()
        self.processor.camera_states["front"].current_frame_time = 119.9

        self.check_at(120.0)

        self.processor.dispatcher.publish_local.assert_called_once_with(
            "front/video_loss", "OFF"
        )
        self.assertNotIn("video_loss_since", self.processor.camera_activity["front"])
        self.processor.requestor.send_data.assert_called_once()

    def test_replay_cameras_are_ignored(self) -> None:
        self.check_at(100.0)
        self.check_at(500.0)

        topics = [
            call.args[0]
            for call in self.processor.dispatcher.publish_local.call_args_list
        ]
        self.assertNotIn("_replay_front/video_loss", topics)


if __name__ == "__main__":
    unittest.main()
