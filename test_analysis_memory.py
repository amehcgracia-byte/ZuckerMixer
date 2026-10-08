import jam_app as a
import jam_mix_pipeline as p


def test_analysis_memory_reuses_disk_analysis_and_invalidates_changed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(a, 'ACTIVE_SOURCE_STATE_ROOT', tmp_path)
    monkeypatch.setattr(a, 'mix_plan_signature', lambda *args: 'signature')
    a.analysis_snapshot_memory.clear()
    path = tmp_path / 'song_analysis_12_signature.npz'
    path.write_bytes(b'first')
    calls = []
    def read(file):
        calls.append(file.read_bytes())
        return {'input_signature': 'signature', 'mix_controls': {'revision': file.read_bytes()}}
    monkeypatch.setattr(p, 'load_analysis_cache', read)
    segment = p.Segment(0, 10)
    first = a.song_analysis_snapshot({}, segment, 12)
    for _ in range(11):
        assert a.song_analysis_snapshot({}, segment, 12) == first
    assert calls == [b'first']
    path.write_bytes(b'replaced analysis')
    assert a.song_analysis_snapshot({}, segment, 12)['mix_controls']['revision'] == b'replaced analysis'
    assert calls == [b'first', b'replaced analysis']
    a.analysis_snapshot_memory.clear()
