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

    def test_unsafe_boundary_is_retained_and_marked_for_review(self):
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
            self.assertEqual(len(result), 2)
            self.assertTrue(all(item.boundary_validation == "needs_review" for item in result))
            self.assertTrue(all("active music" in item.boundary_validation_reason for item in result))
        finally:
            pipeline._boundary_activity = original

    def test_parallel_stems_use_one_global_bounded_cut_list(self):
        stems = [
            pipeline.Stem(
                path=Path(f"stem-{index}.wav"),
                name=f"stem-{index}",
                role="drums" if index == 0 else "guitar",
                samplerate=10,
                channels=1,
                frames=20000,
                duration=2000.0,
                timeline_frames=20000,
                offset_seconds=0.0,
                offset_source="test",
            )
            for index in range(11)
        ]
        timelines = {stem.path.name: __import__("numpy").ones(1000, dtype=float) for stem in stems}
        original_gate = pipeline._boundary_activity
        try:
            pipeline._boundary_activity = lambda *args, **kwargs: (True, [], [])
            result = pipeline.build_duration_bounded_global_proposals(
                [pipeline.Segment(0.0, 2000.0, core_start=0.0, core_end=2000.0, nominal_end=2000.0)],
                stems,
                timelines,
                target_count=3,
            )
            self.assertEqual(len(result), 3)
            self.assertEqual([round(item.duration, 3) for item in result], [round(item.duration, 3) for item in result])
            self.assertTrue(all(480.0 <= item.duration <= 780.0 for item in result))
            self.assertEqual([item.start for item in result], [0.0, result[1].start, result[2].start])
        finally:
            pipeline._boundary_activity = original_gate

    def test_active_session_end_uses_shared_stem_activity(self):
        import numpy as np

        stem = pipeline.Stem(
            path=Path("stem.wav"), name="stem", role="drums", samplerate=10,
            channels=1, frames=20000, duration=2000.0, timeline_frames=20000,
            offset_seconds=0.0, offset_source="test",
        )
        timelines = {"stem.wav": np.concatenate([np.ones(80), np.zeros(20)])}
        self.assertLess(pipeline.active_session_end_from_timelines([stem], timelines, 2000.0), 2000.0)

    def test_final_gate_uses_shared_timeline_and_returns_review_instead_of_raising(self):
        import numpy as np

        stems = [
            pipeline.Stem(Path("Guit.wav"), "Guit", "guitar", 100, 1, 100000, 1000.0, 100000, 0.0, "test"),
            pipeline.Stem(Path("Snare.wav"), "Snare", "snare", 100, 1, 100000, 1000.0, 100000, 0.0, "test"),
        ]
        timelines = {stem.path.name: np.zeros(1000, dtype=float) for stem in stems}
        for timeline in timelines.values():
            timeline[590:603] = 1.0
        segments = [pipeline.Segment(0.0, 600.0), pipeline.Segment(600.0, 1000.0)]
        audit = pipeline.validate_final_render_boundaries(stems, segments, [21, 22], timelines)
        self.assertTrue(audit[0]["needs_review"] is False)
        self.assertEqual(audit[0]["accepted_cut_seconds"], 520.0)
        self.assertEqual(audit[0]["per_stem_samples"]["Guit.wav"], 60000)

    def test_commentator_presentations_define_slots_without_internal_splits(self):
        announcements = [
            {"start": 10.0, "end": 20.0, "announcement_start": 10.0, "text": "First presentation", "speech_intro_text": "First presentation", "speech_confidence": 0.9},
            {"start": 900.0, "end": 910.0, "announcement_start": 900.0, "text": "Second presentation", "speech_intro_text": "Second presentation", "speech_confidence": 0.9},
        ]
        result = pipeline.finalize_presented_slot_segments(announcements, 1200.0)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0].start, 9.5)
        self.assertAlmostEqual(result[0].end, 899.5)
        self.assertEqual(result[0].boundary_source, "whisper-presented-slot")
        self.assertEqual(result[0].boundary_validation, "needs_review")
        self.assertIn("above 13:00", result[0].boundary_validation_reason)


if __name__ == "__main__":
    unittest.main()
