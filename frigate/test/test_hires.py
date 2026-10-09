"""Tests for replacing saved detect stream images with main stream frames."""

import os
import tempfile
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
from playhouse.sqlite_ext import SqliteExtDatabase
from pydantic import ValidationError

from frigate.config import FrigateConfig
from frigate.data_processing.common import hires
from frigate.data_processing.common.face.detector import DetectionResult
from frigate.data_processing.common.hires import (
    HiResUpgrader,
    Reference,
    clip_box,
    expand_box,
    find_recording,
    find_recordings,
    find_synced_frame,
    find_synced_offset,
    go2rtc_stream_name,
    match_score,
    reference_from_bgr,
    reference_from_yuv,
    scale_box,
)
from frigate.data_processing.common.license_plate.mixin import (
    MAX_EXPIRED_PLATES,
    LicensePlateProcessingMixin,
)
from frigate.data_processing.real_time.face import FaceRealTimeProcessor
from frigate.models import Recordings


def build_config(
    stream: str | None = "main",
    record: bool = True,
    go2rtc: bool = False,
    **image_source: float,
) -> FrigateConfig:
    camera: dict = {
        "ffmpeg": {
            "inputs": [
                {"path": "rtsp://10.0.0.1:554/main", "roles": ["record"]},
                {"path": "rtsp://10.0.0.1:554/sub", "roles": ["detect"]},
            ]
        },
        "detect": {"height": 480, "width": 640, "fps": 5},
        "record": {"enabled": record},
    }
    config: dict = {"mqtt": {"host": "mqtt"}, "cameras": {"back": camera}}

    if stream is not None:
        camera["image_source"] = {"stream": stream, **image_source}

    if go2rtc:
        config["go2rtc"] = {"streams": {"back_main": "rtsp://10.0.0.1:554/main"}}
        camera["live"] = {"streams": {"Main": "back_main"}}

    return FrigateConfig(**config)


class TestBoxes(unittest.TestCase):
    def test_scale_box(self):
        assert scale_box((10, 20, 30, 40), 4.0, 3.0) == (40, 60, 120, 120)

    def test_expand_box_pads_each_side(self):
        # a 100x50 box grown by 0.5 gains 25 and 12 pixels on each side
        assert expand_box((100, 100, 200, 150), (1080, 1920, 3), 0.5) == (
            75,
            88,
            225,
            162,
        )

    def test_expand_box_is_clipped_to_frame(self):
        assert expand_box((0, 0, 100, 100), (120, 110, 3), 1.0) == (0, 0, 110, 120)

    def test_clip_box(self):
        assert clip_box((-5, -5, 700, 500), (480, 640, 3)) == (0, 0, 640, 480)


class TestSync(unittest.TestCase):
    FPS = 5

    def _scene(self, position: int) -> np.ndarray:
        """A textured background with a bright object at a horizontal position."""
        frame = np.tile(np.arange(64, dtype=np.uint8) * 2, (48, 1))
        frame[::7] += 40
        frame[14:34, position : position + 10] = 255
        frame[20:26, position + 2 : position + 8] = 0
        return frame

    def _segment(self) -> np.ndarray:
        # the object crosses the frame over 10 seconds, 1 pixel per frame
        return np.stack([self._scene(position) for position in range(50)])

    def _reference(self, position: int) -> Reference:
        box = (position - 2, 10, position + 12, 38)
        return Reference(
            self._scene(position)[10:38, position - 2 : position + 12], box
        )

    def test_score_is_high_only_for_the_same_moment(self):
        reference = self._reference(20)

        assert match_score(self._scene(20), reference) > 0.99
        assert match_score(self._scene(40), reference) < 0.9

    def test_featureless_region_never_matches(self):
        reference = Reference(np.full((10, 10), 7, np.uint8), (0, 0, 10, 10))
        assert match_score(np.full((48, 64), 7, np.uint8), reference) == -1.0

    def test_small_shift_still_matches_but_scores_lower(self):
        reference = self._reference(20)
        exact = match_score(self._scene(20), reference)
        shifted = match_score(self._scene(22), reference)

        assert 0.9 < shifted < exact
        # further than the tolerated shift
        assert match_score(self._scene(26), reference) < 0.9

    def _find(
        self,
        frames: np.ndarray,
        offset: float,
        reference: Reference,
        threshold: float = 0.9,
        before: float = 4.0,
        after: float = 4.0,
    ) -> tuple[float, float] | None:
        return find_synced_offset(
            frames, self.FPS, offset, reference, threshold, before, after
        )

    def test_offset_follows_the_matching_frame(self):
        # the detect stream saw frame 20 (4.0s) but stamped it 1.4s late
        match = self._find(self._segment(), 5.4, self._reference(20))

        assert match is not None
        assert match[0] == 4.0
        assert match[1] > 0.99

    def test_no_offset_outside_the_window(self):
        # the matching frame is 6 seconds from where it was expected
        assert self._find(self._segment(), 9.0, self._reference(15)) is None

    def test_window_sides_are_independent(self):
        # the matching frame is 1.4 seconds before the detect frame time
        segment, reference = self._segment(), self._reference(20)

        # strict enough that the frames beside the matching one do not pass
        def find(offset: float, before: float, after: float) -> object:
            return self._find(segment, offset, reference, 0.995, before, after)

        assert find(5.4, before=1.0, after=4.0) is None
        assert find(5.4, before=2.0, after=0.0) is not None

        # and 1.4 seconds after it
        assert find(2.6, before=4.0, after=1.0) is None
        assert find(2.6, before=0.0, after=2.0) is not None

    def test_threshold_decides_what_counts_as_a_match(self):
        # every frame shows the object 3 pixels off from the reference
        frames = np.stack([self._scene(23) for _ in range(50)])
        reference = self._reference(20)

        assert self._find(frames, 4.0, reference, threshold=0.995) is None

        match = self._find(frames, 4.0, reference, threshold=0.9)
        # all frames tie, so the one at the expected time is used
        assert match is not None and match[0] == 4.0

    def test_still_subject_uses_the_expected_time(self):
        # the frames only differ by noise, so none of them is a better match
        rng = np.random.default_rng(0)
        frames = np.stack(
            [
                np.clip(
                    self._scene(20).astype(np.int16) + rng.integers(-2, 3, (48, 64)),
                    0,
                    255,
                ).astype(np.uint8)
                for _ in range(50)
            ]
        )
        match = self._find(frames, 4.0, self._reference(20))

        assert match is not None and match[0] == 4.0

    def test_no_offset_without_a_match(self):
        reference = self._reference(20)
        empty = np.stack([self._scene(60) for _ in range(50)])

        assert self._find(empty, 4.0, reference) is None

    def test_search_reaches_into_the_next_segment(self):
        # stamped 1009.0 by the detect stream, shown at 1011.0 by the recording
        empty = np.stack([self._scene(60) for _ in range(50)])
        segments = {"/tmp/a.mp4": empty, "/tmp/b.mp4": self._segment()}
        config = build_config()

        with (
            patch.object(
                hires,
                "find_recordings",
                return_value=[("/tmp/a.mp4", 1000.0), ("/tmp/b.mp4", 1010.0)],
            ) as finder,
            patch.object(
                hires,
                "decode_segment",
                side_effect=lambda ffmpeg, path, width, height, fps: segments[path],
            ),
        ):
            found = find_synced_frame(config, "back", 1009.0, self._reference(5))

        finder.assert_called_once_with("back", 1005.0, 1013.0)
        assert found == ("/tmp/b.mp4", 1.0)

    def test_reference_from_yuv_uses_the_luma_plane(self):
        yuv = np.zeros((72, 64), np.uint8)
        yuv[:48] = self._scene(20)
        reference = reference_from_yuv(yuv, [18, 10, 32, 38], 48)

        assert reference.box == (18, 10, 32, 38)
        assert match_score(self._scene(20), reference) > 0.99

    def test_reference_from_bgr(self):
        bgr = cv2.cvtColor(self._scene(20), cv2.COLOR_GRAY2BGR)
        reference = reference_from_bgr(bgr, (18, 10, 32, 38))

        assert match_score(self._scene(20), reference) > 0.99

    def test_empty_box_has_no_reference(self):
        assert (
            reference_from_yuv(np.zeros((72, 64), np.uint8), [70, 0, 90, 10], 48)
            is None
        )


class TestConfig(unittest.TestCase):
    def test_detect_is_the_default(self):
        config = build_config(stream=None)
        assert config.cameras["back"].image_source.stream == "detect"
        assert not HiResUpgrader(config, start_worker=False).enabled("back")

    def test_global_value_is_inherited(self):
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "image_source": {"stream": "auto"},
                "cameras": {
                    "back": {
                        "ffmpeg": {
                            "inputs": [
                                {"path": "rtsp://10.0.0.1:554/v", "roles": ["detect"]}
                            ]
                        },
                        "detect": {"height": 480, "width": 640, "fps": 5},
                    }
                },
            }
        )
        assert config.cameras["back"].image_source.stream == "auto"

    def test_search_defaults(self):
        image_source = build_config().cameras["back"].image_source

        assert image_source.match_threshold == 0.9
        assert image_source.search_before == 4.0
        assert image_source.search_after == 4.0

    def test_search_settings_are_validated(self):
        image_source = (
            build_config(match_threshold=0.8, search_before=1.5, search_after=10)
            .cameras["back"]
            .image_source
        )
        assert image_source.match_threshold == 0.8
        assert image_source.search_before == 1.5
        assert image_source.search_after == 10.0

        for invalid in (
            {"match_threshold": 1.5},
            {"search_before": -1},
            {"search_after": 60},
        ):
            with self.assertRaises(ValidationError):
                build_config(**invalid)

    def test_go2rtc_stream_name(self):
        assert go2rtc_stream_name(build_config(go2rtc=True), "back") == "back_main"
        assert go2rtc_stream_name(build_config(), "back") is None


class TestFindRecording(unittest.TestCase):
    def setUp(self):
        self.db = SqliteExtDatabase(":memory:")
        self.db.bind([Recordings])
        self.db.create_tables([Recordings])

    def tearDown(self):
        self.db.close()

    def _insert(self, id: str, start: float, end: float, stream_type: str) -> None:
        Recordings.create(
            id=id,
            camera="back",
            path=f"/tmp/{id}.mp4",
            start_time=start,
            end_time=end,
            duration=end - start,
            stream_type=stream_type,
        )

    def test_only_the_main_stream_is_used(self):
        self._insert("sub", 1000.0, 1010.0, "sub")
        assert find_recording("back", 1004.0) is None

        self._insert("main", 1001.0, 1011.0, "main")
        assert find_recording("back", 1004.0) == ("/tmp/main.mp4", 3.0)

    def test_missing_segment(self):
        self._insert("main", 1000.0, 1010.0, "main")
        assert find_recording("back", 1020.0) is None

    def test_segments_overlapping_a_range(self):
        self._insert("a", 1000.0, 1010.0, "main")
        self._insert("b", 1010.0, 1020.0, "main")
        self._insert("c", 1020.0, 1030.0, "main")
        self._insert("sub", 1010.0, 1020.0, "sub")

        assert find_recordings("back", 1008.0, 1012.0) == [
            ("/tmp/a.mp4", 1000.0),
            ("/tmp/b.mp4", 1010.0),
        ]
        assert find_recordings("back", 1012.0, 1015.0) == [("/tmp/b.mp4", 1010.0)]
        assert find_recordings("back", 1040.0, 1050.0) == []


class TestHiResUpgrader(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((1080, 1920, 3), np.uint8)
        self.calls: list[tuple[tuple[int, ...], float, float]] = []
        self.recording: tuple[str, float] | None = ("/tmp/main.mp4", 3.0)
        self.fetched: list[tuple] = []

    def _upgrader(self, config: FrigateConfig) -> HiResUpgrader:
        return HiResUpgrader(
            config,
            recording_fetcher=self._fetch_recording,
            live_fetcher=lambda config, camera, stream_name, reference: self.frame,
            recording_finder=lambda camera, frame_time: self.recording,
            start_worker=False,
        )

    def _fetch_recording(self, config, camera, frame_time, reference):
        self.fetched.append((camera, frame_time, reference))
        return self.frame

    def _handler(self, frame: np.ndarray, scale_x: float, scale_y: float) -> None:
        self.calls.append((frame.shape, scale_x, scale_y))

    def _fetch(self, upgrader: HiResUpgrader) -> None:
        """Run the fetches the worker thread would have run."""
        while not upgrader.fetch_queue.empty():
            job, fetch = upgrader.fetch_queue.get_nowait()
            upgrader.completed.append((job, fetch()))

    def test_detect_cameras_are_ignored(self):
        upgrader = self._upgrader(build_config(stream="detect"))
        assert not upgrader.submit("back", 1004.0, "obj", self._handler)
        assert not upgrader.pending

    def test_job_waits_for_the_recording_to_be_saved(self):
        upgrader = self._upgrader(build_config())
        assert upgrader.submit("back", 1004.0, "obj", self._handler)

        upgrader.process()
        assert upgrader.fetch_queue.empty()

        # the segment holding the frame is still in the cache
        upgrader.update_recordings_available("back", 1000.0)
        upgrader.process()
        assert upgrader.fetch_queue.empty()

        upgrader.update_recordings_available("back", 1010.0)
        upgrader.process()
        self._fetch(upgrader)
        upgrader.process()

        assert self.calls == [((1080, 1920, 3), 3.0, 2.25)]
        assert not upgrader.pending
        assert not upgrader.job_counts

    def test_reference_reaches_the_fetcher(self):
        reference = Reference(np.zeros((10, 10), np.uint8), (0, 0, 10, 10))
        upgrader = self._upgrader(build_config())
        upgrader.submit("back", 1004.0, "obj", self._handler, reference)
        upgrader.update_recordings_available("back", 1010.0)
        upgrader.process()
        self._fetch(upgrader)

        assert self.fetched == [("back", 1004.0, reference)]

    def test_job_with_reference_waits_for_the_search_window(self):
        reference = Reference(np.zeros((10, 10), np.uint8), (0, 0, 10, 10))
        upgrader = self._upgrader(build_config(search_after=5))
        upgrader.submit("back", 1004.0, "obj", self._handler, reference)
        upgrader.submit("back", 1004.0, "other", self._handler)

        # the frame is saved but the 5 seconds after it are not
        upgrader.update_recordings_available("back", 1006.0)
        upgrader.process()
        self._fetch(upgrader)
        assert self.fetched == [("back", 1004.0, None)]

        upgrader.update_recordings_available("back", 1010.0)
        upgrader.process()
        self._fetch(upgrader)
        assert self.fetched[1:] == [("back", 1004.0, reference)]

    def test_job_is_retried_until_the_row_is_written(self):
        upgrader = self._upgrader(build_config())
        upgrader.submit("back", 1004.0, "obj", self._handler)
        upgrader.update_recordings_available("back", 1010.0)

        self.recording = None
        upgrader.process()
        assert upgrader.fetch_queue.empty()
        assert len(upgrader.pending) == 1

        self.recording = ("/tmp/main.mp4", 3.0)
        upgrader.pending[0].next_try = 0.0
        upgrader.process()
        assert not upgrader.fetch_queue.empty()

    def test_job_times_out(self):
        upgrader = self._upgrader(build_config())
        upgrader.submit("back", 1004.0, "obj", self._handler)

        with patch.object(
            hires.time, "monotonic", return_value=hires.time.monotonic() + 1000
        ):
            upgrader.process()

        assert not upgrader.pending
        assert not upgrader.job_counts
        assert not self.calls

    def test_jobs_per_object_are_limited(self):
        upgrader = self._upgrader(build_config())
        accepted = [
            upgrader.submit("back", 1000.0 + i, "obj", self._handler)
            for i in range(hires.MAX_JOBS_PER_KEY + 2)
        ]

        assert accepted.count(True) == hires.MAX_JOBS_PER_KEY
        assert upgrader.submit("back", 1000.0, "other", self._handler)

    def test_failed_fetch_keeps_the_detect_image(self):
        upgrader = self._upgrader(build_config())
        upgrader.recording_fetcher = lambda config, camera, frame_time, reference: None
        upgrader.submit("back", 1004.0, "obj", self._handler)
        upgrader.update_recordings_available("back", 1010.0)
        upgrader.process()
        self._fetch(upgrader)
        upgrader.process()

        assert not self.calls
        assert not upgrader.job_counts

    def test_auto_skips_frames_that_are_not_larger(self):
        self.frame = np.zeros((480, 640, 3), np.uint8)

        for stream, expected in (("auto", 0), ("main", 1)):
            self.calls = []
            upgrader = self._upgrader(build_config(stream=stream))
            upgrader.submit("back", 1004.0, "obj", self._handler)
            upgrader.update_recordings_available("back", 1010.0)
            upgrader.process()
            self._fetch(upgrader)
            upgrader.process()

            assert len(self.calls) == expected

    def test_go2rtc_is_used_when_not_recording(self):
        upgrader = self._upgrader(build_config(record=False, go2rtc=True))
        assert upgrader.submit("back", 1004.0, "obj", self._handler)

        # nothing waits on a recording, the current frame is fetched right away
        assert not upgrader.pending
        self._fetch(upgrader)
        upgrader.process()

        assert len(self.calls) == 1

    def test_no_source_without_recording_or_go2rtc(self):
        upgrader = self._upgrader(build_config(record=False))
        assert not upgrader.submit("back", 1004.0, "obj", self._handler)
        assert not upgrader.job_counts


class TestFaceAttemptUpgrade(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.file = os.path.join(self.dir.name, "attempt.webp")
        cv2.imwrite(self.file, np.zeros((25, 20, 3), np.uint8))

        self.processor = FaceRealTimeProcessor.__new__(FaceRealTimeProcessor)
        self.processor.face_config = SimpleNamespace(detection_threshold=0.7)
        self.processor.face_detector = MagicMock()
        self.frame = np.full((1080, 1920, 3), 127, np.uint8)

    def tearDown(self):
        self.dir.cleanup()

    def _upgrade(self) -> tuple[int, ...]:
        # a 20x25 face at (100, 100) in a 640x480 detect frame
        self.processor._FaceRealTimeProcessor__upgrade_face_attempt(
            self.file, (100, 100, 120, 125), self.frame, 3.0, 2.25
        )
        return cv2.imread(self.file).shape

    def test_face_is_replaced_from_the_search_area(self):
        self.processor.face_detector.detect.return_value = DetectionResult(
            face=(30, 20, 90, 85), landmarks=()
        )

        assert self._upgrade() == (65, 60, 3)

        # the scaled box is (300, 225, 360, 281), padded by half its size on each side
        search_area = self.processor.face_detector.detect.call_args.args[0]
        assert search_area.shape == (112, 120, 3)

    def test_detect_crop_is_kept_without_a_face(self):
        self.processor.face_detector.detect.return_value = None
        assert self._upgrade() == (25, 20, 3)

    def test_detect_crop_is_kept_for_a_face_of_another_size(self):
        self.processor.face_detector.detect.return_value = DetectionResult(
            face=(0, 0, 20, 22), landmarks=()
        )
        assert self._upgrade() == (25, 20, 3)

    def test_removed_attempt_is_not_recreated(self):
        os.remove(self.file)
        self.processor.face_detector.detect.return_value = DetectionResult(
            face=(30, 20, 90, 85), landmarks=()
        )
        self.processor._FaceRealTimeProcessor__upgrade_face_attempt(
            self.file, (100, 100, 120, 125), self.frame, 3.0, 2.25
        )

        assert not os.path.exists(self.file)


class TestPlateUpgrade(unittest.TestCase):
    def setUp(self):
        self.processor = LicensePlateProcessingMixin.__new__(
            LicensePlateProcessingMixin
        )
        self.processor.config = SimpleNamespace(
            cameras={
                "back": SimpleNamespace(
                    lpr=SimpleNamespace(enabled=True), detect=SimpleNamespace(fps=5)
                )
            }
        )
        self.processor.lpr_config = SimpleNamespace(
            recognition_threshold=0.9, known_plates={}, match_distance=1
        )
        self.processor.cluster_threshold = 0.85
        self.processor.detected_license_plates = {}
        self.processor.camera_current_cars = {}
        self.processor.expired_plates = OrderedDict()
        self.processor.sub_label_publisher = MagicMock()
        self.processor.requestor = MagicMock()
        self.processor._passes_plate_filters = MagicMock(return_value=True)
        self.frame = np.zeros((1080, 1920, 3), np.uint8)

    def _read(self, plate: str, confidence: float) -> None:
        self.processor._process_license_plate = MagicMock(
            return_value=([plate], [[confidence] * len(plate)], [5000])
        )
        self.processor.lpr_process_hires(
            "obj", "back", (100, 100, 160, 120), {"id": "obj"}, self.frame, 3.0, 2.25
        )

    def _published(self) -> list[str]:
        return [
            call.args[0][2]
            for call in self.processor.sub_label_publisher.publish.call_args_list
            if call.args[0][1] == "recognized_license_plate"
        ]

    def test_plate_crop_is_scaled_and_padded(self):
        self._read("ABC123", 0.95)

        # the scaled box is (300, 225, 480, 270), padded by 0.3 of its size
        crop = self.processor._process_license_plate.call_args.args[2]
        assert crop.shape == (57, 234, 3)

    def test_tracked_object_reading_joins_its_cluster(self):
        self._read("ABC123", 0.95)

        assert self._published() == ["ABC123"]
        assert self.processor.detected_license_plates["obj"]["plate"] == "ABC123"

    def test_low_confidence_reading_is_ignored(self):
        self._read("ABC123", 0.5)

        assert self._published() == []
        assert self.processor.detected_license_plates == {}

    def test_ended_object_takes_a_better_reading_without_being_tracked_again(self):
        self.processor.detected_license_plates["obj"] = {
            "plate": "A8C123",
            "char_confidences": [0.92] * 6,
        }
        self.processor.lpr_expire("obj", "back")

        self._read("ABC123", 0.91)
        assert self._published() == []

        self._read("ABC123", 0.97)
        assert self._published() == ["ABC123"]
        assert self.processor.detected_license_plates == {}

    def test_expired_plates_are_bounded(self):
        for i in range(MAX_EXPIRED_PLATES + 10):
            self.processor.lpr_expire(f"obj{i}", "back")

        assert len(self.processor.expired_plates) == MAX_EXPIRED_PLATES
        assert "obj0" not in self.processor.expired_plates


if __name__ == "__main__":
    unittest.main(verbosity=2)
