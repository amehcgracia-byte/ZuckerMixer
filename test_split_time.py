import unittest

from jam_app import parse_split_offset_seconds


class SplitTimeParsingTest(unittest.TestCase):
    def test_mm_ss_split_is_relative_minutes_seconds(self):
        song_start = 8 * 3600 + 48 * 60 + 25
        offset = parse_split_offset_seconds("12:10")
        self.assertEqual(offset, 12 * 60 + 10)
        self.assertEqual(song_start + offset, 9 * 3600 + 35)

    def test_decimal_typo_compatibility_is_not_fractional_seconds(self):
        self.assertEqual(parse_split_offset_seconds("12.10"), 12 * 60 + 10)


if __name__ == "__main__":
    unittest.main()
