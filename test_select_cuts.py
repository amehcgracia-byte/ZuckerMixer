import json
from pathlib import Path

import jam_app
import jam_mix_pipeline as pipeline


def test_select_cuts_contract_and_endpoint(tmp_path, monkeypatch):
    original_state = jam_app.ensure_pipeline_state
    original_path = jam_app.SEGMENT_SELECTIONS_PATH
    try:
        segment = pipeline.Segment(0.0, 600.0, nominal_end=600.0)
        stem = type("StemStub", (), {"offset_seconds": 0.0, "timeline_duration": 1200.0})()
        monkeypatch.setattr(jam_app, "ensure_pipeline_state", lambda: {"segments": [segment], "stems": [stem]})
        selection_path = tmp_path / "segment_selections.json"
        monkeypatch.setattr(jam_app, "SEGMENT_SELECTIONS_PATH", selection_path)
        client = jam_app.app.test_client()
        response = client.post("/api/segment-selection/1", json={"start_sec": 100.0, "end_sec": 700.0})
        assert response.status_code == 200
        saved = json.loads(selection_path.read_text())
        assert saved["segments"]["1"]["start_sec"] == 100.0
        assert saved["segments"]["1"]["end_sec"] == 700.0
        invalid = client.post("/api/segment-selection/1", json={"start_sec": 0, "end_sec": 0})
        assert invalid.status_code == 400
    finally:
        monkeypatch.setattr(jam_app, "ensure_pipeline_state", original_state)
        monkeypatch.setattr(jam_app, "SEGMENT_SELECTIONS_PATH", original_path)


def test_select_cuts_is_visual_and_manual_authority():
    html = Path("static/app.js").read_text(encoding="utf-8")
    assert "Select Cuts" in html
    assert "/api/segment-selection/" in html
    assert "cut-handle left" in html
    assert "cut-handle right" in html
    assert "cut-center" in html
