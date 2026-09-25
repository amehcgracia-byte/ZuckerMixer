import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf
from unittest.mock import patch

import jam_mix_pipeline as pipeline


class SourceLoadingTests(unittest.TestCase):
    def test_recursive_case_insensitive_wav_scan_reports_every_decodable_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            nested = root / "Sub Folder with spaces"
            nested.mkdir()
            sf.write(root / "Main.WAV", np.zeros(44100, dtype=np.float32), 44100)
            sf.write(nested / "Voice é.WaV", np.zeros(44100, dtype=np.float32), 44100)
            (nested / "notes.txt").write_text("not audio", encoding="utf-8")

            paths, report = pipeline.scan_audio_files(root)

            self.assertEqual(2, len(paths))
            self.assertEqual(
                {"Main.WAV", "Sub Folder with spaces/Voice é.WaV"},
                {item["file"] for item in report["accepted"]},
            )
            self.assertEqual("unsupported format", report["skipped"][0]["reason"])
            self.assertEqual("Sub Folder with spaces/notes.txt", report["skipped"][0]["file"])

    def test_missing_folder_is_a_diagnostic_not_a_successful_empty_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "does not exist"
            paths, report = pipeline.scan_audio_files(missing)
            self.assertEqual([], paths)
            self.assertEqual("Error loading folder", report["status"])
            self.assertIn("source folder not found", report["error"])

    def test_api_state_returns_diagnostics_when_detection_raises(self):
        import jam_app

        jam_app.app.config["TESTING"] = True
        with patch.object(jam_app, "load_detection_snapshot", return_value=None), \
             patch.object(jam_app, "api_redetect"), \
             patch.object(jam_app.pipeline, "scan_audio_files", return_value=([], {"source": "", "accepted": [], "skipped": [], "status": "Scanning folder", "error": ""})):
            response = jam_app.app.test_client().get("/api/state")
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertIn("segmentation_status", payload)
        self.assertIn("source_folder", payload)


if __name__ == "__main__":
    unittest.main()
