import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import jam_app as app


def test_redetect_uses_selected_source_and_records_identity():
    with tempfile.TemporaryDirectory() as root:
        with patch.object(app, 'load_settings', return_value={'source_folder': root}), patch.object(app, 'jobs', []), patch.object(app, 'job_queue'), patch.object(app, 'write_job_status'), patch.object(app, 'lifecycle_log'), patch.object(app, 'append_log'):
            job = app._queue_redetect_job(False)
            assert job['source_folder'] == str(Path(root).resolve())
            assert job['source_id'] and job['session_id'] and job['fingerprint']
            assert job['detection_mode'] == 'fresh'


def test_child_keeps_job_source_when_settings_change():
    with patch.object(app, 'child_status_snapshot', {'source_folder': '/job/source'}), patch.object(app, 'load_json', return_value={'source_folder': '/changed/source'}):
        assert app.load_settings()['source_folder'] == '/job/source'


def test_expired_heartbeat_is_not_reused_even_with_live_pid():
    root = str(app.pipeline.SOURCE_DIR.resolve())
    job = {'id': 'stale', 'kind': 'redetect', 'source_folder': root, 'status': 'running', 'heartbeat': 1, 'child_pid': 123, **app.source_job_identity(app.detection_state_signature())}
    with patch.object(app, 'jobs', [job]), patch.object(app, 'read_job_status_for_job', return_value=None), patch.object(app, '_pid_is_alive', return_value=True), patch.object(app, 'write_job_status'), patch.object(app, 'lifecycle_log'):
        assert app._active_redetect_job(root) is None
        assert job['status'] == 'error'


def test_rescan_writes_candidate_without_touching_current_session():
    with tempfile.TemporaryDirectory() as root:
        current = Path(root) / 'current.json'
        candidate = Path(root) / 'candidate.json'
        current.write_text('{"manual":"keep"}')
        state = {'stems': [], 'segments': [], 'raw_songs': [], 'stem_info': [], 'candidate_pending': True}
        with patch.object(app, 'DETECTION_STATE_PATH', current), patch.object(app, 'REDETECTION_CANDIDATE_PATH', candidate), patch.object(app.pipeline, 'DETECTION_RESCAN_MODE', True):
            app.save_detection_snapshot(state, ('source', (None, None), ()))
        assert json.loads(current.read_text()) == {'manual': 'keep'}
        assert json.loads(candidate.read_text())['candidate_pending'] is True


def test_incomplete_multiple_slot_candidate_cannot_be_committed():
    with patch.object(app, 'load_json', return_value={'raw_songs': [{}, {}, {}], 'candidate_pending': True}):
        response = app.app.test_client().post('/api/redetect/commit')
        assert response.status_code == 409


def test_frontend_tracks_exact_requested_job():
    source = (Path(__file__).parent / 'static' / 'app.js').read_text()
    assert 'item.id === queuedJob.id) ||' not in source
    assert 'item.id === detectionJob.id) ||' not in source


def test_whisper_failure_preserves_acoustic_boundaries_as_review_candidate():
    from contextlib import ExitStack
    from types import SimpleNamespace
    import numpy as np
    pipeline = app.pipeline
    stems = [SimpleNamespace(path=Path('Vox.wav'), role='vocal', offset_seconds=0, timeline_duration=1200), SimpleNamespace(path=Path('Kick.wav'), role='drums', offset_seconds=0, timeline_duration=1200)]
    proposals = [pipeline.Segment(0, 600), pipeline.Segment(600, 1200)]
    mc = {'mc_mask': np.zeros(1200, dtype=bool), 'instrument_playing': np.ones(1200, dtype=bool), 'active_levels': {}}
    with ExitStack() as stack:
        replacements = {
            'WHISPER_ALLOWED': True,
            'load_cached_timelines_or_die': lambda *args: {},
            'active_session_end_from_timelines': lambda *args: 1200,
            'auto_calibrate_detection': lambda *args: (mc, proposals),
            'drum_equivalent_stems': lambda *args: [],
            'print_active_level_table': lambda *args: None,
            'print_mc_breaks': lambda *args: None,
            'LAST_WHISPER_STATUS': {'status': 'unavailable', 'reason': 'test model failure'},
            'DETECTION_STRATEGY': {},
        }
        for key, value in replacements.items():
            stack.enter_context(patch.object(pipeline, key, value))
        stack.enter_context(patch.object(pipeline, 'transcribe_speech_candidates', side_effect=RuntimeError('model failed')))
        segments, _ = pipeline.detect_segments(stems)
        assert [(s.start, s.end) for s in segments] == [(0, 600), (600, 1200)]
        assert pipeline.DETECTION_STRATEGY['candidate_only'] is True
        assert all(s.boundary_validation == 'needs_review' for s in segments)


def test_incomplete_redetect_keeps_previous_file_and_archives_backup():
    with tempfile.TemporaryDirectory() as root:
        current = Path(root) / 'detection.json'
        backup = Path(root) / 'backup.json'
        current.write_text(json.dumps({'raw_songs': [{}, {}, {}], 'manual': 'keep'}))
        result = {'raw_songs': [{}, {}], 'candidate_pending': True}
        with patch.object(app, 'DETECTION_STATE_PATH', current), patch.object(app, 'REDETECTION_BACKUP_PATH', backup), patch.object(app, 'STATE_ROOT', Path(root)), patch.object(app, 'configure_source_folder'), patch.object(app, 'load_settings', return_value={}), patch.object(app, 'ensure_pipeline_state', return_value=result), patch.object(app, 'append_log'), patch.object(app, 'app_progress'):
            returned = app.rebuild_detection_state('new-job')
        assert returned['_redetect_outcome'] == 'incomplete'
        assert json.loads(current.read_text())['manual'] == 'keep'
        assert backup.read_bytes() == current.read_bytes()
        assert list((Path(root) / 'migrations').rglob('detection_state-*.json'))


def test_candidate_from_other_job_cannot_be_committed():
    with patch.object(app, 'load_json', return_value={'job_id': 'old', 'raw_songs': [{}, {}]}):
        response = app.app.test_client().post('/api/redetect/commit', json={'job_id': 'new'})
        assert response.status_code == 409


def test_no_vocal_stem_uses_drum_detection_without_whisper():
    from contextlib import ExitStack
    from types import SimpleNamespace
    pipeline = app.pipeline
    stems = [SimpleNamespace(path=Path('Kick.wav'), role='drums', offset_seconds=0, timeline_duration=1200)]
    proposals = [pipeline.Segment(0, 600), pipeline.Segment(600, 1200)]
    with ExitStack() as stack:
        stack.enter_context(patch.object(pipeline, 'WHISPER_ALLOWED', True))
        stack.enter_context(patch.object(pipeline, 'load_cached_timelines_or_die', return_value={}))
        stack.enter_context(patch.object(pipeline, 'active_session_end_from_timelines', return_value=1200))
        stack.enter_context(patch.object(pipeline, 'auto_calibrate_drum_fallback', return_value=proposals))
        transcribe = stack.enter_context(patch.object(pipeline, 'transcribe_speech_candidates'))
        segments, _ = pipeline.detect_segments(stems)
        assert len(segments) == 2
        transcribe.assert_not_called()
        assert pipeline.LAST_WHISPER_STATUS['status'] == 'skipped'
