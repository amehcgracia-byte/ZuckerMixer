from dataclasses import replace
import threading
import numpy as np
import pytest
import soundfile as sf
import jam_mix_pipeline as p

@pytest.fixture
def stems(tmp_path, monkeypatch):
    source = tmp_path / 'audio'; source.mkdir()
    monkeypatch.setattr(p.os, 'cpu_count', lambda: 4)
    for key, value in [('SOURCE_DIR', source), ('DETECTION_CACHE_ROOT', tmp_path/'cache'), ('LEGACY_DETECTION_CACHE', None), ('PROGRESS_HOOK', None)]:
        monkeypatch.setattr(p, key, value)
    result = []
    for i in range(3):
        path = source / f'stem{i}.wav'
        sf.write(path, np.sin(np.arange(12500)*(.05+i*.01))*.2, 1000)
        result.append(p.Stem(path, path.stem, 'bass', 1000, 1, 12500, 12.5, 12500, 0, 'test'))
    return result


def test_parallel_matches_serial_and_reuses_unchanged(stems, monkeypatch):
    session = p.Segment(0, 12.5)
    original = p.scan_segment_activity
    expected = {s.path.name: original([s], session, s.samplerate, chunk_seconds=10)[4][s.path.name] for s in stems}
    calls = []; lock = threading.Lock(); barrier = threading.Barrier(3)
    def scan(items, *args, **kwargs):
        with lock: calls.append(items[0].path.name)
        barrier.wait(timeout=10)
        return original(items, *args, **kwargs)
    monkeypatch.setattr(p, 'scan_segment_activity', scan)
    updates = []; monkeypatch.setattr(p, 'PROGRESS_HOOK', updates.append)
    p.build_detection_cache(stems)
    assert len(calls) == 3
    actual = p.load_detection_cache(stems)
    for name in expected: np.testing.assert_array_equal(actual[name], expected[name])
    assert [x['bytes_read'] for x in updates] == sorted(x['bytes_read'] for x in updates)
    monkeypatch.setattr(p, 'scan_segment_activity', lambda *a, **k: pytest.fail('cached WAV rescanned'))
    p.build_detection_cache(stems)
    sf.write(stems[1].path, np.ones(12500)*.1, 1000)
    rescanned = []
    def rescan(items, *args, **kwargs):
        rescanned.append(items[0].path.name)
        return original(items, *args, **kwargs)
    monkeypatch.setattr(p, 'scan_segment_activity', rescan)
    p.build_detection_cache(stems)
    assert rescanned == [stems[1].path.name]


def test_added_stem_reuses_existing_window(stems, monkeypatch):
    p.build_detection_cache(stems[:2])
    original = p.scan_segment_activity; scanned = []
    def scan(items, *args, **kwargs):
        scanned.append(items[0].path.name)
        return original(items, *args, **kwargs)
    monkeypatch.setattr(p, 'scan_segment_activity', scan)
    p.build_detection_cache(stems)
    assert scanned == [stems[2].path.name]


def test_corrupt_cache_and_changed_alignment(stems):
    p.build_detection_cache(stems)
    session = p.Segment(0, 12.5); path = p.stem_detection_cache_path(stems[0], session)
    path.write_bytes(b'broken')
    assert p.stem_detection_cache_path(replace(stems[0], offset_seconds=.25), session) != path
    assert p.stem_detection_cache_path(stems[0], p.Segment(0, 13)) != path
    p.build_detection_cache(stems)
    assert np.isfinite(np.load(path, allow_pickle=False)).all()


def test_failed_scan_does_not_publish_partial_cache(stems, monkeypatch):
    def fail(*args, **kwargs): raise OSError('unreadable WAV')
    monkeypatch.setattr(p, 'scan_segment_activity', fail)
    with pytest.raises(OSError, match='unreadable WAV'): p.build_detection_cache(stems)
    assert not p.detection_cache_path().exists()
