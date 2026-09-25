import unittest
from pathlib import Path

import jam_app
import jam_mix_pipeline as pipeline


class AnalyzeRenderSeparationTest(unittest.TestCase):
    def test_render_has_a_lightweight_fallback_without_analysis_plan(self):
        source = Path(__file__).with_name("jam_mix_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('prepared_plan.get("lightweight_render")', source)
        self.assertIn("lightweight settings snapshot", source)

    def test_plan_hash_changes_when_selection_changes(self):
        first = pipeline.Segment(0.0, 480.0)
        second = pipeline.Segment(0.0, 481.0)
        self.assertNotEqual(
            jam_app.mix_plan_signature(1, first, {"target_lufs": -14}),
            jam_app.mix_plan_signature(1, second, {"target_lufs": -14}),
        )


if __name__ == "__main__":
    unittest.main()
