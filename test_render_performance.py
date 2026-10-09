from types import SimpleNamespace
from pathlib import Path
import os
import numpy as np
import pytest
import jam_mix_pipeline as p

@pytest.fixture
def measurement(tmp_path, monkeypatch):
    file=tmp_path/'audio.wav';file.write_bytes(b'first')
    p._measure_ebur128_cached.cache_clear()
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stderr='Integrated loudness:\n I: -12.3 LUFS\nTrue peak:\n Peak: -1.2 dBFS')
    monkeypatch.setattr(p,'resolve_ffmpeg',lambda:'ffmpeg')
    monkeypatch.setattr(p.subprocess,'run',run)
    yield file,calls
    p._measure_ebur128_cached.cache_clear()


def test_true_peak_and_lufs_share_one_independent_pass(measurement):
    file,calls=measurement
    assert p.measure_true_peak_db(file)==-1.2
    assert p.measure_encoded_lufs(file)==-12.3
    assert p.measure_true_peak_db(file)==-1.2
    assert len(calls)==1
    assert 'ebur128=peak=true:framelog=verbose' in calls[0]


def test_same_size_rewrite_invalidates_meter(measurement):
    file,calls=measurement
    p.measure_true_peak_db(file)
    stat=file.stat();file.write_bytes(b'other');os.utime(file,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
    p.measure_true_peak_db(file)
    assert len(calls)==2


def test_replacement_with_same_times_and_size_invalidates_meter(measurement):
    file,calls=measurement
    p.measure_true_peak_db(file);stat=file.stat()
    replacement=file.with_suffix('.new');replacement.write_bytes(b'other');os.utime(replacement,ns=(stat.st_atime_ns,stat.st_mtime_ns));replacement.replace(file)
    p.measure_encoded_lufs(file)
    assert len(calls)==2


def test_failed_measurement_is_never_reused(measurement,monkeypatch):
    file,calls=measurement
    monkeypatch.setattr(p.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=1,stderr='read error'))
    with pytest.raises(RuntimeError):p.measure_true_peak_db(file)
    assert np.isnan(p.measure_encoded_lufs(file))
    assert p._measure_ebur128_cached.cache_info().currsize==0


@pytest.mark.parametrize('shape',[(96000,2),(96000,), (0,2)])
def test_diagnostic_meter_preserves_peak_and_double_precision_rms(shape):
    audio=np.random.default_rng(4).normal(0,.1,shape).astype(np.float32)
    meter={'peak':0.,'sumsq':0.,'count':0};p.accumulate_audio_meter(meter,audio)
    double=audio.astype(np.float64)
    assert meter['peak']==(float(np.max(np.abs(double))) if audio.size else 0.)
    assert meter['sumsq']==pytest.approx(float(np.sum(double*double)),rel=1e-12,abs=1e-12)
    assert meter['count']==audio.size


def test_stem_pool_keeps_stem_order_for_sequential_mixing():
    import threading, time
    seen = []
    def work(i):
        time.sleep(0.02 * (5 - i))  # later stems finish first
        seen.append(threading.current_thread().name)
        return i
    with p._StemPool(4) as pool:
        assert list(pool.map(work, range(5))) == [0, 1, 2, 3, 4]
    assert any(name.startswith('render-stem') for name in seen)
    with p._StemPool(1) as pool:
        assert list(pool.map(lambda i: i * 2, range(3))) == [0, 2, 4]


def test_stem_pool_propagates_stem_errors():
    def work(i):
        if i == 2:
            raise ValueError('bad stem')
        return i
    with pytest.raises(ValueError, match='bad stem'):
        with p._StemPool(3) as pool:
            list(pool.map(work, range(4)))


@pytest.mark.parametrize('configured,stems,expected', [('1', 10, 1), ('8', 3, 3), ('junk', 10, 1), ('0', 10, 1)])
def test_render_stem_workers_honours_override(monkeypatch, configured, stems, expected):
    monkeypatch.setenv('ZUCKER_RENDER_STEM_WORKERS', configured)
    assert p.render_stem_workers(stems) == expected
