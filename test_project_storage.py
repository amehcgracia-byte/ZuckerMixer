from pathlib import Path
import pytest
import project_storage as storage


def test_sessions_directory_and_source_identity(tmp_path):
    source = tmp_path/'ZZSesions/2026/August/session/raw/sound'
    path = storage.project_directory(source)
    assert path.parent == tmp_path/'ZZSesions/ZuckerMixer'
    assert path.name.startswith('session-')
    assert path != storage.project_directory(source.parent/'other')


def test_migration_preserves_saved_cuts_and_export_paths(tmp_path, monkeypatch):
    project = tmp_path/'projects/session'
    monkeypatch.setattr(storage, 'project_directory', lambda _: project)
    old = tmp_path/'old';old.mkdir()
    text = '{"cuts":[[0,30]],"render":"/external/song.mp3"}'
    (old/'manual_editor_state.json').write_text(text)
    target = storage.migrate_source_state('/source', old)
    assert (target/'manual_editor_state.json').read_text() == text
    assert not old.exists()
    assert storage.migrate_source_state('/source', old) == target


def test_failed_copy_preserves_original(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'project_directory', lambda _: tmp_path/'project')
    old = tmp_path/'old';old.mkdir();(old/'cuts.json').write_text('saved')
    monkeypatch.setattr(storage.shutil, 'copytree', lambda *args: (_ for _ in ()).throw(OSError('disk disconnected')))
    with pytest.raises(OSError):storage.migrate_source_state('/source', old)
    assert (old/'cuts.json').read_text() == 'saved'
    assert not (tmp_path/'project/.zuckermixer').exists()


def test_cached_analysis_paths_are_relocated_but_exports_are_not(tmp_path, monkeypatch):
    import json
    project = tmp_path/'project'
    monkeypatch.setattr(storage, 'project_directory', lambda _: project)
    old = tmp_path/'old';old.mkdir()
    (old/'mix_plans.json').write_text(json.dumps({'analysis_cache_path':str(old/'analysis.npz'), 'export':'/external/song.mp3'}))
    (old/'analysis.npz').write_bytes(b'cache')
    target = storage.migrate_source_state('/source', old)
    saved = json.loads((target/'mix_plans.json').read_text())
    assert saved['analysis_cache_path'] == str(target/'analysis.npz')
    assert saved['export'] == '/external/song.mp3'
    assert Path(saved['analysis_cache_path']).read_bytes() == b'cache'


def test_application_import_with_disconnected_project_disk(tmp_path):
    import os, subprocess, sys
    state = tmp_path/'state';state.mkdir()
    (state/'sources').symlink_to(tmp_path/'disconnected', target_is_directory=True)
    result = subprocess.run([sys.executable, '-c', 'import jam_app; assert jam_app.ACTIVE_SOURCE_STATE_ROOT.is_dir()'],
        cwd=Path(__file__).parent, env={**os.environ, 'ZUCKER_MIXER_STATE_ROOT':str(state)}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
