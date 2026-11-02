"""Peak control must change a linked gain envelope, not distort individual wave cycles."""
import numpy as np
import soundfile as sf
import jam_mix_pipeline as p


def harmonic_ratio(audio, sr=48000, frequency=1000):
    # One steady second, an integer number of periods: no window/leakage correction.
    spectrum = np.abs(np.fft.rfft(audio[:sr].astype('float64')))
    bins = [frequency * n for n in range(2, 20)]
    return np.sqrt(np.sum(spectrum[bins] ** 2)) / max(1e-12, spectrum[frequency])


def test_natural_master_does_not_waveshape_a_loud_sustained_note(tmp_path, monkeypatch):
    sr = 48000
    mono = .18 * np.sin(2 * np.pi * 1000 * np.arange(sr * 3) / sr)
    source, target = tmp_path / 'in.wav', tmp_path / 'out.wav'
    sf.write(source, np.column_stack([mono, mono*.5]), sr, subtype='FLOAT')
    monkeypatch.setattr(p, 'MASTERING_INTENSITY', 'natural')
    p.apply_master_pass(source, target, sr, 18)
    audio, _ = sf.read(target)
    thd = harmonic_ratio(audio[sr:2*sr, 0])
    assert thd < .001, f'natural mastering introduces {100*thd:.2f}% harmonic distortion'
    assert np.max(np.abs(audio)) <= p.db_to_amp(p.TRUE_PEAK_CEILING_DBFS) + 1e-6
    assert np.max(np.abs(audio[sr:,0] - 2*audio[sr:,1])) < 3e-7


def test_transient_limiting_is_continuous_across_processing_blocks(tmp_path, monkeypatch):
    sr = 48000; t = np.arange(sr*3) / sr
    mono = .05*np.sin(2*np.pi*440*t)
    mono[48000:48240] += .8*np.sin(2*np.pi*1000*np.arange(240)/sr)
    source = tmp_path / 'in.wav'
    sf.write(source, np.column_stack([mono, mono*.6]), sr, subtype='FLOAT')
    monkeypatch.setattr(p, 'MASTERING_INTENSITY', 'natural')
    monkeypatch.setattr(p, 'RENDER_CHUNK_SECONDS', .25)
    p.apply_master_pass(source, tmp_path/'small.wav', sr, 12)
    monkeypatch.setattr(p, 'RENDER_CHUNK_SECONDS', 2)
    p.apply_master_pass(source, tmp_path/'large.wav', sr, 12)
    a,_=sf.read(tmp_path/'small.wav'); b,_=sf.read(tmp_path/'large.wav')
    np.testing.assert_allclose(a,b,atol=2e-7)
    assert np.max(np.abs(a)) <= p.db_to_amp(p.TRUE_PEAK_CEILING_DBFS) + 1e-6


def test_quiet_pass_stays_linear_and_reports_completion(tmp_path, monkeypatch):
    sr=48000; t=np.arange(sr)/sr
    source=tmp_path/'in.wav'; target=tmp_path/'out.wav'
    x=np.column_stack([.01*np.sin(2*np.pi*440*t)]*2)
    sf.write(source,x,sr,subtype='FLOAT')
    monkeypatch.setattr(p,'MASTERING_INTENSITY','natural')
    progress=[]
    p.apply_master_pass(source,target,sr,3,progress.append)
    y,_=sf.read(target)
    np.testing.assert_allclose(y,x*p.db_to_amp(3),atol=2e-7)
    assert progress[-1]['ratio'] == 1
