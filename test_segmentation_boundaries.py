import unittest
from pathlib import Path

import jam_mix_pipeline as pipeline


class SegmentationBoundaryTests(unittest.TestCase):
    def test_metadata_registration_creates_one_review_window(self):
        stem = pipeline.Stem(
            path=Path("jam.wav"),
            name="jam",
            role="drums",
            samplerate=44100,
            channels=1,
            frames=44100 * 22140,
            duration=22140.0,
            timeline_frames=44100 * 22140,
            offset_seconds=0.0,
            offset_source="test",
        )
        segments = pipeline.provisional_segments_from_metadata([stem])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].boundary_source, "metadata-provisional")
        self.assertEqual(segments[0].duration, 22140.0)

    def test_unsafe_boundary_is_merged_and_marked_for_review(self):
        original = pipeline._boundary_activity
        try:
            pipeline._boundary_activity = lambda *args, **kwargs: (False, ["Guit.wav"], [])
            stem = pipeline.Stem(
                path=Path("jam.wav"),
                name="jam",
                role="guitar",
                samplerate=44100,
                channels=1,
                frames=44100 * 1200,
                duration=1200.0,
                timeline_frames=44100 * 1200,
                offset_seconds=0.0,
                offset_source="test",
            )
            left = pipeline.Segment(0.0, 600.0, core_start=0.0, core_end=600.0, nominal_end=600.0)
            right = pipeline.Segment(600.0, 1200.0, core_start=600.0, core_end=1200.0, nominal_end=1200.0)
            result = pipeline.merge_unsafe_music_boundaries([left, right], [stem], {})
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].boundary_validation, "needs_review")
            self.assertIn("active music", result[0].boundary_validation_reason)
        finally:
            pipeline._boundary_activity = original


if __name__ == "__main__":
    unittest.main()
