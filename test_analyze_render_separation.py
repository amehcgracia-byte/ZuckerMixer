import unittest
from pathlib import Path

import jam_app
import jam_mix_pipeline as pipeline


class AnalyzeRenderSeparationTest(unittest.TestCase):
    def test_render_requires_song_specific_snapshot(self):
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'validate_mastering_reference'):
            stem = pipeline.Stem(Path('guitar.wav'), 'Guitar', 'guitar', 1000, 1, 1000, 1., 1000, 0., 'test')
            with self.assertRaisesRegex(RuntimeError, 'independent per-song'):
                pipeline.render_segment([stem], pipeline.Segment(0, 1), 1, Path(folder), prepared_plan=None)

    def test_plan_hash_changes_when_selection_changes(self):
        first = pipeline.Segment(0.0, 480.0)
        second = pipeline.Segment(0.0, 481.0)
        self.assertNotEqual(
            jam_app.mix_plan_signature(1, first, {"target_lufs": -14}),
            jam_app.mix_plan_signature(1, second, {"target_lufs": -14}),
        )


if __name__ == "__main__":
    unittest.main()
