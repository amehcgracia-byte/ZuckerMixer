import json,struct
from dataclasses import asdict
from pathlib import Path
import numpy as np
import soundfile as sf
import pytest
import jam_app as a
import jam_mix_pipeline as p

@pytest.fixture
def editor(tmp_path,monkeypatch):
    for name,value in list(vars(a).items()):
        if name.endswith('_PATH') or name.endswith('_ROOT') or name in {'PREVIEW_DIR','pipeline_state','pipeline_state_signature'}:
            monkeypatch.setattr(a,name,value)
    monkeypatch.setattr(a,'STATE_ROOT',tmp_path/'state')
    monkeypatch.setattr(p,'SOURCE_DIR',p.SOURCE_DIR)
    source=tmp_path/'audio';source.mkdir()
    audio=source/'bass.wav';sf.write(audio,np.sin(np.arange(400*8000)*.2)*.2,8000)
    a.configure_source_folder(source)
    stem=p.Stem(path=audio,name='bass',role='bass',samplerate=8000,channels=1,frames=400*8000,duration=400,timeline_frames=400*8000,offset_seconds=0,offset_source='test')
    segments=[p.Segment(100,200),p.Segment(200,300)]
    state={'stems':[stem],'segments':segments,'raw_songs':[{'id':i,'slot_id':f'slot-{i}','session_id':'test','start':s.start,'end':s.end,'source_start':s.start,'source_end':s.end,'segment':asdict(s)} for i,s in enumerate(segments,1)],'stem_info':[]}
    monkeypatch.setattr(a,'ensure_pipeline_state',lambda:state)
    monkeypatch.setattr(a,'append_log',lambda *args:None)
    return a.app.test_client(),state

def test_first_and_last_edges_save_atomically_and_reload(editor):
    c,state=editor
    response=c.post('/api/segment-selection/1',json={'start_sec':0,'end_sec':180})
    assert response.status_code==200,response.json
    assert [(s.start,s.end) for s in state['segments']]==[(0,180),(180,300)]
    restored=a.load_detection_snapshot(a.detection_state_signature())
    assert [(s.start,s.end) for s in restored['segments']]==[(0,180),(180,300)]
    assert c.post('/api/editor-cut-operation',json={'operation':'undo'}).status_code==200
    assert [(s.start,s.end) for s in state['segments']]==[(100,200),(200,300)]
    assert c.post('/api/editor-cut-operation',json={'operation':'redo'}).status_code==200
    assert state['segments'][0].start==0
    assert c.post('/api/segment-selection/2',json={'start_sec':180,'end_sec':400}).status_code==200
    assert state['segments'][-1].end==400

def test_add_before_between_and_after_detected_windows(editor):
    c,state=editor
    assert c.post('/api/editor-cut-operation',json={'operation':'add','at_sec':50}).status_code==200
    assert [(s.start,s.end) for s in state['segments']][:2]==[(0,50),(50,100)]
    assert c.post('/api/editor-cut-operation',json={'operation':'add','at_sec':350}).status_code==200
    assert state['segments'][-1].end==400
    assert c.post('/api/editor-cut-operation',json={'operation':'add','at_sec':350}).status_code==409
    assert c.post('/api/editor-cut-operation',json={'operation':'add','at_sec':float('nan')}).status_code==400

def test_manual_topology_survives_profile_change_but_not_different_audio(editor,monkeypatch):
    c,state=editor
    assert c.post('/api/editor-cut-operation',json={'operation':'add','at_sec':150}).status_code==200
    monkeypatch.setattr(p,'DETECTION_PROFILE_SIGNATURE','another-profile')
    assert len(a.load_detection_snapshot(a.detection_state_signature())['segments'])==3
    state['stems'][0].path.write_bytes(b'changed')
    assert a.load_detection_snapshot(a.detection_state_signature()) is None

def test_number_zero_and_stale_project_guard(editor):
    c,state=editor
    assert c.post('/api/editor-cut-operation',json={'operation':'numbering','first_song_number':0}).status_code==200
    assert [s.assigned_song_number for s in state['segments']]==[0,1]
    assert c.post('/api/segment-selection/1',json={'start_sec':0,'end_sec':200,'source_folder':'/wrong/project'}).status_code==409

def test_audio_is_ranged_and_streamed_from_the_full_timeline(editor):
    c,state=editor
    response=c.get('/api/cuts/audio/1',headers={'Range':'bytes=0-43'})
    assert response.status_code==206 and response.data[:4]==b'RIFF'
    assert struct.unpack_from('<I',response.data,24)[0]==16000
    assert int(response.headers['Content-Length'])==44
    # An unaligned range must equal the same bytes in an aligned decode.
    aligned=c.get('/api/cuts/audio/1',headers={'Range':'bytes=640044-640075'}).data
    partial=c.get('/api/cuts/audio/1',headers={'Range':'bytes=640045-640074'}).data
    assert aligned[1:-1]==partial
    assert any(aligned)
    assert c.get('/api/cuts/audio/1',headers={'Range':'bytes=9999999999-'}).status_code==416


def test_insert_keeps_mix_names_skips_and_undo_restores_them(editor):
    c,state=editor
    a.save_json_atomic(a.OVERRIDES_PATH, {'songs':{'2':{'master_db':-2}}})
    a.save_json_atomic(a.SONG_NAMES_PATH, {'songs':{'2':{'name':'Second'}}})
    a.save_skipped_segments([2])
    assert c.post('/api/editor-cut-operation',json={'operation':'add','at_sec':150}).status_code==200
    assert a.load_json(a.OVERRIDES_PATH,{})['songs']=={'3':{'master_db':-2}}
    assert a.load_json(a.SONG_NAMES_PATH,{})['songs']['3']['name']=='Second'
    assert a.load_skipped_segments()=={3}
    assert c.post('/api/editor-cut-operation',json={'operation':'undo'}).status_code==200
    assert a.load_json(a.OVERRIDES_PATH,{})['songs']=={'2':{'master_db':-2}}
    assert a.load_skipped_segments()=={2}

def test_saved_opening_recovered_without_changing_recent_tail(editor):
    c,state=editor
    a.save_json_atomic(a.SEGMENT_SELECTIONS_PATH,{'segments':{'1':{'start_sec':10,'end_sec':90,'source':'manual','slot_id':'old-opening','session_id':'test'}}})
    assert a.restore_missing_saved_opening(state)
    assert [(s.start,s.end) for s in state['segments']]==[(10,90),(100,200),(200,300)]
    assert state['raw_songs'][-1]['slot_id']=='slot-2'

def test_acoustic_opening_music_is_not_dropped():
    active=np.ones(600,dtype=bool)
    segments=p.songs_from_mc_breaks([(300,320)],active,600)
    assert segments[0].start==0 and segments[0].end==300
    assert segments[1].start==320

def test_stream_resampling_has_no_artificial_block_edges(tmp_path):
    from scipy.signal import resample_poly
    rate=44100; target=16000
    values=np.sin(np.arange(rate*3)*2*np.pi*713/rate).astype(np.float32)*.2
    path=tmp_path/'test.wav';sf.write(path,values,rate,subtype='FLOAT')
    stem=p.Stem(path=path,name='test',role='bass',samplerate=rate,channels=1,frames=len(values),duration=3,timeline_frames=len(values),offset_seconds=0,offset_source='test')
    actual=np.frombuffer(b''.join(a.cut_source_audio_blocks([stem],target,0,target*3)),dtype='<i2')/32767
    expected=resample_poly(values,160,441)
    for boundary in [target,target*2]:
        np.testing.assert_allclose(actual[boundary-20:boundary+20],expected[boundary-20:boundary+20],atol=1/32767)
