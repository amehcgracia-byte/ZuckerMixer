import json
import unittest
from pathlib import Path

import jam_app


ROOT = Path(__file__).resolve().parent


class RenderLifecycleTest(unittest.TestCase):
    def test_frontend_has_one_guarded_render_path_and_snapshot(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(source.count("async function mixSongs("), 1)
        self.assertIn("if (renderInProgress)", source)
        self.assertIn("const overridesSnapshot = cloneOverridesPayload()", source)
        self.assertIn("await waitForRenderJob(jobId)", source)
        self.assertIn("finally {", source)

    def test_frontend_exposes_all_mix_actions_through_mix_songs(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        for marker in ("data-mix-one", "data-mix-settings", "#mixSelected", "#mixAll"):
            self.assertIn(marker, source)
        self.assertIn("mixSongs([song.id])", source)
        self.assertIn("mixSongs([songIndex], false, false)", source)
        self.assertIn("mixSongs([...checkedSongs], true, true)", source)

    def test_snapshot_preserves_user_fader_and_gain(self):
        payload = {
            "songs": {
                "2": {
                    "stems": {
                        "Keys R.wav": {"fader_db": -6, "gain_db": 1.5, "mute": False},
                        "Vocal.wav": {"fader_db": 3, "gain_db": 0, "mute": False},
                    }
                }
            }
        }
        normalized = jam_app.normalize_overrides(payload)
        stems = normalized["songs"]["2"]["stems"]
        self.assertEqual(stems["Keys R.wav"]["fader_db"], -6.0)
        self.assertEqual(stems["Vocal.wav"]["fader_db"], 3.0)
        self.assertEqual(stems["Keys R.wav"]["gain_db"], 1.5)

    def test_active_render_is_rejected_instead_of_duplicated(self):
        original_jobs = jam_app.jobs
        original_ffmpeg = jam_app.ffmpeg_status
        try:
            jam_app.jobs = [{"id": "existing-job", "kind": "render", "status": "running", "heartbeat": 0}]
            jam_app.ffmpeg_status = lambda: {"ok": True}
            with self.assertRaisesRegex(RuntimeError, "existing-job"):
                jam_app.enqueue("render", [1], render_target="/tmp/zucker-test-output")
        finally:
            jam_app.jobs = original_jobs
            jam_app.ffmpeg_status = original_ffmpeg

    def test_fader_conversion_is_monotonic_and_single_db_conversion(self):
        source = (ROOT / "jam_mix_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("y *= db_to_amp(level_gain_db)", source)
        self.assertIn('"level_gain_db": level_gain_db', source)
        self.assertNotIn("10 ** (fader_db / 10)", source)


if __name__ == "__main__":
    unittest.main()
