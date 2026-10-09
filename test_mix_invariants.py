import unittest

import jam_mix_pipeline as pipeline


class MixInvariantTest(unittest.TestCase):
    def test_automatic_pan_assignments_are_stable_across_user_overrides(self):
        for role, expected in [("guitar", 0.25), ("keys", 0.35), ("synth", 0.15)]:
            with self.subTest(role=role):
                self.assertEqual(pipeline.pan_for_role(role, role), expected)
                self.assertEqual(pipeline.enforced_pan(role, role, -0.8), expected)
                self.assertEqual(pipeline.enforced_pan(role, role, 0.8), expected)

    def test_pre_session_content_gets_song_zero(self):
        segments = [
            pipeline.Segment(0.0, 120.0, speech_intro_start=30.0, spoken_song_number=1),
            pipeline.Segment(120.0, 720.0, spoken_song_number=2),
        ]
        result = pipeline.apply_spoken_number_structure(segments, [], 720.0)
        self.assertEqual([s.assigned_song_number for s in result], [0, 1, 2])
        self.assertEqual(result[0].boundary_source, "pre-session-content")
        self.assertEqual(result[0].end, 30.0)
        self.assertEqual(result[1].start, 30.0)

    def test_no_pre_session_content_does_not_create_song_zero(self):
        segments = [pipeline.Segment(0.0, 120.0, speech_intro_start=0.5, spoken_song_number=1)]
        result = pipeline.apply_spoken_number_structure(segments, [], 120.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].assigned_song_number, 1)

    def test_silent_pre_session_segment_does_not_create_song_zero(self):
        stems = [pipeline.Stem(path=pipeline.Path("guitar.wav"), role="guitar", name="Guitar", samplerate=1000, channels=1, frames=1000, duration=1.0, timeline_frames=1000, offset_seconds=0.0, offset_source="test")]
        segments = [
            pipeline.Segment(0.0, 10.0, spoken_song_number=None),
            pipeline.Segment(10.0, 120.0, speech_intro_start=20.0, spoken_song_number=1),
        ]
        timelines = {"guitar.wav": pipeline.np.zeros(1000, dtype=pipeline.np.float32)}
        result = pipeline.apply_spoken_number_structure(segments, [], 120.0, stems=stems, timelines=timelines)
        self.assertEqual([s.assigned_song_number for s in result], [1])

    def test_auto_mix_never_boosts_without_explicit_user_control(self):
        requested = pipeline.automatic_makeup_gain_db(-48.0, "keys")
        self.assertLessEqual(min(requested, pipeline.AUTO_MIX_MAX_BOOST_DB), 0.0)
        self.assertEqual(pipeline.AUTO_MIX_MAX_BOOST_DB, 0.0)

    def test_per_song_active_balance_allows_only_bounded_voice_and_bass_recovery(self):
        self.assertAlmostEqual(pipeline.per_song_auto_mix_gain_db("vocal", -25.0, -18.0), 3.0)
        self.assertAlmostEqual(pipeline.per_song_auto_mix_gain_db("bass", -25.0, -18.0), 3.0)
        self.assertEqual(pipeline.per_song_auto_mix_gain_db("keys", -25.0, -18.0), 0.0)
        self.assertEqual(pipeline.per_song_auto_mix_gain_db("guitar", -5.0, -18.0), -12.0)

    def test_per_song_balance_changes_with_each_song_reference(self):
        song_quiet = pipeline.per_song_auto_mix_gain_db("vocal", -30.0, -20.0)
        song_balanced = pipeline.per_song_auto_mix_gain_db("vocal", -18.0, -20.0)
        self.assertNotEqual(song_quiet, song_balanced)
        self.assertLessEqual(song_quiet, 3.0)


if __name__ == "__main__":
    unittest.main()
