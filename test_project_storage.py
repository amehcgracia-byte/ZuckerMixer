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
