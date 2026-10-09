"""Tests for the live PTZ log shown in the camera Debug view."""

import copy
import json
import threading
import unittest

from frigate.const import (
    PTZ_DEBUG_MAX_ENTRIES,
    PTZ_DEBUG_RETENTION,
    PTZ_DEBUG_WATCH_TIMEOUT,
)
from frigate.ptz.event_log import PtzEventLog, PtzSource, finite_number

CAMERA = "ptz_cam"


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_log() -> tuple[PtzEventLog, FakeClock]:
    clock = FakeClock()
    return PtzEventLog(clock=clock, wall_clock=clock), clock


def status(pan_tilt: str, pan: float, tilt: float = 0.0) -> dict:
    return {
        "pan_tilt": pan_tilt,
        "zoom": None,
        "position": {"pan": pan, "tilt": tilt, "zoom": None},
    }


def zoom_status(zoom: float, zoom_state: str = "MOVING") -> dict:
    # cameras that report the move status per axis show PanTilt IDLE while
    # only the zoom moves
    return {
        "pan_tilt": "IDLE",
        "zoom": zoom_state,
        "position": {"pan": 0.1, "tilt": 0.2, "zoom": zoom},
    }


def stop_request(log: PtzEventLog) -> None:
    log.record(CAMERA, PtzSource.command, "request", {"operation": "Stop"})


def skip(log: PtzEventLog, reason: str = "centered") -> None:
    log.record(
        CAMERA,
        PtzSource.autotrack,
        "autotrack",
        {"event": "skipped", "reason": reason},
        collapse_key=("skipped", reason),
    )


def kinds(log: PtzEventLog) -> list[str]:
    return [entry["kind"] for entry in log.entries(CAMERA, 0)[0]]


class TestWatching(unittest.TestCase):
    def test_nothing_is_recorded_while_the_log_is_closed(self) -> None:
        log, _ = make_log()
        stop_request(log)
        log.watch(CAMERA)

        self.assertEqual(kinds(log), [])

    def test_records_while_the_log_is_open(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        stop_request(log)

        entries, seq, missed = log.entries(CAMERA, 0)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "request")
        self.assertEqual(entries[0]["source"], "command")
        self.assertEqual(entries[0]["seq"], seq)
        self.assertEqual(entries[0]["time"], 1000.0)
        self.assertFalse(missed)

    def test_log_closes_when_no_longer_polled(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        self.assertTrue(log.is_watched(CAMERA))

        clock.now += PTZ_DEBUG_WATCH_TIMEOUT

        self.assertFalse(log.is_watched(CAMERA))

    def test_entries_after_a_seq(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        stop_request(log)
        _, seen, _ = log.entries(CAMERA, 0)
        stop_request(log)

        entries, _, _ = log.entries(CAMERA, seen)

        self.assertEqual(len(entries), 1)
        self.assertGreater(entries[0]["seq"], seen)

    def test_reopened_log_drops_old_entries_and_reports_the_gap(self) -> None:
        # a browser tab hidden for a while stops polling, so the log closed
        log, clock = make_log()
        log.watch(CAMERA)
        stop_request(log)
        _, seen, _ = log.entries(CAMERA, 0)
        stop_request(log)  # never polled

        clock.now += PTZ_DEBUG_WATCH_TIMEOUT + 1
        log.watch(CAMERA)
        entries, _, missed = log.entries(CAMERA, seen)

        self.assertEqual(entries, [])
        self.assertTrue(missed)

    def test_reopened_log_always_reports_the_gap(self) -> None:
        # the camera was not logged while the log was closed, even if the
        # client had seen everything recorded before
        log, clock = make_log()
        log.watch(CAMERA)
        stop_request(log)
        _, seen, _ = log.entries(CAMERA, 0)

        clock.now += PTZ_DEBUG_WATCH_TIMEOUT + 1
        log.watch(CAMERA)

        self.assertTrue(log.entries(CAMERA, seen)[2])

    def test_reopened_log_that_held_nothing_reports_the_gap(self) -> None:
        # the seq a client holds counts entries of every camera
        log, clock = make_log()
        log.watch(CAMERA)
        log.watch("other_cam")
        log.record("other_cam", PtzSource.command, "request", {"operation": "Stop"})
        entries, seen, _ = log.entries(CAMERA, 0)
        self.assertEqual(entries, [])

        clock.now += PTZ_DEBUG_WATCH_TIMEOUT + 1
        log.watch(CAMERA)
        entries, seq, missed = log.entries(CAMERA, seen)

        self.assertEqual(entries, [])
        self.assertTrue(missed)
        # the client has now seen the gap, so it is not reported again
        self.assertFalse(log.entries(CAMERA, seq)[2])

    def test_polling_without_a_break_reports_no_gap(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        stop_request(log)
        _, seen, _ = log.entries(CAMERA, 0)

        clock.now += PTZ_DEBUG_WATCH_TIMEOUT - 1
        log.watch(CAMERA)

        self.assertFalse(log.entries(CAMERA, seen)[2])

    def test_a_new_client_has_missed_nothing(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        stop_request(log)
        clock.now += PTZ_DEBUG_WATCH_TIMEOUT + 1
        log.watch(CAMERA)

        self.assertFalse(log.entries(CAMERA, 0)[2])

    def test_removed_camera_is_forgotten(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        stop_request(log)

        log.remove_camera(CAMERA)

        self.assertFalse(log.is_watched(CAMERA))
        self.assertEqual(kinds(log), [])


class TestCollapsing(unittest.TestCase):
    def test_repeats_are_one_row(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        skip(log)
        clock.now += 0.5
        skip(log)
        clock.now += 0.5
        skip(log)

        entries, _, _ = log.entries(CAMERA, 0)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repeats"], 3)
        self.assertEqual(entries[0]["time"], 1000.0)
        self.assertEqual(entries[0]["until"], 1001.0)

    def test_an_updated_row_is_sent_again(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        skip(log)
        entries, seen, _ = log.entries(CAMERA, 0)
        first_id = entries[0]["id"]

        skip(log)
        again, _, _ = log.entries(CAMERA, seen)

        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["id"], first_id)
        self.assertEqual(again[0]["repeats"], 2)

    def test_a_row_already_handed_out_is_never_changed(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        skip(log)
        entries, seen, _ = log.entries(CAMERA, 0)
        held = entries[0]
        snapshot = copy.deepcopy(held)

        skip(log)

        self.assertEqual(held, snapshot)
        updated = log.entries(CAMERA, seen)[0][0]
        self.assertIsNot(updated, held)
        self.assertEqual(updated["repeats"], 2)

    def test_another_entry_ends_the_run(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        skip(log)
        stop_request(log)
        skip(log)

        self.assertEqual(kinds(log), ["autotrack", "request", "autotrack"])

    def test_a_different_reason_is_a_new_row(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        skip(log, "centered")
        skip(log, "stale_frame")

        self.assertEqual(len(log.entries(CAMERA, 0)[0]), 2)


class TestStatus(unittest.TestCase):
    def test_a_move_is_one_row_that_keeps_where_it_started(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        for pan in (0.1, 0.2, 0.3):
            log.record_status(CAMERA, PtzSource.autotrack, status("MOVING", pan), None)

        entries, _, _ = log.entries(CAMERA, 0)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repeats"], 3)
        self.assertEqual(entries[0]["data"]["position"]["pan"], 0.3)
        self.assertEqual(entries[0]["data"]["position_start"]["pan"], 0.1)

    def test_idle_reads_at_one_position_are_one_row(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.3), None)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.3), None)

        self.assertEqual(log.entries(CAMERA, 0)[0][0]["repeats"], 2)

    def test_latest_status_and_its_age(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.3), None)
        clock.now += 0.4

        latest, age = log.latest_status(CAMERA)

        self.assertEqual(latest["pan_tilt"], "IDLE")
        self.assertEqual(latest["position"]["pan"], 0.3)
        self.assertEqual(latest["time"], 1000.0)
        self.assertAlmostEqual(age, 0.4)

    def test_no_status_before_a_read(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)

        self.assertEqual(log.latest_status(CAMERA), (None, None))

    def test_failed_read_is_recorded(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, None, "timeout")

        entry = log.entries(CAMERA, 0)[0][0]
        latest, _ = log.latest_status(CAMERA)

        self.assertEqual(entry["data"]["error"], "timeout")
        self.assertEqual(latest["error"], "timeout")
        self.assertIsNone(latest["position"])


class TestOutsideMoves(unittest.TestCase):
    def test_position_change_without_a_command(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.0), None)
        clock.now += 1
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.5), None)

        entries, _, _ = log.entries(CAMERA, 0)

        self.assertEqual(
            [e["kind"] for e in entries], ["status", "external_move", "status"]
        )
        self.assertEqual(entries[1]["data"]["from"]["pan"], 0.0)
        self.assertEqual(entries[1]["data"]["to"]["pan"], 0.5)

    def test_not_after_a_frigate_move(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.0), None)
        clock.now += 3
        log.record(CAMERA, PtzSource.command, "request", {"operation": "RelativeMove"})
        clock.now += 1
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.5), None)

        self.assertEqual(kinds(log), ["status", "request", "status"])

    def test_not_while_a_frigate_move_may_still_be_running(self) -> None:
        # some cameras report IDLE before the motor has stopped
        log, clock = make_log()
        log.watch(CAMERA)
        log.record(CAMERA, PtzSource.command, "request", {"operation": "RelativeMove"})
        clock.now += 0.5
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.0), None)
        clock.now += 1
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.5), None)

        self.assertEqual(kinds(log), ["request", "status", "status"])

    def test_not_while_moving(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("MOVING", 0.0), None)
        clock.now += 1
        log.record_status(CAMERA, PtzSource.debug, status("MOVING", 0.5), None)

        self.assertEqual(kinds(log), ["status"])

    def test_read_noise_is_not_a_move(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.00001), None)
        clock.now += 1
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.00002), None)

        self.assertEqual(kinds(log), ["status"])


class TestZoomMoves(unittest.TestCase):
    def test_a_zoom_is_one_row_and_no_outside_move(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, zoom_status(0.0, "IDLE"), None)
        clock.now += 1
        log.record(CAMERA, PtzSource.command, "request", {"operation": "AbsoluteMove"})

        # PanTilt stays IDLE and the zoom position changes on every read, for
        # longer than the grace period after the request
        for i in range(1, 61):
            clock.now += 0.05
            log.record_status(CAMERA, PtzSource.debug, zoom_status(i / 100), None)

        entries, _, _ = log.entries(CAMERA, 0)

        self.assertEqual([e["kind"] for e in entries], ["status", "request", "status"])
        self.assertEqual(entries[2]["repeats"], 60)
        self.assertEqual(entries[2]["data"]["position"]["zoom"], 0.6)
        self.assertEqual(entries[2]["data"]["position_start"]["zoom"], 0.01)

    def test_the_end_of_a_zoom_is_not_an_outside_move(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record(CAMERA, PtzSource.command, "request", {"operation": "AbsoluteMove"})
        clock.now += 3
        log.record_status(CAMERA, PtzSource.debug, zoom_status(0.4), None)
        clock.now += 1
        log.record_status(CAMERA, PtzSource.debug, zoom_status(0.8, "IDLE"), None)

        self.assertEqual(kinds(log), ["request", "status", "status"])

    def test_a_zoom_change_while_both_axes_are_idle_is_an_outside_move(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, zoom_status(0.2, "IDLE"), None)
        clock.now += 1
        log.record_status(CAMERA, PtzSource.debug, zoom_status(0.7, "IDLE"), None)

        self.assertEqual(kinds(log), ["status", "external_move", "status"])

    def test_idle_reads_at_one_position_are_one_row_when_zoom_reports_idle(
        self,
    ) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, zoom_status(0.2, "IDLE"), None)
        log.record_status(CAMERA, PtzSource.debug, zoom_status(0.2, "IDLE"), None)

        self.assertEqual(log.entries(CAMERA, 0)[0][0]["repeats"], 2)

    def test_a_pan_move_with_a_zoom_that_stays_idle_is_still_one_row(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        for pan in (0.1, 0.2, 0.3):
            clock.now += 0.05
            log.record_status(
                CAMERA,
                PtzSource.debug,
                {**status("MOVING", pan), "zoom": "IDLE"},
                None,
            )

        entries, _, _ = log.entries(CAMERA, 0)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repeats"], 3)


class TestMoveStarted(unittest.TestCase):
    def test_not_while_a_move_request_waits_for_its_answer(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.0), None)
        clock.now += 3
        # the request is out, the camera has not answered it yet
        log.move_started(CAMERA)
        clock.now += 0.5
        log.record_status(CAMERA, PtzSource.debug, status("MOVING", 0.2), None)
        clock.now += 0.5
        log.record(CAMERA, PtzSource.command, "request", {"operation": "RelativeMove"})

        self.assertEqual(kinds(log), ["status", "status", "request"])

    def test_the_move_is_still_not_outside_when_the_camera_reports_idle_early(
        self,
    ) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.0), None)
        clock.now += 3
        log.move_started(CAMERA)
        clock.now += 0.5
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.1), None)
        clock.now += 0.5
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.2), None)

        self.assertNotIn("external_move", kinds(log))

    def test_records_nothing_itself(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)

        log.move_started(CAMERA)

        self.assertEqual(kinds(log), [])

    def test_does_nothing_while_the_log_is_closed(self) -> None:
        log, clock = make_log()

        log.move_started(CAMERA)
        log.watch(CAMERA)
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.0), None)
        clock.now += 3
        log.record_status(CAMERA, PtzSource.debug, status("IDLE", 0.5), None)

        self.assertEqual(kinds(log), ["status", "external_move", "status"])


class TestLimits(unittest.TestCase):
    def test_old_entries_are_dropped(self) -> None:
        log, clock = make_log()
        log.watch(CAMERA)
        stop_request(log)
        _, seen, _ = log.entries(CAMERA, 0)

        # keep polling, as an open Debug view does
        for _ in range(int(PTZ_DEBUG_RETENTION) + 2):
            clock.now += 1
            log.watch(CAMERA)
        stop_request(log)

        entries, _, _ = log.entries(CAMERA, 0)

        self.assertEqual(len(entries), 1)
        self.assertGreater(entries[0]["seq"], seen)
        # the client had seen the dropped entry
        self.assertFalse(log.entries(CAMERA, seen)[2])

    def test_size_limit(self) -> None:
        log, _ = make_log()
        log.watch(CAMERA)
        for i in range(PTZ_DEBUG_MAX_ENTRIES + 10):
            log.record(
                CAMERA, PtzSource.command, "request", {"operation": "Stop", "i": i}
            )

        entries, _, _ = log.entries(CAMERA, 0)

        self.assertEqual(len(entries), PTZ_DEBUG_MAX_ENTRIES)
        self.assertEqual(entries[0]["data"]["i"], 10)
        self.assertTrue(log.entries(CAMERA, 1)[2])


class TestThreads(unittest.TestCase):
    def test_entries_can_be_serialized_while_others_record(self) -> None:
        log = PtzEventLog()
        log.watch(CAMERA)
        errors: list[Exception] = []

        def writer() -> None:
            try:
                for i in range(2000):
                    skip(log)
                    log.record_status(
                        CAMERA, PtzSource.autotrack, status("MOVING", i / 2000), None
                    )
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        for thread in threads:
            thread.start()

        while any(thread.is_alive() for thread in threads):
            entries, _, _ = log.entries(CAMERA, 0)
            json.dumps(entries)

        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertLessEqual(len(log.entries(CAMERA, 0)[0]), PTZ_DEBUG_MAX_ENTRIES)


class TestFiniteNumber(unittest.TestCase):
    def test_numbers_become_floats(self) -> None:
        self.assertEqual(finite_number(3), 3.0)
        self.assertEqual(finite_number("1.5"), 1.5)

    def test_anything_the_api_cannot_serialize_becomes_none(self) -> None:
        for value in (
            None,
            "abc",
            [],
            float("nan"),
            float("inf"),
            -float("inf"),
            10**400,
        ):
            with self.subTest(value=value):
                self.assertIsNone(finite_number(value))
