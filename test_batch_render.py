import json
from pathlib import Path

import pytest

import jam_app


def test_batch_ids_are_complete_ordered_and_strict():
    assert jam_app.validate_batch_song_ids([3, 1, 2], {1, 2, 3}) == [3, 1, 2]
    with pytest.raises(ValueError, match="unavailable"):
        jam_app.validate_batch_song_ids([1, 9], {1, 2, 3})
    with pytest.raises(ValueError, match="duplicate"):
        jam_app.validate_batch_song_ids([1, 1], {1, 2, 3})


def test_batch_contract_and_partial_failure_are_explicit():
    source = Path(__file__).with_name("jam_app.py").read_text(encoding="utf-8")
    frontend = Path(__file__).with_name("static").joinpath("app.js").read_text(encoding="utf-8")
    assert '"requested_song_ids": list(songs)' in source
    assert '"validated_song_ids": list(songs)' in source
    assert 'child_job_id = f"{job_id}-song-{pos:02d}"' in source
    assert '"status": "partial_failed"' in source
    assert '"batch_summary"' in source
    assert 'expected_song_count: requestedSongs.length' in frontend
    assert '"partial_failed"' in frontend


def test_per_song_artifacts_and_manifest_are_promoted():
    source = Path(__file__).with_name("jam_app.py").read_text(encoding="utf-8")
    pipeline = Path(__file__).with_name("jam_mix_pipeline.py").read_text(encoding="utf-8")
    assert '"premaster_wav": str(premaster_artifact)' in pipeline
    assert '"master_wav": str(master_artifact)' in pipeline
    assert '"manifest_path"' in source
    assert '"stage_execution_counts"' in source
    assert 'RENDER_DIAGNOSTICS_ROOT' in source
    assert 'final_destination_contains_only' in source
    assert 'diagnostics_dir=diagnostic_job_dir' in source
    assert 'summary_path = diagnostic_job_dir' in source


def test_mix_everything_uses_all_visible_songs_and_shows_count():
    frontend = Path(__file__).with_name("static").joinpath("app.js").read_text(encoding="utf-8")
    start = frontend.index("function mixEverything()")
    end = frontend.index("function chooseMixSource()", start)
    block = frontend[start:end]
    assert "renderableSongs().map" in block
    assert "Preparing ${songs.length} songs" in block
    assert "[1, 2, 3]" not in block


def test_out_of_range_windows_remain_visible_and_renderable_with_warning():
    source = Path(__file__).with_name("jam_app.py").read_text(encoding="utf-8")
    frontend = Path(__file__).with_name("static").joinpath("app.js").read_text(encoding="utf-8")
    assert "MIN_RENDER_DURATION_SECONDS = 480.0" in source
    assert "MAX_RENDER_DURATION_SECONDS = 780.0" in source
    assert '"render_valid"' in source
    assert 'if not song.get("skipped")' in source
    assert "return visibleSongs();" in frontend
    assert "needs review" in frontend.lower()


def test_render_all_exposes_stall_diagnostics_and_keeps_source_stems():
    app = Path(__file__).with_name("jam_app.py").read_text(encoding="utf-8")
    pipeline = Path(__file__).with_name("jam_mix_pipeline.py").read_text(encoding="utf-8")
    frontend = Path(__file__).with_name("static").joinpath("app.js").read_text(encoding="utf-8")
    assert '"last_event_at"' in app
    assert '"memory_mb"' in app
    assert '"decodable source included; activity measurement is diagnostic only"' in pipeline
    assert '"source broadband noise detected; kept in manifest and muted by safety policy"' in pipeline
    assert "stalled ${silenceSeconds}s" in frontend
