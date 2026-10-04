from pathlib import Path
from unittest.mock import patch
import numpy as np
import jam_mix_pipeline as p


def classify(audio):
    stem=p.Stem(path=Path('input.wav'),role='guitar',name='Input',samplerate=p.RHYTHM_ANALYSIS_SR,channels=1,frames=len(audio),duration=len(audio)/p.RHYTHM_ANALYSIS_SR,timeline_frames=len(audio),offset_seconds=0,offset_source='test')
    with patch.object(p,'_analysis_mono',return_value=audio):
        return p.classify_noise_stems([stem],p.Segment(0,30),p.RHYTHM_ANALYSIS_SR)['input.wav']


def test_loud_empty_hiss_is_muted():
    rng=np.random.default_rng(42)
    audio=rng.normal(0,0.05,p.RHYTHM_ANALYSIS_SR*8).astype(np.float32)
    assert classify(audio)['case']=='B'


def test_filtered_empty_hiss_is_muted():
    rng=np.random.default_rng(12)
    raw=rng.normal(0,0.04,48000*8)
    audio=p.signal.resample_poly(raw,p.RHYTHM_ANALYSIS_SR,48000).astype(np.float32)
    assert classify(audio)['case']=='B'


def test_quiet_sustained_instrument_is_preserved():
    t=np.arange(p.RHYTHM_ANALYSIS_SR*8)/p.RHYTHM_ANALYSIS_SR
    audio=(0.002*np.sin(2*np.pi*440*t)).astype(np.float32)
    assert classify(audio)['case']!='B'


def test_music_with_hiss_is_not_empty():
    rng=np.random.default_rng(3)
    t=np.arange(p.RHYTHM_ANALYSIS_SR*8)/p.RHYTHM_ANALYSIS_SR
    audio=(rng.normal(0,0.002,len(t))+0.1*np.sin(2*np.pi*440*t)*(np.sin(2*np.pi*t)>0)).astype(np.float32)
    assert classify(audio)['case']!='B'


def test_noise_scan_covers_late_entry():
    stem=p.Stem(path=Path('late.wav'),role='keys',name='Late',samplerate=p.RHYTHM_ANALYSIS_SR,channels=1,frames=600*p.RHYTHM_ANALYSIS_SR,duration=600,timeline_frames=600*p.RHYTHM_ANALYSIS_SR,offset_seconds=0,offset_source='test')
    cuts=[]
    def sample(stem,segment,**kwargs):
        cuts.append((segment.start,segment.end))
        return np.zeros(p.RHYTHM_ANALYSIS_SR*2,dtype=np.float32)
    with patch.object(p,'_analysis_mono',side_effect=sample):
        p.classify_noise_stems([stem],p.Segment(0,600),p.RHYTHM_ANALYSIS_SR)
    assert cuts==[(0,100),(250,350),(500,600)]


def test_preview_and_render_share_empty_input_safety():
    analysis={'rms_values_db':{'hiss':-30,'empty':-93,'quiet_music':-60},'dynamic_spread_db':{'hiss':1,'empty':9,'quiet_music':20},'segment_peaks_db':{'empty':-90,'quiet_music':-40},'noise_diagnostics':{'hiss':{'case':'B'}}}
    assert p.empty_noise_stem_names(analysis)=={'hiss','empty'}


def test_low_recording_noise_is_detected_before_mastering_lift():
    rng=np.random.default_rng(5)
    t=np.arange(p.RHYTHM_ANALYSIS_SR*8)/p.RHYTHM_ANALYSIS_SR
    audio=(rng.normal(0,1e-5,len(t))+0.003*np.sin(2*np.pi*440*t)*(np.sin(2*np.pi*t)>0)).astype(np.float32)
    assert classify(audio)['case']=='A'


def test_noise_reduction_preserves_tone_and_reduces_noise_floor():
    rng=np.random.default_rng(8);sr=p.RHYTHM_ANALYSIS_SR
    noise=rng.normal(0,1e-5,sr*8).astype(np.float32)
    t=np.arange(len(noise))/sr;tone=(0.003*np.sin(2*np.pi*440*t)).astype(np.float32)
    info=classify(noise)
    cleaned,_=p.spectral_subtract_noise(noise,sr,info['noise_profile'],sr,info['noise_floor_dbfs'])
    rms=lambda x:float(np.sqrt(np.mean(x*x)))
    assert p.amp_to_db(rms(cleaned)/rms(noise)) < -4
    with_tone,_=p.spectral_subtract_noise(noise+tone,sr,info['noise_profile'],sr,info['noise_floor_dbfs'])
    amplitude=float(np.dot(with_tone,tone)/np.dot(tone,tone))
    assert abs(p.amp_to_db(amplitude)) < 0.5
