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


def test_recovery_uses_time_overlap_and_preserves_recent_manual_cuts(editor):
    c,state=editor
    state['segments'][1]=p.Segment(200,300,boundary_source='manual-add-cut')
    a.save_json_atomic(a.SEGMENT_SELECTIONS_PATH,{'segments':{
        '1':{'start_sec':10,'end_sec':90,'source':'manual'},
        '7':{'start_sec':110,'end_sec':190,'source':'manual'},
        '8':{'start_sec':210,'end_sec':290,'source':'manual'}}})
    assert a.restore_missing_saved_opening(state)
    assert [(s.start,s.end) for s in state['segments']]==[(10,90),(110,190),(200,300)]


def test_saved_editor_list_is_authoritative_even_for_short_songs(editor):
    c,state=editor
    response=c.post('/api/segment-selection/1',json={'start_sec':100,'end_sec':101})
    assert response.status_code==200
    assert response.json['slot_count']==2
    assert response.json['songs'][0]['render_valid']
    assert response.json['songs'][0]['duration']==1
    assert state['manual_editor_authoritative']
    assert state['detection_calibration']['expected_target']==2


def test_navigation_reuses_source_waveform_but_reads_latest_markers(editor,monkeypatch):
    c,state=editor
    calls=[]
    def waveform():
        calls.append(1)
        return {'peaks':[0,.2,.1,0], 'window_start_sec':0,'window_end_sec':400,'cached':True}
    monkeypatch.setattr(a,'full_session_waveform',waveform)
    first=c.get('/api/cuts/1').json
    assert first['global_waveform']
    response=c.post('/api/editor-cut-operation',json={'operation':'add','at_sec':150})
    assert response.json['slot_count']==3
    second=c.get('/api/cuts/2',query_string={'waveform_key':first['waveform_key']}).json
    assert second['global_waveform'] is None
    assert len(second['markers'])==3
    assert calls==[1]
    assert c.get('/api/cuts/2',query_string={'waveform_key':'old-project'}).json['global_waveform']
    assert len(calls)==2


def test_song_zero_is_a_display_number_not_an_analysis_id(editor,monkeypatch):
    c,state=editor
    c.post('/api/editor-cut-operation',json={'operation':'numbering','first_song_number':0})
    assert a.visible_index_for_segment(1,state)==0
    assert a.visible_index_for_segment(2,state)==1
    monkeypatch.setattr(a,'load_mix_plan',lambda *args,**kwargs:{'stems':{}})
    monkeypatch.setattr(p,'validate_mastering_reference',lambda *args:None)
    monkeypatch.setattr(a,'app_progress',lambda *args:None)
    plan=a.apply_overrides_for_song(2,1,overrides_snapshot={'songs':{}},state_snapshot=state)
    assert plan['analysis_song_id']==2
    cache={'version':2,'song_id':2,'selection':{'start_sec':200,'end_sec':300},'mix_controls':{}}
    monkeypatch.setattr(p,'load_analysis_cache',lambda *args:cache)
    monkeypatch.setattr(p,'empty_noise_stem_names',lambda *args:{state['stems'][0].path.name})
    plan.update(analysis_cache_path='cached.npz',analysis_cache_signature='test')
    # Passing ID validation reaches the later empty-audio check; using display
    # number 1 here previously failed before any audio was read.
    with pytest.raises(RuntimeError,match='No decodable stems'):
        p.render_segment(state['stems'],state['segments'][1],1,a.STATE_ROOT,prepared_plan=plan)
