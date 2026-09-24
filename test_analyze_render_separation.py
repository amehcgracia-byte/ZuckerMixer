import unittest
from pathlib import Path

import jam_app
import jam_mix_pipeline as pipeline


class AnalyzeRenderSeparationTest(unittest.TestCase):
    def test_render_requires_a_frozen_analysis_plan(self):
        stem = pipeline.Stem(
            path=Path("missing.wav"),
            role="vocal",
            name="Vocal",
            samplerate=48_000,
            channels=1,
            frames=48_000,
            duration=1.0,
            timeline_frames=48_000,
            offset_seconds=0.0,
            offset_source="test",
        )
        with self.assertRaisesRegex(RuntimeError, "Analyze required"):
            pipeline.render_segment(
                [stem],
                pipeline.Segment(0.0, 1.0),
                1,
                Path("/tmp/zucker-test-render"),
            )

    def test_plan_hash_changes_when_selection_changes(self):
        first = pipeline.Segment(0.0, 480.0)
        second = pipeline.Segment(0.0, 481.0)
        self.assertNotEqual(
            jam_app.mix_plan_signature(1, first, {"target_lufs": -14}),
            jam_app.mix_plan_signature(1, second, {"target_lufs": -14}),
        )


if __name__ == "__main__":
    unittest.main()
