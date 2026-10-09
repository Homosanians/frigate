"""Closed-loop simulator for PTZ autotracking.

Runs the real PtzAutoTracker and OnvifController against a simulated pan/tilt
camera on a virtual clock, so a full walk-through takes well under a second.

What is simulated:
- a person walking past the camera at a constant angular speed, then standing
- a PTZ motor with a command delay and a pan speed, plus an ONVIF MoveStatus
  that can be configured to report IDLE early, as some firmware does
- stream latency between capture and Frigate receiving the frame; Frigate
  stamps frames with the time they are received, so this is the delay that
  decides which frames count as "captured while moving"
- Frigate's camera motion compensation through the real PtzMotionEstimator,
  with the image-based estimate replaced by the true camera motion plus noise,
  dropouts and some of the person's motion leaking into it
- the real norfair tracker with Frigate's PTZ person settings

Run it inside the Frigate image with the repo mounted, for example:

  docker run --rm -e PYTHONPATH=/opt/frigate -v "$PWD:/opt/frigate" \\
    -w /opt/frigate --entrypoint python3 ghcr.io/blakeblackshear/frigate:<tag> \\
    testing-scripts/ptz_autotrack_sim.py --latency 0.8 --plot /opt/frigate/sim.png

frigate/version.py must exist (it is generated during the image build).

Useful options: --stream-latency to fix the latency instead of measuring it,
--fov-scale for a camera that moves more or less than asked, --idle-before-start
and --early-idle for firmware that reports a move finished too soon, --calibrate,
--sweep, and --debug for the autotracker logs.
"""

import argparse
import asyncio
import itertools
import json
import logging
import math
import random
import selectors
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
from norfair import Detection, Tracker
from norfair.camera_motion import TranslationTransformation
from norfair.filter import OptimizedKalmanFilterFactory

import frigate.ptz.autotrack as autotrack_module
import frigate.ptz.onvif as onvif_module
from frigate.camera import PTZMetrics
from frigate.config import FrigateConfig
from frigate.ptz.autotrack import (
    PtzAutoTracker,
    PtzMotionEstimator,
    ptz_moving_at_frame_time,
)
from frigate.ptz.onvif import OnvifController
from frigate.track.norfair_tracker import frigate_distance

CAMERA = "ptz"
CLOCK_START = 1000.0

logger = logging.getLogger("ptz_sim")


class VirtualClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


class VirtualTimeSelector(selectors.DefaultSelector):
    """Advance the virtual clock to the next timer instead of sleeping."""

    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self.clock = clock

    def select(self, timeout=None):
        if timeout is None:
            raise RuntimeError("simulation stalled: nothing is scheduled")

        if timeout > 0:
            self.clock.now += timeout

        return super().select(0)


@dataclass
class CameraModel:
    hfov: float = 90.0
    width: int = 1280
    height: int = 720
    # degrees per second for a RelativeMove at speed 1
    pan_speed: float = 60.0
    # time from the camera receiving a command to the motor starting
    start_delay: float = 0.3
    # time from the motor stopping to MoveStatus reporting IDLE
    settle: float = 0.2
    # physical move divided by the move the ONVIF FOV space asks for
    fov_scale: float = 1.0
    # MoveStatus reports IDLE until the motor actually starts
    idle_before_start: bool = False
    # MoveStatus reports IDLE this many seconds after the command, moving or not
    early_idle: float | None = None
    rtt: float = 0.05
    preset_pan: float = 0.0
    preset_speed: float = 120.0
    pan_min: float = -170.0
    pan_max: float = 170.0

    @property
    def focal_px(self) -> float:
        return (self.width / 2) / math.tan(math.radians(self.hfov / 2))


class SimPtz:
    """Pan-only PTZ motor with linear motion between commands."""

    def __init__(self, model: CameraModel, clock: VirtualClock) -> None:
        self.m = model
        self.clock = clock
        self.cmd_time = -math.inf
        self.t_start = -math.inf
        self.t_end = -math.inf
        self.from_pan = model.preset_pan
        self.to_pan = model.preset_pan
        self.moves: list[dict[str, Any]] = []

    def pan_at(self, t: float) -> float:
        if t <= self.t_start:
            return self.from_pan

        if t >= self.t_end:
            return self.to_pan

        frac = (t - self.t_start) / (self.t_end - self.t_start)
        return self.from_pan + (self.to_pan - self.from_pan) * frac

    def motor_moving(self, t: float) -> bool:
        return bool(self.t_start <= t < self.t_end and self.from_pan != self.to_pan)

    def _command(self, target: float, speed: float) -> None:
        now = self.clock.now
        current = self.pan_at(now)
        target = min(self.m.pan_max, max(self.m.pan_min, target))
        self.cmd_time = now
        self.from_pan = current
        self.to_pan = target
        self.t_start = now + self.m.start_delay
        self.t_end = self.t_start + abs(target - current) / speed

    def relative_move(self, x_fov: float) -> None:
        delta = float(x_fov) * (self.m.hfov / 2) * self.m.fov_scale
        self._command(self.pan_at(self.clock.now) + delta, self.m.pan_speed)

    def goto_preset(self) -> None:
        self._command(self.m.preset_pan, self.m.preset_speed)

    def move_status(self) -> str:
        now = self.clock.now

        if now >= self.t_end + self.m.settle:
            return "IDLE"

        if self.m.idle_before_start and now < self.t_start:
            return "IDLE"

        if self.m.early_idle is not None and now >= self.cmd_time + self.m.early_idle:
            return "IDLE"

        return "MOVING"


class FakePtzService:
    """The subset of the zeep ONVIF PTZ service that Frigate calls."""

    def __init__(self, sim: "Simulation") -> None:
        self.sim = sim
        self.ptz = sim.ptz

    async def RelativeMove(self, request) -> None:
        await asyncio.sleep(self.ptz.m.rtt / 2)
        x = request.Translation.PanTilt.x
        self.sim.on_relative_move(x)
        self.ptz.relative_move(x)
        await asyncio.sleep(self.ptz.m.rtt / 2)

    async def GetStatus(self, request):
        await asyncio.sleep(self.ptz.m.rtt / 2)
        status = self.ptz.move_status()
        await asyncio.sleep(self.ptz.m.rtt / 2)
        return SimpleNamespace(
            MoveStatus=SimpleNamespace(PanTilt=status, Zoom=None),
            Position=SimpleNamespace(Zoom=SimpleNamespace(x=0.0)),
        )

    async def GotoPreset(self, request) -> None:
        await asyncio.sleep(self.ptz.m.rtt / 2)
        self.ptz.goto_preset()
        await asyncio.sleep(self.ptz.m.rtt / 2)


@dataclass
class Scenario:
    # degrees per second, positive walks to the right
    person_speed: float = 10.0
    walk_time: float = 12.0
    stand_time: float = 8.0
    # where the person appears, as a fraction of half the FOV from center
    entry_offset: float = -0.85
    # angular size of the person
    person_width: float = 4.0
    person_height: float = 12.0
    detect_prob: float = 0.97
    # chance of a detection while the motor is panning (motion blur)
    detect_prob_moving: float = 0.8
    box_noise_px: float = 2.0
    false_positive_frames: int = 3


@dataclass
class PipelineModel:
    fps: int = 5
    # capture to Frigate receiving the frame
    latency: float = 0.5
    latency_jitter: float = 0.05
    # detection and tracked object processing before the autotracker sees it
    processing_delay: float = 0.08
    timeout: int = 10
    # onvif.autotracking.stream_latency, None to let Frigate measure it
    configured_latency: float | None = None
    # motion estimator error: per frame noise in px, chance of no estimate, and
    # the share of the person's movement in the frame that leaks into it
    shift_noise_px: float = 2.0
    estimator_dropout: float = 0.0
    leak: float = 0.1
    # overrides AUTOTRACKING_SETTLE_MOTION_STEP
    settle_threshold: float | None = None
    # latency once setup and calibration are done, to model a stream that slows
    latency_after_setup: float | None = None


def build_config(
    camera: CameraModel, pipeline: PipelineModel, calibrate: bool, weights
) -> FrigateConfig:
    autotracking: dict[str, Any] = {
        "enabled": True,
        "calibrate_on_startup": calibrate,
        "required_zones": ["zone"],
        "return_preset": "home",
        "timeout": pipeline.timeout,
    }

    # only pass it when set, so the simulator also runs against older code
    if pipeline.configured_latency is not None:
        autotracking["stream_latency"] = pipeline.configured_latency

    if weights:
        autotracking["movement_weights"] = weights

    return FrigateConfig(
        **{
            "mqtt": {"enabled": False},
            "cameras": {
                CAMERA: {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://127.0.0.1:554/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {
                        "width": camera.width,
                        "height": camera.height,
                        "fps": pipeline.fps,
                    },
                    "zones": {"zone": {"coordinates": "0,0,1,0,1,1,0,1"}},
                    "onvif": {"host": "127.0.0.1", "autotracking": autotracking},
                }
            },
        }
    )


class SimMotionEstimator(PtzMotionEstimator):
    """The real estimator, with the image-based estimate replaced."""

    def __init__(self, config, metrics, sim: "Simulation") -> None:
        super().__init__(config, metrics)
        self.sim = sim
        self.ref_pan: float | None = None
        self.seen_estimator = None

    def _estimate(self, detections, frame_name, frame_time, camera) -> None:
        # norfair measures against the first frame after a reset
        if self.norfair_motion_estimator is not self.seen_estimator:
            self.seen_estimator = self.norfair_motion_estimator
            self.ref_pan = None

        self.coord_transformations, self.ref_pan = self.sim.estimate_motion(
            frame_name, self.ref_pan
        )


class Simulation:
    def __init__(
        self,
        camera: CameraModel,
        pipeline: PipelineModel,
        scenario: Scenario,
        calibrate: bool = False,
        weights: list[float] | None = None,
        seed: int = 1,
    ) -> None:
        self.camera = camera
        self.pipeline = pipeline
        self.scenario = scenario
        self.calibrate = calibrate
        self.weights = weights
        self.rng = random.Random(seed)
        self.clock = VirtualClock(CLOCK_START)
        self.ptz = SimPtz(camera, self.clock)
        self.metrics = PTZMetrics()
        self.config = build_config(camera, pipeline, calibrate, weights)
        self.camera_config = self.config.cameras[CAMERA]

        self.scenario_start: float | None = None
        self.done = False
        self.last_receive = 0.0
        self.samples: list[dict[str, Any]] = []
        self.moves: list[dict[str, Any]] = []
        self.ended_tracks: list[dict[str, Any]] = []
        self.dequeued: tuple | None = None
        self.frames_by_time: dict[float, dict[str, Any]] = {}

        self.frames_by_name: dict[str, dict[str, Any]] = {}

        # the real estimator when the code has the hook, otherwise an emulation
        # of the older estimator with the same error model
        self.estimator = (
            SimMotionEstimator(self.camera_config, self.metrics, self)
            if hasattr(PtzMotionEstimator, "_estimate")
            else None
        )
        self.estimator_ref_pan: float | None = None
        self.coord_transform = None

        detect = self.camera_config.detect
        self.norfair = Tracker(
            distance_function=frigate_distance,
            distance_threshold=2,
            initialization_delay=detect.min_initialized,
            hit_counter_max=detect.max_disappeared,
            filter_factory=OptimizedKalmanFilterFactory(R=4.5, Q=0.25),
        )
        self.objects: dict[str, SimpleNamespace] = {}
        self.hits: dict[str, int] = {}

    # world

    def person_angle(self, t: float) -> float | None:
        if self.scenario_start is None or t < self.scenario_start:
            return None

        s = self.scenario
        start = self.camera.preset_pan + s.entry_offset * self.camera.hfov / 2
        walked = min(t - self.scenario_start, s.walk_time)
        return start + s.person_speed * walked

    def project(self, rel_angle: float) -> list[int] | None:
        m = self.camera
        if abs(rel_angle) >= 89:
            return None

        cx = m.width / 2 + m.focal_px * math.tan(math.radians(rel_angle))
        half_w = m.focal_px * math.radians(self.scenario.person_width) / 2
        half_h = m.focal_px * math.radians(self.scenario.person_height) / 2
        x1, x2 = cx - half_w, cx + half_w

        # require most of the person to be inside the frame to be detected
        if x1 < -half_w or x2 > m.width + half_w:
            return None

        cy = m.height / 2
        noise = self.scenario.box_noise_px
        box = [
            x1 + self.rng.gauss(0, noise),
            cy - half_h + self.rng.gauss(0, noise),
            x2 + self.rng.gauss(0, noise),
            cy + half_h + self.rng.gauss(0, noise),
        ]
        box = [
            int(min(max(box[0], 0), m.width - 1)),
            int(min(max(box[1], 0), m.height - 1)),
            int(min(max(box[2], 0), m.width - 1)),
            int(min(max(box[3], 0), m.height - 1)),
        ]
        return box if box[2] - box[0] > 4 else None

    def region_for(self, box: list[int]) -> list[int]:
        m = self.camera
        size = max(320, int(1.2 * max(box[2] - box[0], box[3] - box[1])))
        size = min(size, m.width, m.height)
        cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        x1 = min(max(cx - size // 2, 0), m.width - size)
        y1 = min(max(cy - size // 2, 0), m.height - size)
        return [x1, y1, x1 + size, y1 + size]

    # video pipeline

    async def frame_source(self) -> None:
        period = 1 / self.pipeline.fps
        next_t = self.clock.now

        while not self.done:
            await asyncio.sleep(max(0.0, next_t - self.clock.now))
            self.capture(next_t)
            next_t += period

    def capture(self, t_c: float) -> None:
        pan = self.ptz.pan_at(t_c)
        person = self.person_angle(t_c)
        moving = self.ptz.motor_moving(t_c)
        box = None

        if person is not None:
            prob = (
                self.scenario.detect_prob_moving
                if moving
                else self.scenario.detect_prob
            )
            if self.rng.random() < prob:
                box = self.project(person - pan)

        sample = {
            "t": t_c,
            "pan": pan,
            "person": person,
            "offset": None
            if person is None
            else (person - pan) / (self.camera.hfov / 2),
            "motor_moving": moving,
            "detected": box is not None,
            "person_px": None
            if person is None
            else self.camera.focal_px * math.radians(person - pan),
        }
        self.samples.append(sample)

        jitter = self.rng.uniform(
            -self.pipeline.latency_jitter, self.pipeline.latency_jitter
        )
        receive = max(t_c + self.pipeline.latency + jitter, self.last_receive + 1e-3)
        self.last_receive = receive
        loop = asyncio.get_running_loop()
        loop.call_at(receive, self.process_frame, sample, pan, box)

    def estimate_motion(self, frame_name: str, ref_pan: float | None):
        """Camera motion since ref_pan as norfair would estimate it, with errors."""
        sample = self.frames_by_name[frame_name]
        pan = sample["pan"]

        if ref_pan is None:
            ref_pan = pan

        if self.rng.random() < self.pipeline.estimator_dropout:
            return None, ref_pan

        shift = self.camera.focal_px * math.radians(pan - ref_pan)
        shift += self.rng.gauss(0, self.pipeline.shift_noise_px)
        if sample["person_px"] is not None:
            shift -= self.pipeline.leak * sample["person_px"]

        return TranslationTransformation(np.array([-shift, 0.0])), ref_pan

    def process_frame(self, sample: dict[str, Any], pan: float, box) -> None:
        """Mirror the camera process: stamp, estimate camera motion, track."""
        frame_time = self.clock.now
        frame_name = f"{CAMERA}_{sample['t']:.3f}"
        sample["frame_time"] = frame_time
        self.frames_by_time[frame_time] = sample
        self.frames_by_name[frame_name] = sample
        self.metrics.frame_time.value = frame_time

        detection_tuples = (
            [] if box is None else [("person", 0.9, box, 0, 0, self.region_for(box))]
        )

        if self.estimator is not None:
            flagged_moving = ptz_moving_at_frame_time(
                frame_time,
                self.metrics.start_time.value,
                self.metrics.stop_time.value,
                self.metrics.video_stop_time.value,
            )
            self.coord_transform = self.estimator.motion_estimator(
                detection_tuples, frame_name, frame_time, CAMERA
            )
        else:
            # PtzMotionEstimator.motion_estimator before the settle observer
            if self.metrics.reset.is_set():
                self.metrics.reset.clear()
                self.estimator_ref_pan = None
                self.coord_transform = None

            flagged_moving = ptz_moving_at_frame_time(
                frame_time,
                self.metrics.start_time.value,
                self.metrics.stop_time.value,
            )

            if flagged_moving:
                self.coord_transform, self.estimator_ref_pan = self.estimate_motion(
                    frame_name, self.estimator_ref_pan
                )

        sample["flagged_moving"] = flagged_moving

        detections = []
        if box is not None:
            detections.append(
                Detection(
                    points=np.array([[box[0], box[1]], [box[2], box[3]]]),
                    label="person",
                    data={
                        "label": "person",
                        "score": 0.9,
                        "box": tuple(box),
                        "area": (box[2] - box[0]) * (box[3] - box[1]),
                        "ratio": (box[2] - box[0]) / max(1, box[3] - box[1]),
                        "region": tuple(self.region_for(box)),
                        "frame_time": frame_time,
                        "centroid": (
                            int((box[0] + box[2]) / 2),
                            int((box[1] + box[3]) / 2),
                        ),
                    },
                )
            )

        tracked = self.norfair.update(
            detections=detections, coord_transformations=self.coord_transform
        )

        active = set()
        loop = asyncio.get_running_loop()
        for t in tracked:
            obj_id = str(t.global_id)
            active.add(obj_id)

            if t.last_detection.data["frame_time"] != frame_time:
                continue

            self.hits[obj_id] = self.hits.get(obj_id, 0) + 1
            obj = self.objects.get(obj_id)
            if obj is None:
                obj = SimpleNamespace(
                    camera_config=SimpleNamespace(name=CAMERA),
                    entered_zones=["zone"],
                    previous={"false_positive": True},
                    false_positive=True,
                    active=True,
                    obj_data={},
                )
                self.objects[obj_id] = obj

            obj.previous = {"false_positive": obj.false_positive}
            obj.false_positive = self.hits[obj_id] < self.scenario.false_positive_frames
            obj.obj_data = {
                **t.last_detection.data,
                "id": obj_id,
                "estimate_velocity": t.estimate_velocity,
            }

            if self.tracker.autotracker_init.get(CAMERA):
                loop.call_later(
                    self.pipeline.processing_delay,
                    self.tracker.autotrack_object,
                    CAMERA,
                    obj,
                )

        for obj_id in [k for k in self.objects if k not in active]:
            obj = self.objects.pop(obj_id)
            person = self.person_angle(self.clock.now)
            self.ended_tracks.append(
                {
                    "t": self.clock.now,
                    "id": obj_id,
                    "person_offset": None
                    if person is None
                    else (person - self.ptz.pan_at(self.clock.now))
                    / (self.camera.hfov / 2),
                }
            )
            if not obj.false_positive:
                self.tracker.end_object(CAMERA, obj)

    # instrumentation

    def on_relative_move(self, x: float) -> None:
        now = self.clock.now
        frame_time = self.dequeued[0] if self.dequeued else None
        source = self.frames_by_time.get(frame_time) if frame_time else None
        person = self.person_angle(now)
        previous_end = self.ptz.t_end
        self.moves.append(
            {
                "t": now,
                "pan_cmd": x,
                "calibrating": bool(self.tracker.calibrating.get(CAMERA)),
                "source_capture": source["t"] if source else None,
                "source_age": now - source["t"] if source else None,
                "source_offset": source["offset"] if source else None,
                "source_motor_moving": source["motor_moving"] if source else None,
                "source_stale": bool(source and source["t"] < previous_end),
                "true_offset": None
                if person is None
                else (person - self.ptz.pan_at(now)) / (self.camera.hfov / 2),
            }
        )

    def instrument_queue(self) -> None:
        queue = self.tracker.move_queues[CAMERA]
        original_get = queue.get

        async def get():
            item = await original_get()
            self.dequeued = item
            return item

        queue.get = get

    # setup and run

    def build_controllers(self) -> None:
        loop = asyncio.get_running_loop()

        onvif = OnvifController.__new__(OnvifController)
        onvif.config = self.config
        onvif.ptz_metrics = {CAMERA: self.metrics}
        onvif.failed_cams = {}
        onvif.status_locks = {CAMERA: asyncio.Lock()}
        onvif.loop = loop
        onvif.cams = {
            CAMERA: {
                "onvif": None,
                "init": True,
                "active": False,
                "features": ["pt", "pt-r", "pt-r-fov"],
                "presets": {"home": "1"},
                "preset_details": [{"token": "1", "name": "home"}],
                "max_presets": None,
                "profiles": [],
                "ptz": FakePtzService(self),
                "imaging": None,
                "video_source_token": None,
                "move_request": SimpleNamespace(ProfileToken="profile"),
                "relative_move_request": SimpleNamespace(
                    ProfileToken="profile",
                    Translation=SimpleNamespace(PanTilt=SimpleNamespace(x=0.0, y=0.0)),
                    Speed=None,
                ),
                "relative_fov_range": {
                    "XRange": {"Min": -1.0, "Max": 1.0},
                    "YRange": {"Min": -1.0, "Max": 1.0},
                },
            }
        }
        self.onvif = onvif

        tracker = PtzAutoTracker.__new__(PtzAutoTracker)
        tracker.config = self.config
        tracker.onvif = onvif
        tracker.ptz_metrics = {CAMERA: self.metrics}
        tracker.dispatcher = MagicMock()
        tracker.stop_event = threading.Event()
        tracker.tracked_object = {}
        tracker.tracked_object_history = {}
        tracker.tracked_object_metrics = {}
        tracker.move_queues = {}
        tracker.move_queue_locks = {}
        tracker.move_threads = {}
        tracker.autotracker_init = {}
        tracker.move_metrics = {}
        tracker.calibrating = {}
        tracker.intercept = {}
        tracker.move_coefficients = {}
        tracker.zoom_time = {}
        tracker.config_subscriber = MagicMock()
        self.tracker = tracker

    async def maintenance(self) -> None:
        while not self.done:
            await asyncio.sleep(1)
            if self.config.cameras[CAMERA].onvif.autotracking.enabled:
                await self.tracker.camera_maintenance(CAMERA)

    async def run(self) -> dict[str, Any]:
        self.build_controllers()
        source = asyncio.ensure_future(self.frame_source())
        await asyncio.sleep(0.5)

        await self.tracker._autotracker_setup(self.camera_config, CAMERA)
        self.instrument_queue()
        self.calibration_moves = len(self.moves)
        maintenance = asyncio.ensure_future(self.maintenance())

        if self.pipeline.latency_after_setup is not None:
            self.pipeline.latency = self.pipeline.latency_after_setup

        # let the camera settle at the preset after calibration
        await asyncio.sleep(2)
        self.scenario_start = self.clock.now + 1.0
        duration = 1.0 + self.scenario.walk_time + self.scenario.stand_time
        await asyncio.sleep(duration)

        self.done = True
        self.tracker.stop_event.set()
        await asyncio.gather(source, maintenance, return_exceptions=True)

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        return self.summarize()

    def summarize(self) -> dict[str, Any]:
        s = self.scenario
        start = self.scenario_start
        walk_end = start + s.walk_time
        end = walk_end + s.stand_time
        half_person = s.person_width / self.camera.hfov

        walk = [x for x in self.samples if start <= x["t"] < walk_end]
        stand = [x for x in self.samples if walk_end + 3 <= x["t"] < end]
        tracking = [x for x in self.samples if start <= x["t"] < end]
        moves = [m for m in self.moves if not m["calibrating"] and m["t"] >= start]

        def visible(x):
            return abs(x["offset"]) < 1 - half_person

        stale = [m for m in moves if m["source_stale"]]
        overshoots = 0
        for m in moves:
            if m["source_offset"] is None:
                continue
            # where was the person shortly after this move finished
            after = [
                x
                for x in self.samples
                if x["t"] > m["t"] and not x["motor_moving"] and x["t"] > m["t"] + 0.5
            ]
            if not after:
                continue
            settled = after[0]["offset"]
            if (
                settled is not None
                and np.sign(settled) == -np.sign(m["pan_cmd"])
                and abs(settled) > 0.3
            ):
                overshoots += 1

        # how far the camera got ahead of the person, in the walking direction
        direction = 1 if s.person_speed >= 0 else -1
        first_move = moves[0]["t"] + 1.0 if moves else end
        lead = max(
            (-direction * x["offset"] for x in tracking if x["t"] >= first_move),
            default=0.0,
        )

        flagged_wrong = [
            x
            for x in tracking
            if x["motor_moving"] and x.get("flagged_moving") is False
        ]

        return {
            "visible_walk": round(sum(visible(x) for x in walk) / max(1, len(walk)), 3),
            "mean_abs_offset_walk": round(
                float(np.mean([abs(x["offset"]) for x in walk])) if walk else 0.0, 3
            ),
            "final_abs_offset": round(
                float(np.mean([abs(x["offset"]) for x in stand])) if stand else 0.0,
                3,
            ),
            "moves": len(moves),
            "moves_from_stale_frames": len(stale),
            "overshoots": overshoots,
            "max_camera_lead": round(float(lead), 3),
            "moving_frames_flagged_stopped": len(flagged_wrong),
            "tracks_ended": len(
                [e for e in self.ended_tracks if e["t"] >= start and e["t"] < end]
            ),
            "measured_stream_latency": round(
                getattr(
                    self.metrics, "stream_latency", SimpleNamespace(value=-1)
                ).value,
                3,
            ),
            "movement_weights": self.config.cameras[
                CAMERA
            ].onvif.autotracking.movement_weights,
        }

    def plot(self, path: str, title: str) -> None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        start = self.scenario_start - 1
        xs = [x for x in self.samples if x["t"] >= start]
        t = [x["t"] - self.scenario_start for x in xs]
        half = self.camera.hfov / 2

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
        pans = np.array([x["pan"] for x in xs])
        ax1.fill_between(t, pans - half, pans + half, color="#9ecae1", alpha=0.4)
        ax1.plot(t, pans, color="#3182bd", label="camera pan (FOV shaded)")
        ax1.plot(
            t,
            [x["person"] if x["person"] is not None else np.nan for x in xs],
            color="#e6550d",
            label="person",
        )
        for m in self.moves:
            if m["t"] >= start and not m["calibrating"]:
                ax1.axvline(
                    m["t"] - self.scenario_start,
                    color="#d62728" if m["source_stale"] else "#2ca02c",
                    alpha=0.5,
                    lw=1,
                )
        ax1.set_ylabel("degrees")
        ax1.legend(loc="upper left")
        ax1.set_title(title)

        ax2.plot(
            t,
            [x["offset"] if x["offset"] is not None else np.nan for x in xs],
            color="#e6550d",
        )
        ax2.axhline(1, color="gray", ls="--")
        ax2.axhline(-1, color="gray", ls="--")
        ax2.axhline(0, color="gray", lw=0.5)
        ax2.set_ylim(-2.5, 2.5)
        ax2.set_ylabel("person offset (frame edge = +-1)")
        ax2.set_xlabel(
            "seconds since the person appeared "
            "(move lines: green = from a fresh frame, red = from a frame captured before the previous move finished)"
        )
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)


def run_simulation(
    camera: CameraModel,
    pipeline: PipelineModel,
    scenario: Scenario,
    calibrate: bool = False,
    weights: list[float] | None = None,
    seed: int = 1,
    plot: str | None = None,
    title: str = "",
) -> tuple[dict[str, Any], Simulation]:
    sim = Simulation(camera, pipeline, scenario, calibrate, weights, seed)

    selector = VirtualTimeSelector(sim.clock)
    loop = asyncio.SelectorEventLoop(selector)
    loop.time = sim.clock.time  # type: ignore[method-assign]
    loop._clock_resolution = 1e-6  # type: ignore[attr-defined]

    # calibration and PTZ move timing use time.time(); writing the config is skipped
    saved = (
        getattr(autotrack_module, "AUTOTRACKING_SETTLE_MOTION_STEP", None),
        autotrack_module.time,
        onvif_module.time,
        autotrack_module.update_yaml_file_bulk,
        autotrack_module.find_config_file,
    )
    autotrack_module.time = SimpleNamespace(time=sim.clock.time)  # type: ignore[assignment]
    if pipeline.settle_threshold is not None:
        autotrack_module.AUTOTRACKING_SETTLE_MOTION_STEP = (  # type: ignore[attr-defined]
            pipeline.settle_threshold
        )
    onvif_module.time = SimpleNamespace(time=sim.clock.time)  # type: ignore[assignment]
    autotrack_module.update_yaml_file_bulk = lambda *a, **k: None  # type: ignore[assignment]
    autotrack_module.find_config_file = lambda: "/dev/null"  # type: ignore[assignment]

    try:
        summary = loop.run_until_complete(sim.run())
    finally:
        (
            threshold,
            autotrack_module.time,
            onvif_module.time,
            autotrack_module.update_yaml_file_bulk,
            autotrack_module.find_config_file,
        ) = saved
        if threshold is not None:
            autotrack_module.AUTOTRACKING_SETTLE_MOTION_STEP = threshold  # type: ignore[attr-defined]
        loop.close()

    if plot:
        sim.plot(plot, title)

    return summary, sim


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--latency", type=float, default=0.5, help="capture to Frigate, s")
    p.add_argument(
        "--stream-latency",
        type=float,
        default=None,
        help="onvif.autotracking.stream_latency given to Frigate, s (default: measured)",
    )
    p.add_argument("--shift-noise", type=float, default=2.0, help="estimator noise, px")
    p.add_argument("--dropout", type=float, default=0.0, help="estimator failure rate")
    p.add_argument(
        "--leak", type=float, default=0.1, help="person motion leaking into estimate"
    )
    p.add_argument(
        "--settle-threshold",
        type=float,
        default=None,
        help="AUTOTRACKING_SETTLE_MOTION_STEP override",
    )
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--hfov", type=float, default=90.0)
    p.add_argument("--pan-speed", type=float, default=60.0, help="deg/s")
    p.add_argument("--start-delay", type=float, default=0.3, help="s")
    p.add_argument("--settle", type=float, default=0.2, help="s")
    p.add_argument("--fov-scale", type=float, default=1.0)
    p.add_argument("--idle-before-start", action="store_true")
    p.add_argument("--early-idle", type=float, default=None)
    p.add_argument("--person-speed", type=float, default=10.0, help="deg/s")
    p.add_argument("--walk-time", type=float, default=12.0)
    p.add_argument("--stand-time", type=float, default=8.0)
    p.add_argument("--calibrate", action="store_true")
    p.add_argument(
        "--weights",
        type=str,
        default=None,
        help="movement_weights from your config, comma separated",
    )
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--plot", type=str, default=None)
    p.add_argument(
        "--dump", type=str, default=None, help="write samples and moves as JSON"
    )
    p.add_argument("--moves", action="store_true", help="print every move")
    p.add_argument(
        "--sweep",
        action="store_true",
        help="sweep latency and MoveStatus behavior instead of a single run",
    )
    p.add_argument("--debug", action="store_true", help="autotracker debug logs")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(name)s %(message)s")
    if args.debug:
        logging.getLogger("frigate.ptz").setLevel(logging.DEBUG)

    def camera_model(**overrides) -> CameraModel:
        values = {
            "hfov": args.hfov,
            "pan_speed": args.pan_speed,
            "start_delay": args.start_delay,
            "settle": args.settle,
            "fov_scale": args.fov_scale,
            "idle_before_start": args.idle_before_start,
            "early_idle": args.early_idle,
        }
        values.update(overrides)
        return CameraModel(**values)

    scenario = Scenario(
        person_speed=args.person_speed,
        walk_time=args.walk_time,
        stand_time=args.stand_time,
    )
    weights = [float(v) for v in args.weights.split(",")] if args.weights else None

    if not args.sweep:
        summary, sim = run_simulation(
            camera_model(),
            PipelineModel(
                fps=args.fps,
                latency=args.latency,
                configured_latency=args.stream_latency,
                shift_noise_px=args.shift_noise,
                estimator_dropout=args.dropout,
                leak=args.leak,
                settle_threshold=args.settle_threshold,
            ),
            scenario,
            calibrate=args.calibrate,
            weights=weights,
            seed=args.seed,
            plot=args.plot,
            title=f"latency {args.latency}s (stream_latency {args.stream_latency}), "
            f"pan {args.pan_speed} deg/s, "
            f"person {args.person_speed} deg/s",
        )
        if args.dump:
            with open(args.dump, "w") as f:
                json.dump(
                    {
                        "args": vars(args),
                        "scenario_start": sim.scenario_start,
                        "hfov": sim.camera.hfov,
                        "summary": summary,
                        "samples": sim.samples,
                        "moves": sim.moves,
                    },
                    f,
                    default=str,
                )
        if args.moves:
            for m in sim.moves:
                print(
                    json.dumps(
                        {
                            k: (round(v, 3) if isinstance(v, float) else v)
                            for k, v in m.items()
                        },
                        default=str,
                    )
                )
        print(json.dumps(summary, indent=2, default=str))
        return

    rows = []
    for latency, configured, calibrate in itertools.product(
        [0.1, 0.4, 0.8, 1.2, 1.5], ["measured", "actual"], [False, True]
    ):
        status_mode = "idle_before_start" if args.idle_before_start else "ok"
        summaries = []
        for seed in range(3):
            summary, _ = run_simulation(
                camera_model(idle_before_start=status_mode == "idle_before_start"),
                PipelineModel(
                    fps=args.fps,
                    latency=latency,
                    configured_latency=latency if configured == "actual" else None,
                    shift_noise_px=args.shift_noise,
                    estimator_dropout=args.dropout,
                    leak=args.leak,
                    settle_threshold=args.settle_threshold,
                ),
                scenario,
                calibrate=calibrate,
                weights=None,
                seed=seed,
            )
            summaries.append(summary)
        rows.append(
            {
                "latency": latency,
                "stream_latency": configured,
                "move_status": status_mode,
                "calibrated": calibrate,
                "visible_walk": round(
                    float(np.mean([x["visible_walk"] for x in summaries])), 2
                ),
                "final_abs_offset": round(
                    float(np.mean([x["final_abs_offset"] for x in summaries])), 2
                ),
                "moves": round(float(np.mean([x["moves"] for x in summaries])), 1),
                "stale_moves": round(
                    float(np.mean([x["moves_from_stale_frames"] for x in summaries])),
                    1,
                ),
                "overshoots": round(
                    float(np.mean([x["overshoots"] for x in summaries])), 1
                ),
                "max_lead": round(
                    float(np.mean([x["max_camera_lead"] for x in summaries])), 2
                ),
            }
        )

    keys = list(rows[0].keys())
    print("  ".join(f"{k:>17}" for k in keys))
    for r in rows:
        print("  ".join(f"{str(r[k]):>17}" for k in keys))


if __name__ == "__main__":
    main()
