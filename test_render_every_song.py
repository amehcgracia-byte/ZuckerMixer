from contextlib import ExitStack
from unittest.mock import patch
import pytest
import jam_app as app
import jam_mix_pipeline as p

@pytest.mark.parametrize('audit_fails',[False,True])
@pytest.mark.parametrize('song_count',[2,27])
def test_batch_attempts_warned_first_song_and_keeps_exact_windows(tmp_path,audit_fails,song_count):
    segments=[p.Segment(i*20,(i+1)*20) for i in range(song_count)]
    job=tmp_path/'job.json';job.write_text(__import__('json').dumps({'id':'all-songs-proof','kind':'render','songs':list(range(1,song_count+1))}))
    progress=[];attempts=[]
    def render(stems,segment,index,out,**kw):
        attempts.append((index,segment.start,segment.end))
        if index==1:raise RuntimeError('unreadable source fixture')
        kw['output_path'].write_bytes(b'fixture')
        return {'index':index,'file':str(kw['output_path'])}
    with ExitStack() as stack:
        for name,value in {'load_render_state':{'segments':segments,'stems':[]},'load_settings':{},'out_dir':tmp_path,'render_diagnostics_dir':tmp_path,'job_status_path':tmp_path/'status.json','record_render':{'path':str(tmp_path/'out.mp3')},'apply_overrides_for_song':{}}.items():
            stack.enter_context(patch.object(app,name,return_value=value))
        for name in ['append_log','lifecycle_log'] : stack.enter_context(patch.object(app,name))
        stack.enter_context(patch.object(app,'app_progress',side_effect=progress.append))
        stack.enter_context(patch.object(app,'visible_index_for_segment',side_effect=lambda n,s:n))
        for name in ['validate_mastering_reference','load_cached_timelines_or_die','write_report']:stack.enter_context(patch.object(p,name))
        audit=[{'song':1,'safe':False,'needs_review':True,'reason':'weak boundary'},{'song':2,'safe':True,'accepted_cut_seconds':35}]
        stack.enter_context(patch.object(p,'validate_final_render_boundaries',side_effect=RuntimeError('audit failed') if audit_fails else None,return_value=audit))
        stack.enter_context(patch.object(p,'render_segment',side_effect=render))
        assert app._run_child_job(job)==1
    assert attempts==[(i+1,i*20,(i+1)*20) for i in range(song_count)]
    assert progress[-1]['batch_summary']['started']==song_count
    assert progress[-1]['batch_summary']['failed']==1
    assert progress[-1]['batch_summary']['completed']==song_count-1


def test_worker_uses_persisted_editor_cuts_without_reapplying_legacy_selections():
    snapshot={"segments":[p.Segment(286,935)]}
    with patch.object(app,'load_settings',return_value={}),patch.object(app,'configure_source_folder'),patch.object(app,'detection_state_signature'),patch.object(app,'load_detection_snapshot',return_value=snapshot),patch.object(app,'apply_saved_segment_selections') as legacy:
        assert app.load_render_state()['segments'][0].start==286
        legacy.assert_not_called()


def test_successful_render_warnings_do_not_leave_completed_files_pending(tmp_path):
    import jam_app as app
    files=[tmp_path/'00.mp3',tmp_path/'01.mp3']
    for path in files:path.write_bytes(b'already validated by render promotion')
    job={'kind':'mix','status':'pending_review','needs_review_count':2,'batch_summary':{'requested':2,'completed':2,'failed':0,'songs':[{'file':str(path)} for path in files]}}
    result=app.reconcile_completed_job_from_disk(job)
    assert result['status']=='done' and result['done_count']==2
    assert job['needs_review_count']==2
    files[1].unlink()
    assert app.reconcile_completed_job_from_disk(job) is None
    job['batch_summary']['failed']=1
    assert app.reconcile_completed_job_from_disk(job) is None


def test_detection_review_cannot_be_marked_as_completed_render():
    import jam_app as app
    assert app.reconcile_completed_job_from_disk({'kind':'redetect','status':'pending_review','batch_summary':{'requested':1,'completed':1,'failed':0}}) is None
