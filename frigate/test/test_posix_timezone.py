"""Tests for converting user timezones to the POSIX strings ONVIF expects."""

import unittest

from frigate.util.time import posix_timezone


class TestPosixTimezone(unittest.TestCase):
    def test_utc_offsets_invert_the_sign(self):
        for value, expected in (
            ("UTC", "UTC0"),
            ("GMT", "UTC0"),
            ("UTC+0", "UTC0"),
            ("UTC-0:00", "UTC0"),
            ("UTC+3", "UTC-3"),
            ("utc+03", "UTC-3"),
            ("UTC-5", "UTC5"),
            ("GMT-4", "UTC4"),
            ("UTC+5:30", "UTC-5:30"),
            ("UTC+0545", "UTC-5:45"),
            ("UTC -3:30", "UTC3:30"),
            ("UTC+14", "UTC-14"),
            (" UTC+3 ", "UTC-3"),
        ):
            with self.subTest(value=value):
                self.assertEqual(posix_timezone(value), expected)

    def test_iana_zones_use_the_zone_rule(self):
        for value, expected in (
            ("Europe/Moscow", "MSK-3"),
            ("europe/moscow", "MSK-3"),
            ("America/New_York", "EST5EDT,M3.2.0,M11.1.0"),
            ("Asia/Kolkata", "IST-5:30"),
            ("Europe/Istanbul", "<+03>-3"),
            ("Etc/UTC", "UTC0"),
        ):
            with self.subTest(value=value):
                self.assertEqual(posix_timezone(value), expected)

    def test_invalid_values(self):
        for value in (
            "",
            "UTC+",
            "UTC+15",
            "UTC+3:60",
            "UTC+3:5",
            "+3",
            "Mars/Olympus_Mons",
            "../../etc/passwd",
            "Europe/../../etc/passwd",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    posix_timezone(value)


if __name__ == "__main__":
    unittest.main()
