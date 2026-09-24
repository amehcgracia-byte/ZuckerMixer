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
