import json
import unittest
from pathlib import Path

import jam_app


ROOT = Path(__file__).resolve().parent


class RenderLifecycleTest(unittest.TestCase):
    def test_canonical_identity_and_original_controls_are_preserved(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        build = (ROOT / "build_app.sh").read_text(encoding="utf-8")
        spec = (ROOT / "zucker_mixer.spec").read_text(encoding="utf-8")
        self.assertIn('APP_NAME="ZuckerMixer"', build)
        self.assertIn('name="ZuckerMixer.app"', spec)
        self.assertIn("<title>ZuckerMixer</title>", html)
        for label in ("Mix this one", "Mix selected", "Mix everything", "Fine-tune", "Mix with these settings"):
            self.assertIn(label, html + (ROOT / "static" / "app.js").read_text(encoding="utf-8"))

    def test_automatic_gain_is_explicit_and_capped(self):
        source = (ROOT / "jam_mix_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("AUTO_MIX_MAX_BOOST_DB = 0.0", source)
        self.assertIn('overrides.get("auto_mix_gain_db", overrides.get("makeup_gain_db"))', source)

    def test_frontend_has_one_guarded_render_path_and_snapshot(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(source.count("async function mixSongs("), 1)
        self.assertIn("if (renderInProgress)", source)
        self.assertIn("const overridesResponse = await fetch(\"/api/overrides\")", source)
        self.assertIn("cloneOverridesPayload()", source)
        self.assertIn("await waitForRenderJob(jobId)", source)
        self.assertIn("finally {", source)
        render_body = source.split("async function mixSongs(", 1)[1].split("\nfunction mixEverything", 1)[0]
        self.assertNotIn("ensureMixParamsForSong", render_body)

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

    def test_child_batch_failure_cannot_be_reported_as_success(self):
        source = (ROOT / "jam_app.py").read_text(encoding="utf-8")
        failure_block = source.split("if batch_errors:", 1)[1].split("if len(rows) != len(songs):", 1)[0]
        self.assertIn('lifecycle_log(\n            "render_failed"', failure_block)
        self.assertIn("return 1", failure_block)
        self.assertIn('lifecycle_log("render_completed"', source)

    def test_worker_failure_is_visible_to_the_parent(self):
        source = (ROOT / "jam_app.py").read_text(encoding="utf-8")
        self.assertIn('error = f"worker exited with code {code}"', source)
        self.assertIn("stderr_tail = tail_text(stderr_path)", source)
        self.assertIn('stage_detail="see details"', source)

    def test_missing_mix_plan_does_not_block_render(self):
        backend = (ROOT / "jam_app.py").read_text(encoding="utf-8")
        frontend = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/mix-plan-status/<int:segment_id>")', backend)
        self.assertIn("async function ensureRenderPlans(songIds)", frontend)
        self.assertNotIn("await ensureRenderPlans(requestedSongs)", frontend)
        self.assertIn('await postOverrides("before-render")', frontend)
        self.assertIn('"lightweight_render": True', backend)

    def test_worker_uses_frozen_persisted_plan_when_ui_snapshot_lacks_auto_stems(self):
        backend = (ROOT / "jam_app.py").read_text(encoding="utf-8")
        self.assertIn("authoritative_payload = normalize_overrides", backend)
        self.assertIn("prepared_mix = load_mix_plan(segment_id, state_snapshot=state)", backend)

    def test_partial_failure_exposes_batch_error_and_worker_identity(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function jobErrorText(job)", source)
        self.assertIn("job.batch_errors", source)
        self.assertIn("PID ${workerPid}", source)
        self.assertIn('job.status === "partial_failed"', source)

    def test_terminal_job_releases_render_controls_in_frontend(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if (tracked && ["done", "partial_failed", "error", "cancelled"].includes(tracked.status))', source)
        self.assertIn("setRenderControlsBusy(false)", source)
        self.assertIn("Render failed:", source)

    def test_fader_conversion_is_monotonic_and_single_db_conversion(self):
        source = (ROOT / "jam_mix_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("y *= db_to_amp(level_gain_db)", source)
        self.assertIn('"level_gain_db": level_gain_db', source)
        self.assertNotIn("10 ** (fader_db / 10)", source)


if __name__ == "__main__":
    unittest.main()
