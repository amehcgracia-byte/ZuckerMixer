import subprocess
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
import jam_app as a
import jam_mix_pipeline as p


def test_quiet_preview_preserves_source_level_without_pcm16_hiss(tmp_path,monkeypatch):
    ffmpeg=p.resolve_ffmpeg()
    if not ffmpeg:pytest.skip('FFmpeg unavailable')
    sr=44100;t=np.arange(sr*3)/sr;original=(1e-4*np.sin(2*np.pi*440*t)).astype(np.float32)
    source=tmp_path/'quiet.wav';sf.write(source,original,sr,subtype='FLOAT')
    stem=p.Stem(path=source,role='guitar',name='Quiet guitar',samplerate=sr,channels=1,frames=len(original),duration=3,timeline_frames=len(original),offset_seconds=0,offset_source='test')
    state={'stems':[stem],'segments':[p.Segment(0,3)],'active_stems_detection_version':a.ACTIVE_STEM_DETECTION_VERSION,'active_stems_by_song':{'1':['quiet.wav']}}
    monkeypatch.setattr(a,'ensure_pipeline_state',lambda:state)
    monkeypatch.setattr(a,'song_analysis_snapshot',lambda *args:{'segment_peaks_db':{'quiet.wav':-80}})
    monkeypatch.setattr(a,'PREVIEW_DIR',tmp_path/'preview')
    with a.app.test_client() as client:r=client.get('/stem-full/1/0')
    assert r.status_code==200
    gain=float(r.headers['X-Preview-Source-Gain-Db']);assert gain==60
    encoded=tmp_path/'encoded.mp3';encoded.write_bytes(r.data)
    raw=subprocess.check_output([ffmpeg,'-v','error','-i',str(encoded),'-f','f32le','-ac','1','-ar',str(sr),'-'])
    restored=np.frombuffer(raw,dtype=np.float32)*p.db_to_amp(-gain)
    assert len(restored)==len(original)
    rms=lambda x:float(np.sqrt(np.mean(x*x)))
    assert abs(p.amp_to_db(rms(restored))-p.amp_to_db(rms(original)))<1
    assert p.amp_to_db(rms(restored-original)) < -105
    assert '_full_v3_' in next((tmp_path/'preview').glob('*.mp3')).name
